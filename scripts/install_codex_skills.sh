#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
CODEX_SKILLS_DIR="${CODEX_SKILLS_DIR:-$CODEX_HOME/skills}"
VALIDATOR="$CODEX_HOME/skills/.system/skill-creator/scripts/quick_validate.py"

for skill_dir in skills/*; do
  [[ -d "$skill_dir" ]] || continue
  skill_name="$(basename "$skill_dir")"
  mkdir -p "$CODEX_SKILLS_DIR/$skill_name"
  cp -R "$skill_dir/." "$CODEX_SKILLS_DIR/$skill_name/"
  python "$VALIDATOR" "$CODEX_SKILLS_DIR/$skill_name"
done
