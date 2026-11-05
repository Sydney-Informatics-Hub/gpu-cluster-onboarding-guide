#!/usr/bin/env bash
# train_wrapper.sh — run a training command as a job's main process, so that a preemption
# reaches it, its log survives, a finished job is not re-run, and a crash does not loop.
#
# Use it as the job's command, in place of `bash -c '...; python train.py'`. With `bash -c`,
# bash is PID 1 and a preemption's SIGTERM may never reach Python, so checkpoint-on-SIGTERM
# never runs. And some schedulers re-create a crashing job's pod even with no retries allowed,
# holding the GPU each time; exiting 0 after logging the real code stops that.
#
#   train_wrapper.sh -- python train.py --config cfg.yaml
#   train_wrapper.sh --log /shared/run1/train.log --done-marker /shared/run1/DONE -- python train.py
#   train_wrapper.sh --exit real -- python train.py          # the scheduler sees the real code
#
#   runai training submit JOB -p PROJECT ... --command -- \
#       /shared/bin/train_wrapper.sh --log /shared/run1/train.log -- python train.py
#   srun train_wrapper.sh --log "$SCRATCH/run1/train.log" -- python train.py
#
# Options:
#
#   --log FILE          append all output (the command's and the wrapper's, stderr merged
#                       into stdout) to FILE as well as stdout. Put it on shared storage, so the
#                       log outlives the pod.
#   --done-marker FILE  if FILE exists at start, do nothing and exit 0 (resubmitting a finished
#                       job is a no-op). Written, with a timestamp, only when the command exits
#                       0 and no signal was received.
#   --exit zero|real    exit status after a normal finish: 0 (default) or the command's own.
#
# Signals: TERM, INT and HUP are forwarded to the command, and the wrapper waits until it has
# really exited, so a checkpoint handler has time to finish (within the scheduler's grace
# period). Under Slurm, scancel signals every process of the step, so the command may see TERM
# twice: make its handler idempotent.
#
# Environment: TEE_WAIT_S  seconds to let the log writer drain at the end (default 10); a
# leftover background process still holding the output cannot then hang the job.
#
# Every line the wrapper writes starts with `[train_wrapper]`; the one to grep for is
# `[train_wrapper] exit <command's code>`, which also says whether a signal was received.
#
# Exit status: 143 if a signal was received (a preemption: the scheduler should see an
# interruption); otherwise 0, or the command's code with --exit real; 2 on a usage error.
set -uo pipefail   # no -e: the command's failure is reported, not fatal to the wrapper

LOG=""
MARKER=""
EXIT_MODE=zero
TEE_WAIT_S=${TEE_WAIT_S:-10}

usage() { awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; }
die() { echo "[train_wrapper] $*" >&2; exit 2; }
need() { [ "$2" -ge 2 ] || die "$1 needs a value (see --help)"; }
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

while [ $# -gt 0 ]; do
  case "$1" in
    --log)         need "$1" $#; LOG=$2; shift 2 ;;
    --done-marker) need "$1" $#; MARKER=$2; shift 2 ;;
    --exit)        need "$1" $#; EXIT_MODE=$2; shift 2 ;;
    -h|--help)     usage; exit 0 ;;
    --)            shift; break ;;
    *)             die "unknown argument '$1'; put the command after -- (see --help)" ;;
  esac
done
[ $# -ge 1 ] || die "no command given; usage: $0 [--log FILE] [--done-marker FILE] [--exit zero|real] -- COMMAND ..."
case "$EXIT_MODE" in zero|real) ;; *) die "--exit must be 'zero' or 'real', not '$EXIT_MODE'" ;; esac
case "$TEE_WAIT_S" in ''|*[!0-9]*) TEE_WAIT_S=10 ;; esac

# --- Log: a FIFO read by a background tee. The tee ignores TERM, INT and HUP, so it outlives a
# signal sent to the whole process group and still writes the wrapper's last lines.
TEE_PID=""
FIFO_DIR=""
if [ -n "$LOG" ]; then
  mkdir -p "$(dirname "$LOG")" 2>/dev/null
  if : >> "$LOG" 2>/dev/null && FIFO_DIR=$(mktemp -d "${TMPDIR:-/tmp}/train_wrapper.XXXXXX") \
     && mkfifo "$FIFO_DIR/out"; then
    ( trap '' TERM INT HUP; exec tee -a "$LOG" ) < "$FIFO_DIR/out" &
    TEE_PID=$!
    exec > "$FIFO_DIR/out" 2>&1
  else
    echo "[train_wrapper] cannot write --log $LOG; logging to stdout only" >&2
  fi
fi

# Close our end of the log, let the tee drain (bounded), then exit with $1.
finish() {
  if [ -n "$TEE_PID" ]; then
    exec >&- 2>&-
    local waited=0
    while kill -0 "$TEE_PID" 2>/dev/null && [ "$waited" -lt $((TEE_WAIT_S * 10)) ]; do
      sleep 0.1 2>/dev/null || sleep 1
      waited=$((waited + 1))
    done
    # Still running means something else holds the output open; all it read is written.
    if kill -0 "$TEE_PID" 2>/dev/null; then kill -KILL "$TEE_PID" 2>/dev/null; fi
    wait "$TEE_PID" 2>/dev/null
    rm -rf "$FIFO_DIR"
  fi
  exit "$1"
}

if [ -n "$MARKER" ] && [ -e "$MARKER" ]; then
  echo "[train_wrapper] done-marker $MARKER exists; nothing to do"
  finish 0
fi

GOT_SIG=""
CHILD=""
# shellcheck disable=SC2329  # called from the traps below
on_signal() {
  GOT_SIG=$1
  echo "[train_wrapper] $1 received at $(now); forwarding it to the command${CHILD:+ (PID $CHILD)}"
  if [ -n "$CHILD" ]; then kill -s "$1" "$CHILD" 2>/dev/null; fi
}
trap 'on_signal TERM' TERM
trap 'on_signal INT' INT
trap 'on_signal HUP' HUP

echo "[train_wrapper] start $(now) on ${HOSTNAME:-?} (wrapper PID $$): $*"
# Job control only for the launch: without it, a background command of a non-interactive shell
# starts with INT ignored, and a forwarded INT would do nothing.
set -m
"$@" &
CHILD=$!
set +m
if [ -n "$GOT_SIG" ]; then kill -s "$GOT_SIG" "$CHILD" 2>/dev/null; fi

# A trapped signal makes `wait` return early (128+n) while the command is still running:
# keep waiting until it has really gone.
rc=0
while :; do
  wait "$CHILD"
  rc=$?
  kill -0 "$CHILD" 2>/dev/null || break
done
if [ -n "$GOT_SIG" ]; then
  # The command may have exited just as a signal interrupted `wait`; its real status is
  # still there to collect (127 means it was already collected above).
  wait "$CHILD" 2>/dev/null
  r=$?
  [ "$r" -eq 127 ] || rc=$r
fi

if [ -n "$GOT_SIG" ]; then
  echo "[train_wrapper] exit $rc ($GOT_SIG received: exiting 143 so the scheduler sees an interruption)"
  finish 143
fi

echo "[train_wrapper] exit $rc"
if [ "$rc" -eq 0 ] && [ -n "$MARKER" ]; then
  mkdir -p "$(dirname "$MARKER")" 2>/dev/null
  if printf 'completed %s: %s\n' "$(now)" "$*" > "$MARKER.tmp.$$" && mv "$MARKER.tmp.$$" "$MARKER"; then
    echo "[train_wrapper] wrote done-marker $MARKER"
  else
    echo "[train_wrapper] could not write done-marker $MARKER"
  fi
fi
if [ "$EXIT_MODE" = real ]; then
  finish "$rc"
fi
if [ "$rc" -ne 0 ]; then
  echo "[train_wrapper] exiting 0 (--exit zero) so a scheduler that re-creates failed pods does not loop; the real code is $rc, above"
fi
finish 0
