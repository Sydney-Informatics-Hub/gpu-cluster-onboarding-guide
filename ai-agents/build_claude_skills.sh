#!/usr/bin/env bash
# build_claude_skills.sh — assemble the Claude skills from the sources in this directory.
#
# The guides and the scripts are each kept in exactly one place, because they are the primary
# artefacts: any agent, or a person, can use them without Claude. A Claude skill expects its
# runbook under references/ and its scripts under scripts/, so they are copied in at build time
# rather than committed twice and left to drift.
#
#   ./build_claude_skills.sh                 # every skill under skills/
#   ./build_claude_skills.sh gpu-efficiency  # one skill
#
# Produces, for each skill (git-ignored — they are build output, the sources are what gets
# reviewed):
#
#   dist/<name>/        the skill folder — copy this into ~/.claude/skills/
#   dist/<name>.skill   the same folder zipped, for apps that take a skill upload
#
# Each skill's directory under skills/ must match the `name:` field in its SKILL.md: Claude
# identifies a skill by its directory name. Set DIST to build somewhere else (the tests do).
set -euo pipefail

cd "$(dirname "$0")"
DIST=${DIST:-dist}

# What each bundle carries besides its SKILL.md. Guides go to references/, scripts to scripts/.
# Keep these lists in step with the scripts each SKILL.md and guide tells the agent to run.
guides_for() {
  case "$1" in
    apollo-gpu)     echo APOLLO_AGENT_GUIDE.md ;;
    gpu-efficiency) echo GPU_EFFICIENCY_GUIDE.md ;;
    *) return 1 ;;
  esac
}
scripts_for() {
  case "$1" in
    apollo-gpu)     echo apollo_run.sh ;;
    gpu-efficiency) echo ;;
    *) return 1 ;;
  esac
}

die() { echo "build_claude_skills: $*" >&2; exit 1; }

build_one() {
  local skill=$1 name out f
  [ -f "skills/$skill/SKILL.md" ] || die "no skills/$skill/SKILL.md"
  name=$(awk -F': *' '/^name:/{print $2; exit}' "skills/$skill/SKILL.md")
  [ "$name" = "$skill" ] || die "skills/$skill/SKILL.md has 'name: $name'; it must match the directory"
  guides_for "$skill" >/dev/null || die "no file list for '$skill' — add it to guides_for and scripts_for"

  out="$DIST/$name"
  rm -rf "$out" "$DIST/$name.skill"
  mkdir -p "$out/references" "$out/scripts"
  cp "skills/$skill/SKILL.md" "$out/SKILL.md"
  for f in $(guides_for "$skill"); do
    [ -f "$f" ] || die "$skill: missing guide $f"
    cp "$f" "$out/references/$f"
  done
  for f in $(scripts_for "$skill"); do
    [ -f "scripts/$f" ] || die "$skill: missing script scripts/$f"
    cp -p "scripts/$f" "$out/scripts/$f"
  done
  rmdir "$out/scripts" 2>/dev/null || true
  find "$out" -name '.DS_Store' -delete

  ( cd "$DIST" && zip -r -q "$name.skill" "$name" -x '*.DS_Store' )
  echo "$out"
  echo "$DIST/$name.skill"
}

mkdir -p "$DIST"
if [ $# -gt 0 ]; then
  for s in "$@"; do build_one "$s"; done
else
  for d in skills/*/; do build_one "$(basename "$d")"; done
fi
