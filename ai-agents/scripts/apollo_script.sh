#!/usr/bin/env bash
# apollo_script.sh — run a local multi-line script in a throwaway Apollo workload.
#
# The script FILE on this machine is sent to a CPU-only workload, run there with bash in
# $WORKDIR (with $MOUNT and $WORKDIR set), and its output is printed here; the workload is then
# deleted. Like apollo_run.sh it never requests a GPU, so it is free to run without asking.
# Arguments reach the script intact: spaces, quotes and '$' included.
#
#   apollo_script.sh check_inputs.sh
#   apollo_script.sh --cpu 4 --mem 16G --timeout 3600 prepare.sh "$WORKDIR/raw data" --force
#   apollo_script.sh --no-record --prefix probe probe.sh
#
# Unless --no-record is given, the pod keeps a copy of the script at
# $WORKDIR/logs/apollo/<name>.sh and all of its output in $WORKDIR/logs/apollo/<name>.log on the
# PVC: the logs of a deleted or re-created pod are otherwise lost. If that directory cannot be
# written, the script still runs, with a warning.
#
# Options (environment equivalents in brackets):
#   --image I            container image                       [IMAGE]
#   --cpu N              CPU cores to request (default 1)      [CPU]
#   --mem M              memory to request (default 2G)        [MEM]
#   --large-shm          request a large /dev/shm              [LARGE_SHM=1]
#   --exclude-node N     avoid node N; repeatable              [EXCLUDE_NODE, space-separated]
#   --timeout S          seconds to wait (default 900)         [TIMEOUT_S]
#   --keep               leave the workload afterwards         [KEEP=1]
#   --prefix P           workload name prefix (default script); apollo_cleanup.sh finds it by this
#   --no-record          no copy or log on the PVC             [RECORD=0]
#
# Configuration otherwise as for apollo_run.sh: APOLLO_ENV, RUNAI, PROJECT, PVC, MOUNT, WORKDIR,
# NAME, AUTO_DELETE, STUCK_S, POLL_S (see there and _apollo_lib.sh). The whole script is sent
# with the job, and a submitted command is capped at 10,000 characters: a script bigger than
# about 6,500 characters is refused here — put it on the PVC and run it with apollo_run.sh.
#
# Exit status: 0 when the script succeeded; the script's own exit status when it failed;
# 1 when the workload failed or timed out without reporting one (the phase is printed);
# 2 on a setup or connection problem (configuration, login, VPN, a script that is too big).
set -euo pipefail

# shellcheck source=_apollo_lib.sh
. "$(cd "$(dirname "$0")" && pwd)/_apollo_lib.sh"

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; }

PREFIX=script
need_value() { [ $# -ge 2 ] || apollo_die "$1 needs a value (see --help)"; }
while [ $# -gt 0 ]; do
  case $1 in
    --image) need_value "$@"; IMAGE=$2; shift 2 ;;
    --cpu) need_value "$@"; CPU=$2; shift 2 ;;
    --mem) need_value "$@"; MEM=$2; shift 2 ;;
    --large-shm) LARGE_SHM=1; shift ;;
    --exclude-node) need_value "$@"; EXCLUDE_NODE="${EXCLUDE_NODE:-} $2"; shift 2 ;;
    --timeout) need_value "$@"; TIMEOUT_S=$2; shift 2 ;;
    --keep) KEEP=1; shift ;;
    --prefix) need_value "$@"; PREFIX=$2; shift 2 ;;
    --no-record) RECORD=0; shift ;;
    -h | --help) usage; exit 0 ;;
    --) shift; break ;;
    -*) apollo_die "unknown option $1 (see --help)" ;;
    *) break ;;
  esac
done
[ $# -ge 1 ] || apollo_die "usage: $0 [options] script.sh [args...] (see --help)"
SCRIPT=$1
shift
[ -f "$SCRIPT" ] && [ -r "$SCRIPT" ] || apollo_die "cannot read the script '$SCRIPT'"
case ${TIMEOUT_S:-900} in *[!0-9]* | "") apollo_die "--timeout needs whole seconds" ;; esac

apollo_load_env
apollo_init
apollo_check_conn

NAME=${NAME:-$(apollo_name "$PREFIX")}
apollo_job "$NAME" "${RECORD:-1}" "$SCRIPT" "$@"
exit "$APOLLO_RC"
