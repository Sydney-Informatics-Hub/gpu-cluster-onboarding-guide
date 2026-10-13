#!/usr/bin/env bash
# apollo_run.sh — run one shell command inside a throwaway Apollo workload and print its output.
#
# Deliberately CPU-only: it never requests a GPU, so it costs nothing against a project's
# allocation and is safe to run without asking. Use it to inspect cluster storage, check paths,
# verify a stage completed, or run any short non-GPU command. The workload is deleted afterwards.
#
#   apollo_run.sh 'ls -la $WORKDIR'
#   apollo_run.sh 'du -sh $WORKDIR/*'
#   apollo_run.sh 'python -c "import sys; print(sys.version)"'
#   KEEP=1 apollo_run.sh 'nproc; free -g'      # leave the workload for `runai ... describe`
#
# Quote the command once, for your local shell; it reaches the pod byte for byte (it travels
# base64-encoded), runs with bash in $WORKDIR, and sees $MOUNT and $WORKDIR. Several arguments
# are joined with spaces, as ssh does. For a multi-line script use apollo_script.sh; for code
# bigger than about 6,500 characters, put it on the PVC first and run it from there.
#
# Configuration comes from the environment, or from a cluster.env file (see $APOLLO_ENV below),
# written during discovery. Nothing is hard-coded: every site, project and user differs. A value
# already set in the environment wins over the file.
#
#   APOLLO_ENV  file to read first                (default: ./cluster.env)
#   RUNAI       path to the runai binary          (default: $PATH, else ~/.runai/bin/runai)
#   PROJECT     Run:ai project name               (required — `runai project list`)
#   PVC         PVC claim name                    (required — `runai pvc list -p $PROJECT`)
#   MOUNT       mount path inside the container   (required — `runai datasource describe ... -o yaml`)
#   WORKDIR     directory to work in              (default: $MOUNT)
#   IMAGE       container image                   (default: the SIH interactive terminal base image)
#   NAME        workload name                     (default: peek-<tag>-<time>-<pid>-<random>)
#   KEEP=1      do not delete the workload afterwards
#   TIMEOUT_S   seconds to wait for it to finish  (default: 900)
#   CPU, MEM, LARGE_SHM, EXCLUDE_NODE, AUTO_DELETE, STUCK_S, POLL_S: see _apollo_lib.sh
#
# Exit status: 0 when the command succeeded; the command's own exit status when it failed;
# 1 when the workload failed or timed out without reporting one (the phase is printed);
# 2 on a setup or connection problem (configuration, login, VPN, a command that is too big).
set -euo pipefail

# shellcheck source=_apollo_lib.sh
. "$(cd "$(dirname "$0")" && pwd)/_apollo_lib.sh"

case ${1:-} in
  -h | --help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
esac
[ $# -ge 1 ] || apollo_die "usage: $0 '<shell command; \$WORKDIR and \$MOUNT are set>'"

apollo_load_env
apollo_init
apollo_check_conn

NAME=${NAME:-$(apollo_name peek)}
printf '%s\n' "$*" >"$APOLLO_TMP/command.sh"
apollo_job "$NAME" 0 "$APOLLO_TMP/command.sh"
exit "$APOLLO_RC"
