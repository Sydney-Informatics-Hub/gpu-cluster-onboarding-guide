#!/usr/bin/env bash
# globus_xfer.sh — submit one Globus transfer to an approved collection, wait for it, and report.
#
# Globus is the preferred route between a laptop (Globus Connect Personal), the Research Data
# Store and a project's cluster storage: it verifies every file by checksum, retries by itself,
# and keeps going if the laptop sleeps. This wrapper adds the policy. It does a DRY RUN unless
# --yes is given, the destination must be on an allow-list, and the result is a summary an agent
# can act on. Paths are COLLECTION_ID:/path; a source path ending in / is a directory.
#
#   globus_xfer.sh SRC_ID:/data/run1/ DST_ID:/project/run1/          # dry run: shows the plan
#   globus_xfer.sh --yes SRC_ID:/data/run1/ DST_ID:/project/run1/    # after the user approves
#   globus_xfer.sh --yes --no-wait --label results-v2 SRC_ID:/out/model.pt DST_ID:/keep/model.pt
#
# Options:
#   --yes             submit; without it nothing is submitted
#   --allow-external  allow a destination outside the allow-list (needs the user's explicit OK)
#   --recursive       transfer a directory (implied when the source path ends with /)
#   --sync-level L    checksum (default) | mtime | size | exists — skip files already there
#   --label L         task label (default apollo-YYYYmmdd-HHMMSS)
#   --timeout S       seconds to wait for completion (default 3600)
#   --no-wait         return as soon as the task is submitted
# Files are always checksum-verified after transfer and keep their modification times.
#
# Configuration comes from the environment, or from cluster.env ($APOLLO_ENV, default
# ./cluster.env). Look IDs up with `globus endpoint search "<name>"` and record them there:
#
#   GLOBUS                 path to the globus CLI                      (default: globus on $PATH)
#   GLOBUS_PVC_COLLECTION  collection ID exposing the project's cluster storage
#   GLOBUS_RDS_COLLECTION  collection ID of the Research Data Store
#   GLOBUS_EXTRA_ALLOWED   more approved destination IDs, space-separated (e.g. the user's own
#                          Globus Connect Personal endpoint)
#   GLOBUS_POLL_S          seconds between status checks while waiting  (default 15)
#
# Exit status: 0 dry run shown, or the transfer SUCCEEDED (with --no-wait: was submitted);
# 1 the transfer failed, or globus reported an error; 2 usage error, destination refused, globus
# missing or not logged in; 3 Globus needs the user's consent (a human step); 4 the task was
# still running when --timeout expired (it carries on; check it later).
set -euo pipefail

APOLLO_ENV=${APOLLO_ENV:-./cluster.env}
# shellcheck disable=SC1090
[ -f "$APOLLO_ENV" ] && . "$APOLLO_ENV"

GLOBUS=${GLOBUS:-globus}
POLL_S=${GLOBUS_POLL_S:-15}
UUID_RE='^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$'

die()   { echo "globus_xfer: $*" >&2; exit 2; }
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }
lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

YES=0 ALLOW_EXT=0 RECURSIVE=0 WAIT=1 SYNC=checksum LABEL="" TIMEOUT=3600 SRC="" DST=""
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) YES=1 ;;
    --allow-external) ALLOW_EXT=1 ;;
    -r|--recursive) RECURSIVE=1 ;;
    --no-wait) WAIT=0 ;;
    --sync-level|--label|--timeout)
      [ $# -ge 2 ] || die "$1 needs a value"
      case "$1" in --sync-level) SYNC=$2 ;; --label) LABEL=$2 ;; --timeout) TIMEOUT=$2 ;; esac
      shift ;;
    --sync-level=*) SYNC=${1#*=} ;;
    --label=*) LABEL=${1#*=} ;;
    --timeout=*) TIMEOUT=${1#*=} ;;
    -h|--help) usage; exit 0 ;;
    -*) die "unknown option: $1 (see --help)" ;;
    *) if [ -z "$SRC" ]; then SRC=$1; elif [ -z "$DST" ]; then DST=$1
       else die "too many arguments: $1"; fi ;;
  esac
  shift
done
[ -n "$DST" ] || die "usage: $0 [--yes] [options] SRC_COLLECTION_ID:/path DST_COLLECTION_ID:/path"

case "$SYNC" in checksum|mtime|size|exists) ;; *) die "--sync-level must be checksum, mtime, size or exists" ;; esac
case "$TIMEOUT" in ''|*[!0-9]*|0) die "--timeout must be a whole number of seconds > 0" ;; esac
case "$POLL_S" in ''|*[!0-9]*|0) POLL_S=15 ;; esac
[ "$POLL_S" -le "$TIMEOUT" ] || POLL_S=$TIMEOUT   # globus refuses an interval above the timeout
LABEL=${LABEL:-apollo-$(date +%Y%m%d-%H%M%S)}

# ID:/path -> id and path, validated. Collection IDs are UUIDs in globus-cli 3.x.
for spec in "$SRC" "$DST"; do
  case "$spec" in *:?*) ;; *) die "expected COLLECTION_ID:/path, got '$spec'" ;; esac
  [[ ${spec%%:*} =~ $UUID_RE ]] || die "'${spec%%:*}' is not a collection ID (a UUID); find it with: globus endpoint search \"<name>\""
  case "${spec#*:}" in /*|\~*) ;; *) die "the path in '$spec' must start with / or ~" ;; esac
done
SRC_ID=$(lower "${SRC%%:*}") SRC_PATH=${SRC#*:}
DST_ID=$(lower "${DST%%:*}") DST_PATH=${DST#*:}
case "$SRC_PATH" in */) RECURSIVE=1 ;; esac

# The destination decides where the data ends up, so it is the side that must be approved.
ALLOWED=$(lower "${GLOBUS_PVC_COLLECTION:-} ${GLOBUS_RDS_COLLECTION:-} ${GLOBUS_EXTRA_ALLOWED:-}" | tr -s ' \t\n' ' ')
APPROVED=""
for var in GLOBUS_PVC_COLLECTION GLOBUS_RDS_COLLECTION GLOBUS_EXTRA_ALLOWED; do
  case " $(lower "${!var:-}" | tr -s ' \t\n' ' ') " in *" $DST_ID "*) APPROVED=$var; break ;; esac
done
if [ -z "$APPROVED" ] && [ "$ALLOW_EXT" = 0 ]; then
  if [ -z "$(printf '%s' "$ALLOWED" | tr -d ' ')" ]; then
    die "no approved collections are configured, so the destination cannot be checked.
  Find the IDs with: globus endpoint search \"<name>\"   (the project's cluster storage collection,
  the Research Data Store, the user's Globus Connect Personal endpoint), confirm them with the
  user, and record them in $APOLLO_ENV as GLOBUS_PVC_COLLECTION, GLOBUS_RDS_COLLECTION and
  GLOBUS_EXTRA_ALLOWED (space-separated)."
  fi
  die "destination $DST_ID is not an approved collection (GLOBUS_PVC_COLLECTION,
  GLOBUS_RDS_COLLECTION, GLOBUS_EXTRA_ALLOWED). Sending data anywhere else takes it off approved
  infrastructure: it needs the user's explicit OK and must fit the project's data rules. Only
  if they agree, re-run with --allow-external."
fi
EXTERNAL_NOTE=""
if [ -z "$APPROVED" ]; then
  EXTERNAL_NOTE="WARNING: destination $DST_ID is NOT an approved collection. This data is leaving approved infrastructure; that needs the user's explicit OK and must fit the project's data rules."
  echo "globus_xfer: $EXTERNAL_NOTE" >&2
fi

if ! command -v "$GLOBUS" >/dev/null 2>&1; then
  die "the globus CLI was not found ('$GLOBUS'). Install globus-cli, e.g. 'pipx install globus-cli'
  (or 'python3 -m pip install --user globus-cli'), or set GLOBUS to its path. Then the USER runs
  'globus login' — a browser sign-in, a human step — and you re-run this."
fi

TMP=$(mktemp -d "${TMPDIR:-/tmp}/globus_xfer.XXXXXX")
trap 'rm -rf "$TMP"' EXIT

# JSON parsing and message scanning, in python3 (no jq dependency).
PY=$(cat <<'PYEOF'
import json, re, sys

def load(path):
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return None, ""
    try:
        return json.loads(text), text
    except ValueError:
        i, j = text.find("{"), text.rfind("}")
        if 0 <= i < j:
            try:
                return json.loads(text[i:j + 1]), text
            except ValueError:
                pass
    return None, text

cmd, args = sys.argv[1], sys.argv[2:]
if cmd == "auth":
    # Text mode prints the exact command; with -F json the CLI dumps the raw API error instead,
    # so rebuild `globus session consent` from its required_scopes, as the CLI itself would.
    found = []
    for path in args:
        doc, text = load(path)
        for line in text.splitlines():
            m = re.match(r"\s*(globus session (?:consent|update)\b.*?)\s*$", line)
            if m and m.group(1) not in found:
                found.append(m.group(1))
        if isinstance(doc, dict):
            ap = doc.get("authorization_parameters") or {}
            scopes = doc.get("required_scopes") or ap.get("required_scopes") or []
            if scopes and not found:
                found.append("globus session consent " + " ".join("'%s'" % s for s in scopes))
            elif (doc.get("code") in ("ConsentRequired", "AuthorizationRequired") or ap) \
                    and not found:
                found.append("globus session update   # see the error above for the identity "
                             "or policy it asks for")
    print("\n".join(found))
    sys.exit(0 if found else 1)
doc, text = load(args[0])
if not isinstance(doc, dict):
    sys.exit(1)
if cmd == "name":
    print(doc.get("display_name") or doc.get("name") or doc.get("canonical_name")
          or "(no display name)")
elif cmd == "get":
    v = doc.get(args[1])
    if v is None:
        sys.exit(1)
    print(v)
elif cmd == "summary":
    def n(k):
        v = doc.get(k)
        return "?" if v is None else v
    b = doc.get("bytes_transferred") or 0
    size = "%d bytes" % b
    for unit, f in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if b >= f:
            size = "%.1f %s (%d bytes)" % (b / f, unit, b)
            break
    s = ("Task %s %s: %s files in total, %s transferred, %s skipped as already identical; "
         "%s transferred; %s faults; checksum verification %s."
         % (doc.get("task_id"), doc.get("status"), n("files"), n("files_transferred"),
            n("files_skipped"), size, n("faults"),
            "on" if doc.get("verify_checksum") else "OFF"))
    if doc.get("nice_status"):
        s += " Current state: %s." % doc["nice_status"]
    fe = doc.get("fatal_error")
    if isinstance(fe, dict):
        s += " Fatal error: %s — %s." % (fe.get("code"), fe.get("description"))
    print(s)
elif cmd == "events":
    events = doc.get("DATA") or []
    for e in events[:int(args[1])]:
        details = e.get("details") or ""
        if len(details) > 400:
            details = details[:400] + "..."
        print("  %s  %s  %s%s" % (e.get("time", ""), e.get("code", ""), e.get("description", ""),
                                  ("\n      " + details) if details else ""))
    if not events:
        print("  (no error events recorded)")
PYEOF
)
pyh() { python3 -c "$PY" "$@"; }

# g NAME ARGS... — run globus with ARGS; output in $TMP/NAME.out and $TMP/NAME.err.
g() { local name=$1; shift; "$GLOBUS" "$@" >"$TMP/$name.out" 2>"$TMP/$name.err"; }

# If globus asked for consent or re-authentication, hand that step to the user and stop.
auth_check() {
  local steps
  if steps=$(pyh auth "$TMP/$1.out" "$TMP/$1.err"); then
    cat "$TMP/$1.err" >&2
    echo "globus_xfer: Globus needs the USER's consent before it can do this (a browser step;"
    echo "  you cannot do it for them). Ask them to run:"
    printf '%s\n' "$steps" | sed 's/^/    /'
    echo "  then re-run this command."
    exit 3
  fi
}

fail() {  # NAME MESSAGE — report a globus failure and exit 1
  auth_check "$1"
  echo "globus_xfer: $2" >&2
  cat "$TMP/$1.out" "$TMP/$1.err" >&2
  exit 1
}

wrc=0
g whoami whoami || wrc=$?
if [ "$wrc" != 0 ]; then
  if [ "$wrc" = 4 ] || grep -Eqi 'MissingLogin|missing login|globus login|log ?in again|no longer valid' \
       "$TMP/whoami.out" "$TMP/whoami.err"; then
    echo "globus_xfer: not logged in to Globus. Ask the user to run \`globus login\` (browser" >&2
    echo "  sign-in; a human step — never do it for them), then re-run this." >&2
    exit 2
  fi
  fail whoami "\`globus whoami\` failed (network?):"
fi

# Display names, so the user can recognise both ends. `endpoint show` refuses GCSv5 collections
# (exit 3, "use globus collection show"), and `collection show` would need an extra
# per-endpoint login; the Transfer API's endpoint document has the name for every kind of
# collection and needs only the login already checked.
resolve() {  # ID -> $RESOLVED (not via $(...), so that auth_check can still talk and exit)
  RESOLVED=""
  if g resolve endpoint show "$1" -F json; then RESOLVED=$(pyh name "$TMP/resolve.out") || RESOLVED=""; fi
  if [ -z "$RESOLVED" ] && g resolve api transfer GET "/endpoint/$1" -F json; then
    RESOLVED=$(pyh name "$TMP/resolve.out") || RESOLVED=""
  fi
  if [ -z "$RESOLVED" ]; then
    fail resolve "could not look up collection $1. Is the ID right? (globus endpoint search \"<name>\")"
  fi
}
resolve "$SRC_ID"; SRC_NAME=$RESOLVED
resolve "$DST_ID"; DST_NAME=$RESOLVED

ARGS=(transfer "$SRC_ID:$SRC_PATH" "$DST_ID:$DST_PATH" --sync-level "$SYNC"
      --verify-checksum --preserve-timestamp --label "$LABEL")
if [ "$RECURSIVE" = 1 ]; then ARGS+=(--recursive); fi
SHOWN=$(printf '%q ' globus "${ARGS[@]}")
SHOWN=${SHOWN% }

MODE="DRY RUN — nothing is submitted" REC="no (single file)" WHERE=" — EXTERNAL"
if [ "$YES" = 1 ]; then MODE="SUBMITTING"; fi
if [ "$RECURSIVE" = 1 ]; then REC="yes"; fi
if [ -n "$APPROVED" ]; then WHERE=" — approved ($APPROVED)"; fi
cat <<EOF
globus_xfer: $MODE
  source       $SRC_NAME ($SRC_ID)
               $SRC_PATH
  destination  $DST_NAME ($DST_ID)$WHERE
               $DST_PATH
  recursive    $REC
  sync level   $SYNC (files already identical at the destination are skipped)
  integrity    checksum verified after transfer; modification times preserved
  label        $LABEL
  command      $SHOWN
EOF
if [ -n "$EXTERNAL_NOTE" ]; then echo "  $EXTERNAL_NOTE"; fi
if [ "$YES" = 0 ]; then
  echo "globus_xfer: show this to the user; after they approve, re-run with --yes."
  exit 0
fi

g submit "${ARGS[@]}" -F json || fail submit "submission failed:"
TASK=$(pyh get "$TMP/submit.out" task_id) || fail submit "submitted, but no task_id in the reply:"
echo "globus_xfer: task $TASK submitted"
echo "globus_xfer: progress: https://app.globus.org/activity/$TASK"

timed_out() {
  echo "globus_xfer: task $TASK is still running after ${TIMEOUT}s. It carries on at Globus; nothing" >&2
  echo "  is lost. Check it later with: globus task show $TASK" >&2
  echo "  or wait again with: globus task wait $TASK --timeout 3600" >&2
  exit 4
}

if [ "$WAIT" = 0 ]; then
  echo "globus_xfer: not waiting. Check it with: globus task show $TASK"
  exit 0
fi

# A distinct exit code separates "still running" from "ended unsuccessfully" (both 1 by default).
wrc=0
g wait task wait "$TASK" --timeout "$TIMEOUT" --polling-interval "$POLL_S" --timeout-exit-code 50 || wrc=$?
if [ "$wrc" = 50 ]; then timed_out; fi
if [ "$wrc" != 0 ]; then auth_check wait; fi   # otherwise the task ended; its status says how

g show task show "$TASK" -F json || fail show "could not read the task status:"
STATUS=$(pyh get "$TMP/show.out" status) || fail show "no status in the task document:"
echo "globus_xfer: $(pyh summary "$TMP/show.out")"
case "$STATUS" in
  SUCCEEDED) exit 0 ;;
  ACTIVE) timed_out ;;
  INACTIVE)
    echo "globus_xfer: the task is INACTIVE — paused until someone acts, usually expired credentials" >&2
    echo "  or consent on one of the collections. Ask the user to open" >&2
    echo "  https://app.globus.org/activity/$TASK and follow the prompt there." >&2
    exit 4 ;;
esac
echo "globus_xfer: task $TASK ended $STATUS. First error events:" >&2
if g events task event-list "$TASK" --filter-errors --limit 5 -F json; then
  pyh events "$TMP/events.out" 5 >&2 || cat "$TMP/events.out" >&2
else
  cat "$TMP/events.err" >&2
fi
exit 1
