#!/usr/bin/env bash
# build_skill.sh — package a skill directory into an installable .skill bundle.
#
#   ./build_skill.sh              # packages every skill in this directory
#   ./build_skill.sh apollo-gpu   # packages one
#
# Output goes to dist/<name>.skill, which is a plain zip containing <name>/ at its root.
# dist/ is git-ignored: the source directories are the thing under review, the bundle is
# a build artefact that anyone can reproduce with this script.
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p dist

package() {
  local name=$1
  [ -f "$name/SKILL.md" ] || { echo "build_skill: $name has no SKILL.md — not a skill" >&2; return 1; }
  rm -f "dist/$name.skill"
  # -x excludes editor and macOS cruft; the bundle must contain only what the skill needs.
  zip -r -q "dist/$name.skill" "$name" -x '*.DS_Store' '*/__pycache__/*' '*.pyc'
  echo "dist/$name.skill"
}

if [ $# -gt 0 ]; then
  for name in "$@"; do package "${name%/}"; done
else
  for dir in */; do
    [ "$dir" = "dist/" ] && continue
    [ -f "$dir/SKILL.md" ] || continue
    package "${dir%/}"
  done
fi
