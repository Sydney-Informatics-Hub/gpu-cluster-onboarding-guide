#!/usr/bin/env bash
# pvc_sync_code.sh — copy code and config files into a RUNNING workspace, without a TTY.
#
# For small text files (scripts, configs), into the project's own directory. Data goes by
# Globus or the copyparty route instead. The files are packed into a tar.gz and sent as base64
# through plain `runai workspace exec` calls, in chunks that fit Run:ai's 10,000-character
# command limit. stdin is not an option: `exec -i` without a TTY fails with "inappropriate ioctl
# for device". The archive's sha256 is checked inside the workspace before anything is extracted.
#
#   pvc_sync_code.sh devbox code train.py src configs/base.yaml
#   pvc_sync_code.sh --dry-run devbox code src
#
# PATHs are relative to the current directory and keep that layout under DEST_SUBDIR, so
# `src/model.py` lands at $WORKDIR/DEST_SUBDIR/src/model.py, replacing an older copy of it.
# Nothing else is deleted. Directories are walked for allowed files only, skipping hidden files
# and directories (often secrets or caches), __pycache__, node_modules and venv. Allowed: .py
# .sh .yaml .yml .toml .txt .json .cfg. A file named explicitly with any other extension is
# refused.
#
# Configuration comes from the environment, or from cluster.env ($APOLLO_ENV, default ./cluster.env):
#
#   RUNAI           path to the runai binary  (default: $PATH, else ~/.runai/bin/runai)
#   PROJECT         Run:ai project name       (required)
#   WORKDIR         project directory inside the workspace (default: $MOUNT; one is required)
#   REMOTE_TMP      scratch directory inside the workspace (default: /tmp)
#   CHUNK_CHARS     base64 characters per exec call (default and maximum: 8000)
#   MAX_ARCHIVE_KB  refuse a compressed archive larger than this (default: 1024)
#
# Exit status: 0 copied (or dry run shown); 1 an exec failed or the checksum did not match;
# 2 usage error, refused path or file, or not logged in.
set -euo pipefail

APOLLO_ENV=${APOLLO_ENV:-./cluster.env}
# shellcheck disable=SC1090
[ -f "$APOLLO_ENV" ] && . "$APOLLO_ENV"

RUNAI=${RUNAI:-$(command -v runai || echo "$HOME/.runai/bin/runai")}
WORKDIR=${WORKDIR:-${MOUNT:-}}
REMOTE_TMP=${REMOTE_TMP:-/tmp}
CHUNK_CHARS=${CHUNK_CHARS:-8000}
MAX_ARCHIVE_KB=${MAX_ARCHIVE_KB:-1024}
COMMAND_CAP=9500   # Run:ai rejects commands over 10,000 characters; keep a margin

die()   { echo "pvc_sync_code: $*" >&2; exit 2; }
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }
# Single-quote a string for the remote bash.
sq()    { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
allowed_ext() {
  case "$(printf '%s' "${1##*/}" | tr '[:upper:]' '[:lower:]')" in
    *.py|*.sh|*.yaml|*.yml|*.toml|*.txt|*.json|*.cfg) return 0 ;;
  esac
  return 1
}
# Relative, no `..`, no newline: the same rules for DEST_SUBDIR and for each PATH.
safe_rel() {
  case "$1" in /*|*$'\n'*) return 1 ;; esac
  case "/$1/" in */../*) return 1 ;; esac
  return 0
}

DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; break ;;
    -*) die "unknown option: $1 (see --help)" ;;
    *) break ;;
  esac
done
[ $# -ge 3 ] || die "usage: $0 [--dry-run] WORKSPACE DEST_SUBDIR PATH..."
WS=$1 DEST=$2
shift 2

[ -n "${PROJECT:-}" ] || die "PROJECT is not set. Run discovery first and write cluster.env (see APOLLO_AGENT_GUIDE.md §3)"
[ -n "$WORKDIR" ] || die "WORKDIR (or MOUNT) is not set. It is the project's directory inside the workspace."
case "$CHUNK_CHARS" in ''|*[!0-9]*|0) die "CHUNK_CHARS must be a positive number" ;; esac
[ "$CHUNK_CHARS" -le 8000 ] || die "CHUNK_CHARS must be at most 8000 (Run:ai's command limit is 10,000)"
if [ -z "$DEST" ] || ! safe_rel "$DEST"; then
  die "DEST_SUBDIR must be a relative path under \$WORKDIR without '..': '$DEST'"
fi
DEST=${DEST%/}
while [ "${DEST#./}" != "$DEST" ]; do DEST=${DEST#./}; done
DEST=${DEST:-.}

TMP=$(mktemp -d "${TMPDIR:-/tmp}/pvc_sync_code.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
: > "$TMP/list"
: > "$TMP/refused"
skipped=0

add() {  # a regular file, already known to be allowed
  case "$1" in *$'\n'*|*$'\t'*) echo "$1 (tab or newline in the name)" >> "$TMP/refused"; return ;; esac
  printf '%s\0' "$1" >> "$TMP/list"
}

for p in "$@"; do
  while [ "${p#./}" != "$p" ]; do p=${p#./}; done
  if ! safe_rel "$p" || [ -z "$p" ]; then
    echo "$p (must be relative to the current directory, without '..')" >> "$TMP/refused"
  elif [ -L "$p" ]; then
    echo "$p (a symlink; name its target instead)" >> "$TMP/refused"
  elif [ -f "$p" ]; then
    if allowed_ext "$p"; then add "$p"; else echo "$p" >> "$TMP/refused"; fi
  elif [ -d "$p" ]; then
    while IFS= read -r -d '' f; do
      if allowed_ext "$f"; then add "$f"; else skipped=$((skipped + 1)); fi
    done < <(find "$p" -mindepth 1 \
               \( -type d \( -name '.?*' -o -name __pycache__ -o -name node_modules -o -name venv \) -prune \) \
               -o \( -type f ! -name '.*' -print0 \))
  else
    echo "$p (not found)" >> "$TMP/refused"
  fi
done

if [ -s "$TMP/refused" ]; then
  {
    echo "pvc_sync_code: refused — only code and config files (.py .sh .yaml .yml .toml .txt .json"
    echo "  .cfg) are copied this way; move data with Globus or the copyparty route. Offenders:"
    sed 's/^/    /' "$TMP/refused"
  } >&2
  exit 2
fi

# Dedupe (a file named both directly and through its directory) and fix the order.
LC_ALL=C sort -z -u "$TMP/list" > "$TMP/list.sorted"
NFILES=$(tr -cd '\0' < "$TMP/list.sorted" | wc -c | tr -d ' ')
[ "$NFILES" -gt 0 ] || die "no allowed files found in: $*"

TARFLAGS=()
# bsdtar (macOS) would add AppleDouble files and xattr headers that GNU tar warns about.
if tar --version 2>/dev/null | grep -q bsdtar; then
  TARFLAGS=(--no-xattrs --no-mac-metadata --no-fflags --no-acls)
fi
COPYFILE_DISABLE=1 tar ${TARFLAGS[@]+"${TARFLAGS[@]}"} -czf "$TMP/code.tgz" --null -T "$TMP/list.sorted"

if command -v sha256sum >/dev/null 2>&1; then
  SHA=$(sha256sum "$TMP/code.tgz" | cut -d' ' -f1)
else
  SHA=$(shasum -a 256 "$TMP/code.tgz" | cut -d' ' -f1)
fi
TGZ_BYTES=$(wc -c < "$TMP/code.tgz" | tr -d ' ')
[ "$TGZ_BYTES" -le $((MAX_ARCHIVE_KB * 1024)) ] || die "the archive is $((TGZ_BYTES / 1024)) KB, over MAX_ARCHIVE_KB=$MAX_ARCHIVE_KB. That is not code; use Globus or the copyparty route."
base64 < "$TMP/code.tgz" | tr -d '\n' > "$TMP/code.b64"
B64_CHARS=$(wc -c < "$TMP/code.b64" | tr -d ' ')
NCHUNKS=$(( (B64_CHARS + CHUNK_CHARS - 1) / CHUNK_CHARS ))

RTMP="${REMOTE_TMP%/}/pvc_sync_code.$$.$RANDOM"
TARGET="$WORKDIR/$DEST"
if [ "$DEST" = "." ]; then TARGET=$WORKDIR; fi

echo "pvc_sync_code: $NFILES file(s), $TGZ_BYTES bytes compressed, $NCHUNKS exec call(s) + 1"
echo "  workspace $WS (project $PROJECT) -> $TARGET"
if [ "$skipped" -gt 0 ]; then echo "  ($skipped file(s) in the directories were skipped: not code/config)"; fi
tr '\0' '\n' < "$TMP/list.sorted" | head -50 | sed 's/^/  /'
[ "$NFILES" -le 50 ] || echo "  ... and $((NFILES - 50)) more"
if [ "$DRY" = 1 ]; then
  echo "pvc_sync_code: dry run — nothing sent."
  exit 0
fi

"$RUNAI" whoami >/dev/null 2>&1 || die "not authenticated. Ask the user to run 'runai login'; this step is theirs, not yours"

# rexec WHAT SCRIPT — run SCRIPT with bash in the workspace; on failure, report and stop.
# `runai exec` exits 1 whatever the remote code was, and names it only on stderr.
rexec() {
  [ "${#2}" -le "$COMMAND_CAP" ] || die "internal: a command of ${#2} characters would exceed Run:ai's limit; shorten REMOTE_TMP or WORKDIR"
  if "$RUNAI" workspace exec "$WS" -p "$PROJECT" -- bash -c "$2" >"$TMP/exec.out" 2>"$TMP/exec.err"; then
    return 0
  fi
  local code
  code=$(sed -n 's/.*command terminated with exit code \([0-9][0-9]*\).*/\1/p' "$TMP/exec.err" | head -1)
  {
    echo "pvc_sync_code: $1 failed${code:+ (remote exit code $code)}:"
    cat "$TMP/exec.out" "$TMP/exec.err"
    if [ -z "$code" ]; then
      echo "  Is the workspace running? Check with: $RUNAI workspace list -p $PROJECT"
    fi
  } >&2
  "$RUNAI" workspace exec "$WS" -p "$PROJECT" -- rm -f "$RTMP.b64" "$RTMP.tgz" >/dev/null 2>&1 || true
  exit 1
}

i=0
while [ "$i" -lt "$NCHUNKS" ]; do
  chunk=$(dd if="$TMP/code.b64" bs="$CHUNK_CHARS" skip="$i" count=1 2>/dev/null)
  redirect=">>"
  [ "$i" -gt 0 ] || redirect=">"   # the first chunk truncates any leftover from an earlier run
  # base64 has no quote characters, so the chunk is safe inside single quotes.
  rexec "sending chunk $((i + 1))/$NCHUNKS" "printf %s '$chunk' $redirect $(sq "$RTMP.b64")"
  i=$((i + 1))
done

# The remote side is Linux; sha256sum is in coreutils (shasum is the fallback).
read -r -d '' FINISH <<'EOF' || true
set -e
trap 'rm -f "$f" "$a"' EXIT
base64 -d < "$f" > "$a"
if command -v sha256sum >/dev/null 2>&1; then got=$(sha256sum "$a" | cut -d' ' -f1)
else got=$(shasum -a 256 "$a" | cut -d' ' -f1); fi
if [ "$got" != "$want" ]; then
  echo "pvc_sync_code: checksum mismatch in the workspace (got $got, expected $want); nothing extracted" >&2
  exit 3
fi
mkdir -p "$d"
tar -xzf "$a" -C "$d"
n=$(tar -tzf "$a" | grep -vc '/$' || true)
echo "pvc_sync_code: $n file(s) extracted into $d (sha256 verified)"
EOF
rexec "unpacking" "f=$(sq "$RTMP.b64") a=$(sq "$RTMP.tgz") d=$(sq "$TARGET") want=$SHA
$FINISH"
cat "$TMP/exec.out"
