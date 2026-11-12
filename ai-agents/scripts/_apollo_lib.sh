# shellcheck shell=bash
# _apollo_lib.sh — shared helpers for the apollo_*.sh scripts. Sourced by them, never run.
#
# The scripts run work as CPU-only throwaway trainings. Three facts about Run:ai shape how:
#
#   - A command string is parsed twice: by the local shell that builds it, then by bash in the
#     pod. A quoted '>' once became a redirection in the pod and wrote a stray file to shared
#     storage. So the script and its arguments travel base64-encoded and are decoded in the pod.
#   - A container that exits non-zero is re-created. Run:ai treats --backoff-limit 0 as unset, so
#     Kubernetes' default of 6 retries applies (7 pods; verified 2026-10-14 on Run:ai 2.25, with
#     --restart-policy Never too), while --backoff-limit 1 is honoured. So jobs are submitted with
#     --backoff-limit 1 as a cap, and the pod-side wrapper records the real exit code as a last
#     log line, "[apollo] exit N", and always exits 0; the local side reads the code back.
#   - A submitted command is capped at 10,000 characters, so anything bigger is refused here,
#     before submitting, with directions to put the code on the PVC first.
#
# Functions:
#   apollo_load_env [VAR...]        read cluster.env, set defaults, require VARs
#                                   (default: PROJECT PVC MOUNT); exits 2 when one is missing
#   apollo_init                     temp dir, plus traps that delete the job on any exit
#   apollo_check_conn               whoami, then a real call; exits 2 with the user's next step
#   apollo_tag                      6-hex tag from the caller's Run:ai user ID ($APOLLO_TAG wins)
#   apollo_name PREFIX              unique name PREFIX-TAG-HHMMSS-PID-RANDOM
#   apollo_job NAME RECORD FILE [ARG...]
#                                   run FILE with bash in a CPU-only job, print its output, set
#                                   APOLLO_RC; RECORD=1 keeps a copy and a log on the PVC
#   apollo_delete NAME              delete a training, with one retry
#   apollo_py MODE ARG...           the JSON helpers shared by apollo_cleanup and apollo_snapshot
#
# Settings, from the environment or cluster.env:
#   CPU, MEM          CPU cores and memory to request         (default 1 and 2G)
#   LARGE_SHM=1       request a large /dev/shm
#   EXCLUDE_NODE      node names to avoid, space-separated
#   AUTO_DELETE       Run:ai deletes the job this long after it ends, as a backstop in case our
#                     own cleanup fails (default 30m; empty disables; KEEP=1 disables it unless set)
#   TIMEOUT_S         give up waiting after this many seconds (default 900). A job still
#                     Running then is left in place, not deleted: raise this for long work
#   STUCK_S           warn once if the job is not Running after this many seconds (default 180)
#   POLL_S            seconds between status polls (default 5)
#   LOG_RETRIES, LOG_RETRY_S   retries while logs are "not ready to stream" (default 6 x 5 s)
#   RETRY_S           pause before retrying a failed delete (default 2)
#   KEEP=1            leave the workload in place afterwards

APOLLO_SELF=${0##*/}
APOLLO_SELF=${APOLLO_SELF%.sh}
APOLLO_JOB=""       # set once a workload may exist; the EXIT trap deletes it
APOLLO_TMP=""
APOLLO_WHOAMI=""
APOLLO_PHASE=""
APOLLO_RC=1
APOLLO_AUTODEL=""   # the auto-deletion delay Run:ai accepted, if any
_APOLLO_TAG=""

# Settings that keep the value they already have when cluster.env is read, so that a one-off
# `WORKDIR=... apollo_run.sh` or a command-line option does what it says.
_APOLLO_VARS="RUNAI PROJECT PVC MOUNT WORKDIR IMAGE CPU MEM LARGE_SHM EXCLUDE_NODE AUTO_DELETE
TIMEOUT_S STUCK_S POLL_S KEEP RECORD"

apollo_die() { echo "$APOLLO_SELF: $*" >&2; exit 2; }
apollo_say() { echo "$APOLLO_SELF: $*" >&2; }

apollo_load_env() {
  local f=${APOLLO_ENV:-./cluster.env} v s kept=""
  if [ -f "$f" ]; then
    for v in $_APOLLO_VARS; do
      if [ -n "${!v+x}" ]; then
        kept="$kept $v"
        printf -v "_apollo_kept_$v" '%s' "${!v}"
      fi
    done
    # shellcheck disable=SC1090
    . "$f"
    for v in $kept; do
      s=_apollo_kept_$v
      printf -v "$v" '%s' "${!s}"
    done
  fi
  RUNAI=${RUNAI:-$(command -v runai || echo "$HOME/.runai/bin/runai")}
  IMAGE=${IMAGE:-sydneyinformaticshub/dgx-interactive-terminal}
  WORKDIR=${WORKDIR:-${MOUNT:-}}
  [ $# -gt 0 ] || set -- PROJECT PVC MOUNT
  for v in "$@"; do
    [ -n "${!v:-}" ] ||
      apollo_die "$v is not set. Run discovery first and write cluster.env (see APOLLO_AGENT_GUIDE.md §3)"
  done
  [ -x "$RUNAI" ] || command -v "$RUNAI" >/dev/null 2>&1 ||
    apollo_die "runai CLI not found at '$RUNAI' — set RUNAI"
}

apollo_init() {
  APOLLO_TMP=$(mktemp -d "${TMPDIR:-/tmp}/apollo.XXXXXX") ||
    apollo_die "cannot create a temporary directory"
  trap apollo_on_exit EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
}

# Runs on every exit. Must not change the exit status, so errexit is off in here: a failing
# command in an EXIT trap would otherwise turn the status into 1.
apollo_on_exit() {
  local rc=$?
  set +e
  if [ -n "$APOLLO_JOB" ]; then
    if [ "${KEEP:-0}" = 1 ]; then
      apollo_say "KEEP=1: leaving workload $APOLLO_JOB in place — delete it when done:" \
        "runai training standard delete $APOLLO_JOB -p $PROJECT"
    else
      apollo_delete "$APOLLO_JOB"
    fi
  fi
  [ -z "$APOLLO_TMP" ] || rm -rf "$APOLLO_TMP"
  exit "$rc"
}

# Exits 2 with the user's next step when CLI output shows a VPN or login problem.
_apollo_conn_error() {
  case $1 in
    *"DNS lookup failed"* | *"no such host"*)
      apollo_die "cannot reach the Run:ai control plane — is the University VPN connected?" \
        "Reconnecting is the user's step" ;;
    *"token has expired"* | *"Authentication failed"*)
      apollo_die "not authenticated — ask the user to run 'runai login'; this step is theirs, not yours" ;;
  esac
}

apollo_check_conn() {
  local out
  if ! APOLLO_WHOAMI=$("$RUNAI" whoami 2>&1); then
    _apollo_conn_error "$APOLLO_WHOAMI"
    apollo_die "not authenticated — ask the user to run 'runai login'; this step is theirs, not yours"
  fi
  # whoami answers from a cached token even with the VPN down; only a real call shows that.
  if ! out=$("$RUNAI" project list 2>&1); then
    _apollo_conn_error "$out"
    printf '%s\n' "$out" >&2
    apollo_die "'runai project list' failed (output above)"
  fi
}

_apollo_sha1_6() {
  local h=""
  h=$(printf %s "$1" | shasum 2>/dev/null | cut -c1-6) || h=""
  case $h in [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) echo "$h"; return ;; esac
  h=$(printf %s "$1" | sha1sum 2>/dev/null | cut -c1-6) || h=""
  case $h in [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) echo "$h"; return ;; esac
  printf %s "$1" | python3 -c \
    'import hashlib,sys; print(hashlib.sha1(sys.stdin.buffer.read()).hexdigest()[:6])' 2>/dev/null
}

# A stable tag naming the caller's throwaway jobs, so apollo_cleanup.sh can pick out only their
# own on a shared project. Derived from the Run:ai user ID rather than the email, which would
# otherwise be visible in every job name.
apollo_tag() {
  local re='logged in with ID ([^[:space:]]+)' id="" h=""
  if [ -z "$_APOLLO_TAG" ]; then
    if [ -n "${APOLLO_TAG:-}" ]; then
      h=$(printf %s "$APOLLO_TAG" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9' | cut -c1-12)
    else
      [ -n "$APOLLO_WHOAMI" ] || APOLLO_WHOAMI=$("$RUNAI" whoami 2>/dev/null) || true
      if [[ $APOLLO_WHOAMI =~ $re ]]; then id=${BASH_REMATCH[1]}; fi
      [ -z "$id" ] || h=$(_apollo_sha1_6 "$id") || h=""
    fi
    _APOLLO_TAG=${h:-anon}
  fi
  echo "$_APOLLO_TAG"
}

# PREFIX-TAG-HHMMSS-PID-RANDOM: lowercase, at most 50 characters, a valid Kubernetes name.
# Time + PID + RANDOM, because parallel submissions with $RANDOM alone risk a collision.
apollo_name() {
  local prefix rest max
  apollo_tag >/dev/null   # fill the cache here, not in the subshells below
  rest="$_APOLLO_TAG-$(date +%H%M%S)-$$-$RANDOM"
  prefix=$(printf %s "$1" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | tr -s '-')
  case $prefix in [a-z]*) ;; *) prefix="j$prefix" ;; esac
  max=$((50 - 1 - ${#rest}))
  prefix=${prefix:0:$max}
  while [ "${prefix%-}" != "$prefix" ]; do prefix=${prefix%-}; done
  echo "$prefix-$rest"
}

apollo_delete() {
  local name=$1 out
  out=$("$RUNAI" training standard delete "$name" -p "$PROJECT" 2>&1) && return 0
  sleep "${RETRY_S:-2}"
  out=$("$RUNAI" training standard delete "$name" -p "$PROJECT" 2>&1) && return 0
  case $out in *"not found"* | *NotFound*) return 0 ;; esac   # already gone
  echo "apollo: could not delete workload $name — delete it later:" \
    "runai training standard delete $name -p $PROJECT" >&2
  return 1
}

_apollo_b64() { base64 | tr -d '\r\n'; }

# The pod-side command (see the top of this file). Kept to ASCII and to one line. The script is
# decoded to a file and run with bash; arguments are each encoded as "x<arg>" so an empty one
# survives, and decoded with a trailing "." so command substitution keeps their newlines.
# shellcheck disable=SC2016  # the single-quoted text is meant to expand in the pod, not here
_apollo_wrapper() {
  local name=$1 record=$2 file=$3 a b64 args="" w
  shift 3
  b64=$(_apollo_b64 <"$file")
  for a in "$@"; do args="$args $(printf 'x%s' "$a" | _apollo_b64)"; done
  w='f=$(mktemp "${TMPDIR:-/tmp}/apollo.XXXXXX") || f=/tmp/apollo.$$; log=; '
  if [ "$record" = 1 ]; then
    w=$w'L=$WORKDIR/logs/apollo; '
    w=$w'if mkdir -p "$L" 2>/dev/null && touch "$L/'$name'.log" 2>/dev/null; then '
    w=$w'rm -f "$f"; f=$L/'$name'.sh; log=$L/'$name'.log; echo "[apollo] record: $log"; '
    w=$w'else echo "[apollo] warning: cannot write to $L, running without a record"; fi; '
  fi
  w=$w'printf %s '$b64' | base64 -d >"$f" || { echo "[apollo] cannot decode the command into'
  w=$w' $f: no base64 in the image, or no writable /tmp?"; echo "[apollo] exit 125"; exit 0; }; '
  w=$w'a=(); '
  if [ -n "$args" ]; then
    w=$w'for x in'$args'; do v=$(printf %s "$x" | base64 -d; echo .); v=${v%.}; a+=("${v#x}"); done; '
  fi
  w=$w'cd "$WORKDIR" 2>/dev/null || true; '
  # A pipeline ends only after tee has exited, so the log is complete (and, being closed,
  # flushed to the PVC) before the marker is written; PIPESTATUS keeps the script's own code.
  w=$w'if [ -n "$log" ]; then bash "$f" "${a[@]}" 2>&1 | tee "$log"; rc=${PIPESTATUS[0]}; '
  w=$w'echo "[apollo] exit $rc" >>"$log"; '
  w=$w'else bash "$f" "${a[@]}"; rc=$?; rm -f "$f"; fi; '
  w=$w'echo "[apollo] exit $rc"; exit 0'
  printf '%s' "$w"
}

_apollo_submit() {
  local name=$1 wrapper=$2 auto=${AUTO_DELETE-30m} n out
  local -a args
  # A kept job should stay until the user deletes it, unless AUTO_DELETE was set explicitly.
  if [ "${KEEP:-0}" = 1 ] && [ -z "${AUTO_DELETE+x}" ]; then auto=""; fi
  # CPU-only by construction: no GPU flag is ever added here.
  args=(training submit "$name" -p "$PROJECT" -i "$IMAGE"
    --cpu-core-request "${CPU:-1}" --cpu-memory-request "${MEM:-2G}" --run-as-user
    --existing-pvc "claimname=$PVC,path=$MOUNT"
    --backoff-limit 1
    -e "MOUNT=$MOUNT" -e "WORKDIR=$WORKDIR")
  case ${LARGE_SHM:-0} in 0 | "") ;; *) args+=(--large-shm) ;; esac
  # shellcheck disable=SC2086  # one flag per word is the point
  for n in ${EXCLUDE_NODE:-}; do args+=(--exclude-node "$n"); done

  APOLLO_JOB=$name
  if [ -n "$auto" ]; then
    # --auto-deletion-time-after-completion is new in runai 2.25 and not yet verified on Apollo:
    # if this CLI or cluster rejects it, submit once more without it.
    if out=$("$RUNAI" "${args[@]}" --auto-deletion-time-after-completion "$auto" \
      --command -- bash -c "$wrapper" 2>&1); then
      APOLLO_AUTODEL=$auto
      return 0
    fi
    case $out in
      *auto-deletion* | *autoDeletion* | *AutoDeletion*)
        apollo_say "this runai rejected --auto-deletion-time-after-completion; submitting without it" ;;
      *) _apollo_submit_failed "$name" "$out" ;;
    esac
  fi
  out=$("$RUNAI" "${args[@]}" --command -- bash -c "$wrapper" 2>&1) ||
    _apollo_submit_failed "$name" "$out"
}

_apollo_submit_failed() {
  # The workload most likely does not exist; one quiet delete covers the case where it does.
  APOLLO_JOB=""
  "$RUNAI" training standard delete "$1" -p "$PROJECT" >/dev/null 2>&1 || true
  _apollo_conn_error "$2"
  printf '%s\n' "$2" >&2
  case $2 in *"at most 10000 characters"*) apollo_die "$(_apollo_too_big_msg)" ;; esac
  apollo_die "submit failed (output above)"
}

_apollo_too_big_msg() {
  echo "Run:ai caps a submitted command at 10,000 characters. Put the code on the PVC first" \
    "(pvc_sync_code.sh into a running workspace, a git clone inside a job, or Globus), then run" \
    "it from there, e.g. apollo_run.sh 'bash \$WORKDIR/<path>/script.sh'"
}

# Poll until the job reaches a terminal phase; returns 1 on timeout. Sets APOLLO_PHASE.
_apollo_wait() {
  local name=$1 start=$SECONDS elapsed out phase last="" warned=0 conn_fail=0
  local timeout=${TIMEOUT_S:-900} stuck=${STUCK_S:-180}
  APOLLO_PHASE=""
  while :; do
    if out=$("$RUNAI" training standard describe "$name" -p "$PROJECT" 2>&1); then
      conn_fail=0
      phase=$(awk '!done && /^[[:space:]]*Phase:/ {
                     sub(/^[[:space:]]*Phase:[[:space:]]*/, ""); sub(/[[:space:]]+$/, "")
                     print; done = 1 }' <<<"$out")
      [ -z "$phase" ] || APOLLO_PHASE=$phase
    else
      case $out in
        *"DNS lookup failed"* | *"no such host"* | *"token has expired"* | *"Authentication failed"*)
          conn_fail=$((conn_fail + 1))
          [ "$conn_fail" -lt 3 ] || _apollo_conn_error "$out" ;;
      esac
    fi
    if [ "$APOLLO_PHASE" != "$last" ]; then
      apollo_say "$name: $APOLLO_PHASE"
      last=$APOLLO_PHASE
    fi
    case $APOLLO_PHASE in Completed | Succeeded | Failed | Stopped | Error) return 0 ;; esac
    elapsed=$((SECONDS - start))
    if [ "$warned" = 0 ] && [ "$elapsed" -ge "$stuck" ] && [ "$APOLLO_PHASE" != Running ]; then
      warned=1
      apollo_say "warning: start-up is slow ($name is ${APOLLO_PHASE:-not yet reported} after" \
        "${elapsed}s). If its events end at Bound/ContainerCreating the node may be stuck — see" \
        "APOLLO_AGENT_GUIDE.md §6.5 (Run:ai's --pod-running-timeout does not end a pod stuck in" \
        "ContainerCreating). Events: runai training standard describe $name -p $PROJECT"
    fi
    [ "$elapsed" -lt "$timeout" ] || return 1
    sleep "${POLL_S:-5}"
  done
}

# Logs go to $APOLLO_TMP/logs. Logs can be "not ready to stream" for a few seconds even after
# the phase is terminal, so that error is retried; any other error is printed.
_apollo_logs() {
  local name=$1 retries=$2 i=0
  while :; do
    if "$RUNAI" training standard logs "$name" -p "$PROJECT" \
      >"$APOLLO_TMP/logs" 2>"$APOLLO_TMP/logs.err"; then
      return 0
    fi
    i=$((i + 1))
    if [ "$i" -gt "$retries" ] ||
      ! grep -qE 'not ready to stream|pod is not ready' "$APOLLO_TMP/logs.err"; then
      cat "$APOLLO_TMP/logs.err" >&2
      return 1
    fi
    sleep "${LOG_RETRY_S:-5}"
  done
}

# Print the logs without the wrapper's lines: the last "[apollo] exit N" goes to
# $APOLLO_TMP/rc, other "[apollo]" notes go to stderr.
_apollo_print_logs() {
  awk -v self="$APOLLO_SELF" -v rcfile="$APOLLO_TMP/rc" '
    { sub(/\r$/, "") }
    match($0, /\[apollo\] exit [0-9]+$/) {
      rc = substr($0, RSTART + 14); pre = substr($0, 1, RSTART - 1)
      if (pre != "") print pre
      next
    }
    /^\[apollo\] / { print self ": " substr($0, 10) > "/dev/stderr"; next }
    { print }
    END { if (rc != "") print rc > rcfile }' "$APOLLO_TMP/logs"
}

apollo_job() {
  local name=$1 record=$2 file=$3 wrapper len timed_out=0
  shift 3
  case $name in
    [!a-z]* | *[!a-z0-9-]* | *-)
      apollo_die "invalid workload name '$name': lowercase letters, digits and '-'," \
        "starting with a letter" ;;
  esac
  [ "${#name}" -le 50 ] || apollo_die "workload name '$name' is longer than 50 characters"
  wrapper=$(_apollo_wrapper "$name" "$record" "$file" "$@")
  len=$((${#wrapper} + 8)) # the --command text is "bash -c <wrapper>"
  if [ "$len" -ge 9500 ]; then
    apollo_die "the command is $len characters once encoded for the pod (the limit here is" \
      "9,500). $(_apollo_too_big_msg)"
  fi
  _apollo_submit "$name" "$wrapper"
  apollo_say "submitted $name (CPU-only, ${CPU:-1} CPU, ${MEM:-2G}, image $IMAGE)"

  _apollo_wait "$name" || timed_out=1
  rm -f "$APOLLO_TMP/rc"
  if [ "$timed_out" = 1 ]; then
    _apollo_logs "$name" 0 2>/dev/null || : >"$APOLLO_TMP/logs"
  else
    _apollo_logs "$name" "${LOG_RETRIES:-6}" || : >"$APOLLO_TMP/logs"
  fi
  _apollo_print_logs

  if [ -s "$APOLLO_TMP/rc" ]; then
    APOLLO_RC=$(cat "$APOLLO_TMP/rc")
    [ "$APOLLO_RC" = 0 ] || apollo_say "the command exited with status $APOLLO_RC"
  elif [ "$timed_out" = 1 ] && [ "$APOLLO_PHASE" = Running ]; then
    # Deleting a job that is still working would throw its work away, and deletes are the
    # user's call: leave it, and say how to follow it.
    APOLLO_RC=1
    APOLLO_JOB=""
    apollo_say "timed out after ${TIMEOUT_S:-900}s, but $name is still Running, so it was left" \
      "in place. Follow it: runai training standard logs $name -p $PROJECT (the last line" \
      "'[apollo] exit N' gives its exit code). Delete it when done:" \
      "runai training standard delete $name -p $PROJECT${APOLLO_AUTODEL:+ (or Run:ai deletes it $APOLLO_AUTODEL after it ends)}." \
      "Next time, raise TIMEOUT_S."
  elif [ "$timed_out" = 1 ]; then
    APOLLO_RC=1
    apollo_say "timed out after ${TIMEOUT_S:-900}s waiting for $name (phase:" \
      "${APOLLO_PHASE:-never reported}); it never started, so it is deleted. A first image pull" \
      "can take several minutes — raise TIMEOUT_S"
  else
    APOLLO_RC=1
    apollo_say "workload $name ended in phase $APOLLO_PHASE without reporting an exit code;" \
      "run with KEEP=1 and inspect: runai training standard describe $name -p $PROJECT"
  fi
}

# JSON helpers for `runai training list --json` and `runai workspace list --json`. The field
# names of that JSON are not documented, so lookups are tolerant; see ASSUMED below.
IFS= read -r -d '' _APOLLO_PY <<'PY' || true
import datetime, json, os, re, sys, time

LIST_KEYS = ("workloads", "items", "data")
NAME_KEYS = ("name", "workloadName", "Name")
PHASE_KEYS = ("phase", "status", "Phase", "Status")
TIME_KEYS = ("createdAt", "creationTimestamp", "submittedAt")
ASSUMED = ("assumed: a JSON list of workloads, or an object holding it under workloads/items/"
           "data; each workload with name/workloadName/Name and phase/status/Phase/Status, "
           "at the top level or under metadata/status")
TERMINAL = ("completed", "succeeded", "failed", "stopped", "error")
DONE = ("completed", "succeeded")
FAILED = ("failed", "error")
RUNNING = ("running", "degraded")
PRE_RUN = ("pending", "creating", "initializing", "resuming", "queued")
SELF = os.environ.get("APOLLO_SELF", "apollo")


class Unparsable(Exception):
    pass


def field(item, keys):
    for scope in (item, item.get("metadata"), item.get("status")):
        if isinstance(scope, dict):
            for k in keys:
                v = scope.get(k)
                if isinstance(v, (str, int, float)) and not isinstance(v, bool) and str(v):
                    return str(v)
    return ""


def load(path):
    with open(path) as f:
        text = f.read().strip()
    if text in ("", "null") or re.match(r"(?i)no( \w+)+ found", text):
        return []
    try:
        doc = json.loads(text)
    except ValueError:
        raise Unparsable()
    if isinstance(doc, dict):
        keys = [k for k in LIST_KEYS if k in doc]
        if not keys:
            keys = [k for k, v in doc.items() if isinstance(v, list)]
            if len(keys) != 1:
                raise Unparsable()
        doc = doc[keys[0]]
        if doc is None:
            return []
    if not isinstance(doc, list):
        raise Unparsable()
    rows = []
    for item in doc:
        if not isinstance(item, dict):
            raise Unparsable()
        name = field(item, NAME_KEYS)
        if not name:
            raise Unparsable()
        rows.append((name, field(item, PHASE_KEYS) or "?", field(item, TIME_KEYS)))
    return rows


def load_or_die(path, what):
    try:
        return load(path)
    except Unparsable:
        with open(path) as f:
            sys.stdout.write(f.read())
        sys.stderr.write("%s: cannot parse the output of '%s' (shown above); %s\n"
                         % (SELF, what, ASSUMED))
        sys.exit(3)


def age(ts):
    if not ts:
        return "?"
    try:
        if re.match(r"^\d+(\.\d+)?$", ts):
            t = float(ts)
            if t > 1e11:
                t /= 1000.0
        else:
            s = re.sub(r"[Zz]$", "+00:00", ts.strip())
            s = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", s)
            s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s)
            d = datetime.datetime.fromisoformat(s)
            if d.tzinfo is None:
                d = d.replace(tzinfo=datetime.timezone.utc)
            t = d.timestamp()
    except (ValueError, OverflowError):
        return "?"
    n = max(0, int(time.time() - t))
    if n >= 86400:
        return "%dd%dh" % (n // 86400, n % 86400 // 3600)
    if n >= 3600:
        return "%dh%dm" % (n // 3600, n % 3600 // 60)
    if n >= 60:
        return "%dm" % (n // 60)
    return "%ds" % n


def classify(then, now):
    t, n = then.lower(), (now or "").lower()
    if now is None:
        return "missing"
    if n in DONE:
        return "finished"
    if n in FAILED:
        return "failed"
    if n in RUNNING:
        return "still-running"
    if n in PRE_RUN and t in PRE_RUN:
        return "still-pending"
    if t in RUNNING or t in PRE_RUN:
        return "interrupted"
    return "unchanged"


def main(argv):
    mode = argv[0]
    if mode == "names":                       # names FILE
        for name, _, _ in load_or_die(argv[1], "runai training list --json"):
            print(name)
    elif mode == "cleanup":                   # cleanup TRAININGS WORKSPACES TAG ALL PREFIX...
        tfile, wfile, tag, all_tags, prefixes = argv[1], argv[2], argv[3], argv[4] == "1", argv[5:]
        for name, phase, ts in load_or_die(tfile, "runai training list --json"):
            if any(name.startswith("%s-%s-" % (p, tag)) for p in prefixes):
                pass
            elif not (all_tags and any(name.startswith(p + "-") for p in prefixes)):
                continue
            if all_tags:
                action = "list"
            elif phase.lower() in TERMINAL:
                action = "delete"
            else:
                action = "keep"
            print("\t".join(("T", name, phase, age(ts), action)))
        if wfile:
            for name, phase, ts in load_or_die(wfile, "runai workspace list --json"):
                if phase.lower() in ("stopped", "suspended"):
                    print("\t".join(("W", name, phase, age(ts), "-")))
    elif mode == "compare":                   # compare SNAPDIR NOW_TRAININGS NOW_WORKSPACES
        snap, now_t, now_w = argv[1], argv[2], argv[3]
        then, now = {}, {}
        for kind, path in (("training", os.path.join(snap, "training_list.json")),
                           ("workspace", os.path.join(snap, "workspace_list.json"))):
            if os.path.exists(path) and (kind == "training" or now_w):
                for name, phase, _ in load_or_die(path, "the snapshot's " + os.path.basename(path)):
                    then[(kind, name)] = phase
        for kind, path in (("training", now_t), ("workspace", now_w)):
            if path:
                for name, phase, _ in load_or_die(path, "runai %s list --json" % kind):
                    now[(kind, name)] = phase
        fmt = "%-10s %-50s %-13s %-13s %s"
        print(fmt % ("KIND", "NAME", "THEN", "NOW", "CLASS"))
        candidates, requeued = [], []
        for kind, name in sorted(then):
            tp, np_ = then[(kind, name)], now.get((kind, name))
            cls = classify(tp, np_)
            print(fmt % (kind, name, tp, np_ or "-", cls))
            was_active = tp.lower() in RUNNING + PRE_RUN
            line = "  %s  (%s: %s -> %s)" % (name, kind, tp, np_ or "missing")
            if cls == "interrupted" and (np_ or "").lower() in PRE_RUN:
                requeued.append(line)
            elif (was_active and cls in ("failed", "missing")) or cls == "interrupted":
                candidates.append(line)
        if not then:
            print("(the snapshot lists no workloads)")
        print("")
        print("resubmit candidates:")
        print("\n".join(candidates) if candidates else "  (none)")
        if requeued:
            print("re-queued by the scheduler (do not resubmit; check they resume from a checkpoint):")
            print("\n".join(requeued))
    else:
        sys.stderr.write("%s: unknown helper mode %s\n" % (SELF, mode))
        sys.exit(2)


main(sys.argv[1:])
PY

apollo_py() { APOLLO_SELF=$APOLLO_SELF python3 -c "$_APOLLO_PY" "$@"; }
