#!/usr/bin/env bash
# apollo_run.sh — run one shell command inside an Apollo workload and print its output.
#
# Deliberately CPU-only: it never requests a GPU, so it costs nothing against a project's
# allocation and is safe to run without asking. Use it to inspect cluster storage, check paths,
# verify a stage completed, or run any short non-GPU command.
#
#   apollo_run.sh 'ls -la $WORKDIR'
#   apollo_run.sh 'du -sh $WORKDIR/*'
#   apollo_run.sh 'python -c "import sys; print(sys.version)"'
#
# Configuration comes from the environment, or from a cluster.env file (see $APOLLO_ENV below),
# written during discovery. Nothing is hard-coded: every site, project and user differs.
#
#   RUNAI    path to the runai binary        (default: $PATH, else ~/.runai/bin/runai)
#   PROJECT  Run:ai project name             (required — `runai project list`)
#   PVC      PVC claim name                  (required — `runai pvc list -p $PROJECT`)
#   MOUNT    mount path inside the container (required — `runai datasource describe ... -o yaml`)
#   WORKDIR  directory to work in            (default: $MOUNT)
#   IMAGE    container image                 (default: the SIH interactive terminal base image)
#
# Exit status is 0 when the workload completed, 1 when it failed or timed out.
set -euo pipefail

APOLLO_ENV=${APOLLO_ENV:-./cluster.env}
# shellcheck disable=SC1090
[ -f "$APOLLO_ENV" ] && . "$APOLLO_ENV"

RUNAI=${RUNAI:-$(command -v runai || echo "$HOME/.runai/bin/runai")}
IMAGE=${IMAGE:-sydneyinformaticshub/dgx-interactive-terminal}
WORKDIR=${WORKDIR:-${MOUNT:-}}
TIMEOUT_S=${TIMEOUT_S:-600}

die() { echo "apollo_run: $*" >&2; exit 2; }
[ $# -ge 1 ] || die "usage: $0 '<shell command; \$WORKDIR and \$MOUNT are set>'"
[ -x "$RUNAI" ] || command -v "$RUNAI" >/dev/null 2>&1 || die "runai CLI not found at '$RUNAI' — set RUNAI"
for v in PROJECT PVC MOUNT; do
  [ -n "${!v:-}" ] || die "$v is not set. Run discovery first and write cluster.env (see references/APOLLO_AGENT_GUIDE.md §3)"
done

"$RUNAI" whoami >/dev/null 2>&1 || die "not authenticated. Ask the user to run 'runai login' — this step is theirs, not yours"

CMD="$1"
NAME=${NAME:-peek-$(date +%H%M%S)-$RANDOM}

cleanup() { "$RUNAI" training standard delete "$NAME" -p "$PROJECT" >/dev/null 2>&1 || true; }
trap cleanup EXIT

"$RUNAI" training submit "$NAME" -p "$PROJECT" \
  -i "$IMAGE" \
  --cpu-core-request 1 --cpu-memory-request 2G --run-as-user \
  --existing-pvc "claimname=$PVC,path=$MOUNT" \
  --command -- bash -c "export MOUNT='$MOUNT' WORKDIR='$WORKDIR'; cd \"\$WORKDIR\" 2>/dev/null || true; $CMD" >/dev/null

phase=""
deadline=$(( $(date +%s) + TIMEOUT_S ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  phase=$("$RUNAI" training standard describe "$NAME" -p "$PROJECT" 2>/dev/null \
          | awk -F: '/^Phase:/{gsub(/ /,"",$2);print $2}') || true
  case "$phase" in Completed|Succeeded|Failed|Stopped) break;; esac
  sleep 5
done

"$RUNAI" training standard logs "$NAME" -p "$PROJECT" 2>&1 || true

case "$phase" in
  Completed|Succeeded) exit 0 ;;
  "") echo "apollo_run: timed out after ${TIMEOUT_S}s waiting for the workload; a first image pull can take several minutes" >&2; exit 1 ;;
  *)  echo "apollo_run: workload phase: $phase" >&2; exit 1 ;;
esac
