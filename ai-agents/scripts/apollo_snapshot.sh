#!/usr/bin/env bash
# apollo_snapshot.sh — record what is running in a project before planned maintenance, and
# afterwards show what finished, failed, vanished or was interrupted.
#
#   apollo_snapshot.sh save                       # every training in the project (up to 50)
#   apollo_snapshot.sh save --dir before-outage train-a train-b
#   apollo_snapshot.sh compare apollo_snapshots/20261014-093000
#
# `save` writes to D (default ./apollo_snapshots/<YYYYmmdd-HHMMSS>/):
#   training_list.json    runai training list -p $PROJECT --json
#   workspace_list.json   runai workspace list -p $PROJECT --json
#   describe/<name>.json  runai training standard describe <name> -o json, with every pod and
#                         event (node names, restarts), for each NAME given, or for every listed
#                         training when none is given (at most 50, with a warning)
#   meta.txt              project and time of the snapshot
# It only reads, and submits nothing, so it is free and safe to run without asking.
#
# `compare DIR` lists the project again and prints, for every workload in the snapshot, its
# phase then and now and a class: finished (now Completed/Succeeded), failed, missing (gone
# from the list), still-running, still-pending (queued then and now), interrupted (was Running
# or Pending, now neither running nor finished), or unchanged. It ends with the resubmit
# candidates. A phase is not proof: only a job's own done-marker on the PVC shows that a run
# finished (APOLLO_AGENT_GUIDE.md §6.8).
#
# The list JSON is read tolerantly (see apollo_cleanup.sh); if it cannot be parsed, the raw
# output is printed with the assumption that failed.
#
# Configuration: APOLLO_ENV, RUNAI and PROJECT as for apollo_run.sh.
#
# Exit status: 0 on success (a single failed describe is only a warning); 2 on a usage, setup or
# connection problem, or when the CLI's JSON could not be parsed.
set -euo pipefail

# shellcheck source=_apollo_lib.sh
. "$(cd "$(dirname "$0")" && pwd)/_apollo_lib.sh"

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; }
MAX_DESCRIBE=50

# list KIND FILE: `runai KIND list --json` into FILE. Returns 1 (after saying why) on failure.
list() {
  if "$RUNAI" "$1" list -p "$PROJECT" --json >"$2" 2>"$APOLLO_TMP/err"; then return 0; fi
  _apollo_conn_error "$(cat "$APOLLO_TMP/err")"
  cat "$APOLLO_TMP/err" >&2
  rm -f "$2"
  return 1
}

save() {
  local dir="" n=0 described=0 name total
  while [ $# -gt 0 ]; do
    case $1 in
      --dir) [ $# -ge 2 ] || apollo_die "--dir needs a value"; dir=$2; shift 2 ;;
      -h | --help) usage; exit 0 ;;
      -*) apollo_die "unknown option $1 (see --help)" ;;
      *) break ;;
    esac
  done
  dir=${dir:-./apollo_snapshots/$(date +%Y%m%d-%H%M%S)}
  mkdir -p "$dir/describe" || apollo_die "cannot create $dir"

  list training "$dir/training_list.json" || apollo_die "'runai training list' failed (output above)"
  list workspace "$dir/workspace_list.json" ||
    apollo_say "warning: 'runai workspace list' failed (output above); workspaces are not in this snapshot"
  printf 'project=%s\nsaved=%s\n' "$PROJECT" "$(date '+%Y-%m-%d %H:%M:%S %z')" >"$dir/meta.txt"

  if [ $# -gt 0 ]; then
    printf '%s\n' "$@" >"$APOLLO_TMP/names"
  else
    if ! apollo_py names "$dir/training_list.json" >"$APOLLO_TMP/names"; then
      cat "$APOLLO_TMP/names"   # the raw CLI output, when it could not be parsed
      apollo_die "the lists are saved in $dir, but no workload was described"
    fi
    total=$(wc -l <"$APOLLO_TMP/names" | tr -d ' ')
    if [ "$total" -gt "$MAX_DESCRIBE" ]; then
      apollo_say "warning: $total trainings listed; describing the first $MAX_DESCRIBE only." \
        "Name the ones that matter: $0 save NAME..."
      head -n "$MAX_DESCRIBE" "$APOLLO_TMP/names" >"$APOLLO_TMP/names.cut"
      mv "$APOLLO_TMP/names.cut" "$APOLLO_TMP/names"
    fi
  fi

  while IFS= read -r name; do
    [ -n "$name" ] || continue
    n=$((n + 1))
    if "$RUNAI" training standard describe "$name" -p "$PROJECT" -o json \
      --pod-limit=-1 --event-limit=-1 >"$dir/describe/${name//\//_}.json" 2>"$APOLLO_TMP/err"; then
      described=$((described + 1))
    else
      rm -f "$dir/describe/${name//\//_}.json"
      cp "$APOLLO_TMP/err" "$dir/describe/${name//\//_}.err"
      apollo_say "warning: could not describe $name: $(head -n 1 "$APOLLO_TMP/err")"
    fi
  done <"$APOLLO_TMP/names"

  echo "Saved a snapshot of project $PROJECT to $dir ($described of $n trainings described)."
  echo "After the maintenance: $0 compare $dir"
}

compare() {
  local dir=${1:-} snap_project wfile
  [ $# -eq 1 ] || apollo_die "usage: $0 compare DIR"
  [ -f "$dir/training_list.json" ] ||
    apollo_die "$dir has no training_list.json; is it a directory written by '$0 save'?"
  snap_project=$(sed -n 's/^project=//p' "$dir/meta.txt" 2>/dev/null || true)
  if [ -n "$snap_project" ] && [ "$snap_project" != "$PROJECT" ]; then
    apollo_say "warning: the snapshot is of project $snap_project, but PROJECT is $PROJECT"
  fi

  list training "$APOLLO_TMP/trainings.json" || apollo_die "'runai training list' failed (output above)"
  wfile=$APOLLO_TMP/workspaces.json
  if ! list workspace "$wfile"; then
    apollo_say "warning: 'runai workspace list' failed (output above); workspaces are not compared"
    wfile=""
  fi
  [ -f "$dir/workspace_list.json" ] || wfile=""

  apollo_py compare "$dir" "$APOLLO_TMP/trainings.json" "$wfile" || exit 2
  echo
  echo "Only the job's own done-marker on the PVC proves a run finished (APOLLO_AGENT_GUIDE.md §6.8)."
  echo "Check it before resubmitting anything, and before reporting a 'finished' job as done."
  echo "Details from before the maintenance (nodes, events): $dir/describe/"
}

cmd=${1:-}
[ $# -eq 0 ] || shift
case $cmd in
  save | compare) ;;
  -h | --help) usage; exit 0 ;;
  *) apollo_die "usage: $0 save [--dir D] [NAME...] | $0 compare DIR (see --help)" ;;
esac

apollo_load_env PROJECT
apollo_init
apollo_check_conn
"$cmd" "$@"
