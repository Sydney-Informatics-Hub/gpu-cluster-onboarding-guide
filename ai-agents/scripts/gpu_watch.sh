#!/usr/bin/env bash
# gpu_watch.sh — sample a running job's GPUs for about a minute; print one verdict and one action.
#
# A GPU at 40 % utilisation is a paid GPU-hour that is mostly waiting. The first ten minutes of a
# long job are when to find out: this samples `nvidia-smi` where the job runs, takes one `top`
# snapshot, optionally reads the tail of the training log, and hands everything to
# gpu_watch_verdict.py (next to this script), which names the limit and what to do about it.
#
# Read-only: it never changes, restarts or signals the job. It works with any scheduler, because
# everything after `--` is a PREFIX that runs a command where the job runs. No prefix means here
# (a workstation, or a terminal inside the job such as a Jupyter terminal).
#
#   gpu_watch.sh                                                        # this machine's GPUs
#   gpu_watch.sh -n 24 -i 5 --train-log /data/run1/train.jsonl          # two minutes, with log
#   gpu_watch.sh -- runai training standard exec JOB -p PROJECT --      # Run:ai
#   gpu_watch.sh -- srun --jobid 12345 --overlap                        # Slurm
#   gpu_watch.sh -- kubectl exec POD --                                 # plain Kubernetes
#   gpu_watch.sh --restarts 3 --json -- runai training standard exec JOB -p PROJECT --
#
# Options:
#
#   -n SAMPLES        nvidia-smi samples to take (default 12)
#   -i SECONDS        seconds between samples (default 5; 0 for back-to-back)
#   --out FILE        keep the samples CSV here (default: a temporary file, removed at exit)
#   --top-out FILE    keep the `top -bn1` snapshot here (default: temporary)
#   --train-log PATH  training log (JSON lines) as seen by the job; its last 500 lines are read
#                     through the prefix, because the log lives where the job runs
#   --restarts N      how many times the scheduler re-created the job (any N >= 1: restart-loop)
#   --json            print the verdict as one JSON object
#
# Environment: PYTHON  interpreter for gpu_watch_verdict.py (default python3; stdlib only).
#
# Each sample runs `PREFIX nvidia-smi --query-gpu=index,utilization.gpu,memory.used,memory.total,
# power.draw --format=csv,noheader,nounits`. A failed sample is skipped, not fatal; if none
# succeed the verdict is stuck-starting. Locally, every GPU on the machine is sampled, including
# ones other people are using. On a shared (fractional) GPU, utilisation covers every tenant.
#
# Exit status is 0 whatever the verdict (the verdict is data, not an error), 2 on a usage error
# or unreadable input.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
PYTHON=${PYTHON:-python3}
QUERY=--query-gpu=index,utilization.gpu,memory.used,memory.total,power.draw
HEADER=ts,gpu_index,util_gpu,mem_used_mib,mem_total_mib,power_w

N=12
INTERVAL=5
OUT=""
TOP_OUT=""
TRAIN_LOG=""
RESTARTS=0
JSON=""
PREFIX=()

die() { echo "gpu_watch: $*" >&2; exit 2; }
say() { echo "gpu_watch: $*" >&2; }
usage() { awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; }
need() { [ "$2" -ge 2 ] || die "$1 needs a value (see --help)"; }

while [ $# -gt 0 ]; do
  case "$1" in
    -n)          need "$1" $#; N=$2; shift 2 ;;
    -i)          need "$1" $#; INTERVAL=$2; shift 2 ;;
    --out)       need "$1" $#; OUT=$2; shift 2 ;;
    --top-out)   need "$1" $#; TOP_OUT=$2; shift 2 ;;
    --train-log) need "$1" $#; TRAIN_LOG=$2; shift 2 ;;
    --restarts)  need "$1" $#; RESTARTS=$2; shift 2 ;;
    --json)      JSON=1; shift ;;
    -h|--help)   usage; exit 0 ;;
    --)          shift; PREFIX=("$@"); break ;;
    *)           die "unknown argument '$1' (see --help)" ;;
  esac
done

case "$N" in ''|*[!0-9]*) die "-n must be a positive integer, not '$N'" ;; esac
[ "$N" -ge 1 ] || die "-n must be at least 1"
case "$INTERVAL" in ''|*[!0-9.]*|*.*.*) die "-i must be a number of seconds, not '$INTERVAL'" ;; esac
case "$RESTARTS" in ''|*[!0-9]*) die "--restarts must be a whole number, not '$RESTARTS'" ;; esac
[ -f "$HERE/gpu_watch_verdict.py" ] || die "gpu_watch_verdict.py not found next to $0"

WORK=$(mktemp -d "${TMPDIR:-/tmp}/gpu_watch.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
OUT=${OUT:-$WORK/samples.csv}
TOP_OUT=${TOP_OUT:-$WORK/top.txt}

# Run a command where the job runs. The length test avoids "${PREFIX[@]}" on an empty array,
# which bash 3.2 treats as unbound under `set -u`.
on_target() {
  if [ ${#PREFIX[@]} -gt 0 ]; then "${PREFIX[@]}" "$@"; else "$@"; fi
}

if [ ${#PREFIX[@]} -gt 0 ]; then where="via: ${PREFIX[*]}"; else where="locally"; fi
say "$N samples every ${INTERVAL}s, $where"

echo "$HEADER" > "$OUT"
ok=0
last_err=""
i=1
while [ "$i" -le "$N" ]; do
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  if raw=$(on_target nvidia-smi "$QUERY" --format=csv,noheader,nounits 2>"$WORK/err"); then
    # Keep only well-formed rows (an exec wrapper may mix error text into stdout); a value
    # such as [N/A] or [Not Supported] becomes empty.
    rows=$(printf '%s\n' "$raw" | awk -F, -v ts="$ts" '
      {
        for (k = 1; k <= NF; k++) gsub(/^[ \t\r]+|[ \t\r]+$/, "", $k)
        if (NF < 5 || $1 !~ /^[0-9]+$/) next
        line = ts "," $1
        for (k = 2; k <= 5; k++) line = line "," ($k ~ /^[0-9]+(\.[0-9]+)?$/ ? $k : "")
        print line
      }')
    if [ -n "$rows" ]; then
      printf '%s\n' "$rows" >> "$OUT"
      ok=$((ok + 1))
    else
      last_err="nvidia-smi printed no GPU rows: $(printf '%s' "$raw" | head -n 2)"
    fi
  else
    last_err=$(head -n 3 "$WORK/err" 2>/dev/null || true)
    [ -n "$last_err" ] || last_err="nvidia-smi failed (exit status only)"
  fi
  if [ "$i" -lt "$N" ] && [ "$INTERVAL" != 0 ]; then sleep "$INTERVAL"; fi
  i=$((i + 1))
done
if [ "$ok" -eq 0 ]; then
  say "no sample succeeded; last error: ${last_err:-none}"
else
  say "$ok of $N samples succeeded"
fi

args=("$OUT" --restarts "$RESTARTS")
if on_target top -bn1 > "$TOP_OUT" 2>/dev/null && [ -s "$TOP_OUT" ]; then
  args+=(--top "$TOP_OUT")
else
  say "top -bn1 failed; deciding without process information"
fi
if [ -n "$TRAIN_LOG" ]; then
  if on_target tail -n 500 "$TRAIN_LOG" > "$WORK/train.log" 2>/dev/null; then
    args+=(--train-log "$WORK/train.log")
  else
    say "could not read the training log $TRAIN_LOG where the job runs; deciding without it"
  fi
fi
[ -z "$JSON" ] || args+=(--json)

rc=0
"$PYTHON" "$HERE/gpu_watch_verdict.py" "${args[@]}" || rc=$?
exit "$rc"
