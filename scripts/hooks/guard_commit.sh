#!/usr/bin/env bash
# PreToolUse(Bash) guard: a `git commit` touching the anychain repo is denied unless the
# full test suite (including the golden answers) passes. Reads the hook JSON on stdin.
REPO="${ANYCHAIN_REPO:-$HOME/Projetos/anychain}"
input=$(cat)
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // ""')
cwd=$(printf '%s' "$input" | jq -r '.cwd // ""')
case "$cmd" in *"git commit"*) ;; *) exit 0 ;; esac
case "$cwd$cmd" in *"$REPO"*|*"anychain"*) ;; *) exit 0 ;; esac
if out=$(cd "$REPO" && uv run pytest -q 2>&1); then
  exit 0
fi
tail=$(printf '%s' "$out" | tail -15)
jq -n --arg r "Commit blocked: the anychain test suite fails. Fix it before committing.
$tail" '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: $r}}'
