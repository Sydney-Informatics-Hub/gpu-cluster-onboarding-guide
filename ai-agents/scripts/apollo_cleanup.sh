#!/usr/bin/env bash
# apollo_cleanup.sh — find, and with --yes delete, your own finished throwaway Apollo jobs.
#
# apollo_run.sh and apollo_script.sh delete their workload when they finish, but an interrupted
# session can leave one behind. Their names start with <prefix>-<tag>-, where the tag is derived
# from your Run:ai user ID, so on a shared project this lists only jobs you submitted.
#
#   apollo_cleanup.sh                     # dry run: list your peek-* and script-* trainings
#   apollo_cleanup.sh --yes               # delete those that have finished
#   apollo_cleanup.sh --prefix probe      # other prefixes (repeatable; replaces the defaults)
#   apollo_cleanup.sh --all-tags          # any peek-*/script-* job, other members' too; list only
#
# Only finished jobs (Completed, Succeeded, Failed, Stopped, Error) are deleted; Running,
# Pending and Initializing ones are listed and never touched. --all-tags never deletes, even
# with --yes, because other members' jobs match too. Your own suspended workspaces (by
# `submittedBy` in `runai workspace describe`) are listed as a reminder, with the command to
# delete one, and never deleted here; other members' are only counted.
#
# Reads `runai training list --json` and `runai workspace list --json`. Their field names are
# not documented, so they are read tolerantly (a list, or an object holding it under
# workloads/items/data; name from name/workloadName/Name, phase from phase/status/Phase/Status,
# age from createdAt/creationTimestamp/submittedAt). If that fails, the raw output is printed.
#
# Configuration: APOLLO_ENV, RUNAI and PROJECT as for apollo_run.sh; APOLLO_TAG overrides the
# tag; RETRY_S is the pause before retrying a failed delete (default 2).
#
# Exit status: 0, including when a delete failed (the job is named on stderr); 2 on a setup or
# connection problem, or when the CLI's JSON could not be parsed.
set -euo pipefail

# shellcheck source=_apollo_lib.sh
. "$(cd "$(dirname "$0")" && pwd)/_apollo_lib.sh"

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; }

prefixes=""
all_tags=0
yes=0
while [ $# -gt 0 ]; do
  case $1 in
    --prefix)
      [ $# -ge 2 ] || apollo_die "--prefix needs a value"
      prefixes="$prefixes $2"
      shift 2 ;;
    --all-tags) all_tags=1; shift ;;
    --yes) yes=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) apollo_die "unknown argument $1 (see --help)" ;;
  esac
done
prefixes=${prefixes:-peek script}

apollo_load_env PROJECT
apollo_init
apollo_check_conn
tag=$(apollo_tag)

if ! "$RUNAI" training list -p "$PROJECT" --json >"$APOLLO_TMP/trainings.json" 2>"$APOLLO_TMP/err"; then
  _apollo_conn_error "$(cat "$APOLLO_TMP/err")"
  cat "$APOLLO_TMP/err" >&2
  apollo_die "'runai training list' failed (output above)"
fi
wfile=$APOLLO_TMP/workspaces.json
if ! "$RUNAI" workspace list -p "$PROJECT" --json >"$wfile" 2>"$APOLLO_TMP/err"; then
  apollo_say "warning: 'runai workspace list' failed, so suspended workspaces are not shown:"
  cat "$APOLLO_TMP/err" >&2
  wfile=""
fi

# shellcheck disable=SC2086  # one argument per prefix
if ! apollo_py cleanup "$APOLLO_TMP/trainings.json" "$wfile" "$tag" "$all_tags" $prefixes \
  >"$APOLLO_TMP/rows"; then
  cat "$APOLLO_TMP/rows"   # the raw CLI output, when it could not be parsed
  exit 2
fi

fmt='%-50s %-12s %-7s %s\n'
n_rows=0
n_delete=0
: >"$APOLLO_TMP/delete"
while IFS=$'\t' read -r kind name phase age action; do
  [ "$kind" = T ] || continue
  if [ "$n_rows" = 0 ]; then
    # shellcheck disable=SC2059  # the format is ours
    printf "$fmt" NAME PHASE AGE ACTION
  fi
  n_rows=$((n_rows + 1))
  case $action in
    delete)
      n_delete=$((n_delete + 1))
      echo "$name" >>"$APOLLO_TMP/delete"
      if [ "$yes" = 1 ]; then action="delete"; else action="would delete"; fi ;;
    keep) action="keep (not finished)" ;;
    list) action="list only (--all-tags)" ;;
  esac
  # shellcheck disable=SC2059
  printf "$fmt" "$name" "$phase" "$age" "$action"
done <"$APOLLO_TMP/rows"

if [ "$n_rows" = 0 ]; then
  # shellcheck disable=SC2086
  shown=$(printf '%s-* ' $prefixes)
  if [ "$all_tags" = 1 ]; then
    echo "No trainings named ${shown% } in project $PROJECT."
  else
    echo "No throwaway trainings of yours in project $PROJECT (${shown% }; tag $tag)."
  fi
elif [ "$all_tags" = 1 ]; then
  echo
  echo "--all-tags: other members' jobs may be listed, so nothing is deleted in this mode."
elif [ "$n_delete" = 0 ]; then
  echo
  echo "Nothing to delete: none of these has finished."
elif [ "$yes" = 1 ]; then
  ok=0
  while IFS= read -r name; do
    if apollo_delete "$name"; then ok=$((ok + 1)); fi
  done <"$APOLLO_TMP/delete"
  echo
  echo "Deleted $ok of $n_delete finished workload(s)."
else
  echo
  echo "Dry run: nothing deleted. Re-run with --yes to delete the $n_delete marked 'would delete'."
fi

# Suspended workspaces: the list does not say who submitted them, so describe each (up to 30)
# and show only the caller's. A delete command for someone else's workspace on a shared project
# is the last thing to hand an agent.
if grep -q '^W' "$APOLLO_TMP/rows"; then
  me=""
  if [[ $APOLLO_WHOAMI =~ User\ ([^[:space:]]+)\ logged ]]; then me=${BASH_REMATCH[1]}; fi
  mine="" others=0 unknown=0 n=0
  while IFS=$'\t' read -r kind name phase age _; do
    [ "$kind" = W ] || continue
    n=$((n + 1))
    if [ "$n" -gt 30 ]; then unknown=$((unknown + 1)); continue; fi
    owner=$("$RUNAI" workspace describe "$name" -p "$PROJECT" -o json 2>/dev/null |
      python3 -c 'import json,sys; print((json.load(sys.stdin).get("general") or {}).get("submittedBy") or "")' \
      2>/dev/null) || owner=""
    if [ -z "$owner" ] || [ -z "$me" ]; then
      unknown=$((unknown + 1))
    elif [ "$owner" = "$me" ]; then
      mine="$mine$(printf '  %-40s %-10s %-7s runai workspace delete %s -p %s' \
        "$name" "$phase" "$age" "$name" "$PROJECT")"$'\n'
    else
      others=$((others + 1))
    fi
  done <"$APOLLO_TMP/rows"
  echo
  if [ -n "$mine" ]; then
    echo "Your suspended workspaces in project $PROJECT (never deleted here). Deleting one destroys"
    echo "everything in it that is not on the PVC; if one is no longer needed, the user can run:"
    printf '%s' "$mine"
  else
    echo "No suspended workspaces of yours in project $PROJECT."
  fi
  [ "$others" = 0 ] || echo "($others suspended workspace(s) belong to other members; not shown.)"
  [ "$unknown" = 0 ] || echo "($unknown suspended workspace(s) whose owner could not be read; not shown.)"
fi
exit 0
