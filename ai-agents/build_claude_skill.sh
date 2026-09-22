#!/usr/bin/env bash
# build_claude_skill.sh — assemble the Claude skill from the sources in this directory.
#
# The runbook is kept in exactly one place, ./APOLLO_AGENT_GUIDE.md, because it is the primary
# artefact and every agent reads it. Claude expects it inside the skill at references/, so it is
# copied in at build time rather than committed twice and left to drift.
#
#   ./build_claude_skill.sh
#
# Produces (both git-ignored — they are build output, the sources are what gets reviewed):
#
#   dist/apollo-gpu/        the skill folder — copy this into ~/.claude/skills/
#   dist/apollo-gpu.skill   the same folder zipped, for apps that take a skill upload
#
# The output is named apollo-gpu, not claude-skill: Claude identifies a skill by its directory
# name, which has to match the `name:` field in SKILL.md.
set -euo pipefail

cd "$(dirname "$0")"

NAME=$(awk -F': *' '/^name:/{print $2; exit}' claude-skill/SKILL.md)
[ -n "$NAME" ] || { echo "build_claude_skill: no 'name:' field in claude-skill/SKILL.md" >&2; exit 1; }

OUT="dist/$NAME"
rm -rf "$OUT" "dist/$NAME.skill"
mkdir -p "$OUT/references"

cp claude-skill/SKILL.md "$OUT/SKILL.md"
cp -R claude-skill/scripts "$OUT/scripts"
cp APOLLO_AGENT_GUIDE.md "$OUT/references/APOLLO_AGENT_GUIDE.md"
find "$OUT" -name '.DS_Store' -delete

( cd dist && zip -r -q "$NAME.skill" "$NAME" -x '*.DS_Store' )

echo "$OUT"
echo "dist/$NAME.skill"
