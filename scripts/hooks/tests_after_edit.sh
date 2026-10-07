#!/usr/bin/env bash
# PostToolUse(Edit|Write) check: after a change under anychain's src/, tests/ or configs/,
# run the test suite and feed failures back so they are fixed before anything else.
REPO="${ANYCHAIN_REPO:-$HOME/Projetos/anychain}"
file=$(jq -r '.tool_input.file_path // .tool_response.filePath // ""')
case "$file" in "$REPO"/src/*|"$REPO"/tests/*|"$REPO"/configs/*) ;; *) exit 0 ;; esac
if out=$(cd "$REPO" && uv run pytest -q 2>&1); then
  exit 0
fi
tail=$(printf '%s' "$out" | tail -15)
jq -n --arg r "anychain tests fail after editing $file:
$tail" '{decision: "block", reason: $r}'
