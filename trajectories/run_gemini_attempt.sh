#!/usr/bin/env bash
# Why this exists: README section 5 makes claims about how coding agents fail on
# POLARIS-SWE-042. This script turns a claim into evidence. It hands a Gemini-
# powered agent (Google's Antigravity CLI, `agy`, the successor to Gemini CLI) a
# fresh copy of workspace/ plus the verbatim prompt.md, lets it work until it says
# it is done (bounded by a timeout), then records the agent's diff and the
# grader's JSON report under trajectories/ as gemini_attempt_<N>.diff and
# gemini_attempt_<N>_score.json, plus a small _meta.json describing the run.
#
# Usage: trajectories/run_gemini_attempt.sh <attempt-number> [model]
# Env:   GEMINI_MODEL           model override when no [model] argument is given
#        POLARIS_AGENT_TIMEOUT  wall-clock bound for the agent (default: 20m)
#        POLARIS_PY_BIN         bin dir of a Python 3.11 + pytest, put first on PATH
#                               so the agent tests against the grader's Python
#        POLARIS_RUN_ROOT       where scratch run directories are created
#
# The agent runs with all tool calls auto-approved, so run this in a directory
# tree you are happy to let an autonomous agent edit and execute commands in.
set -euo pipefail

ATTEMPT_NUM=$((10#${1:?usage: $0 <attempt-number> [model]}))
GEMINI_MODEL="${2:-${GEMINI_MODEL:-}}"
ATTEMPT_ID=$(printf '%02d' "$ATTEMPT_NUM")
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_ROOT="${POLARIS_RUN_ROOT:-$HOME/code/personal/polaris-trajectory-runs}"
RUN_DIR="$RUN_ROOT/gemini_attempt_${ATTEMPT_ID}"
OUT="$REPO/trajectories/gemini_attempt_${ATTEMPT_ID}"
TIMEOUT="${POLARIS_AGENT_TIMEOUT:-20m}"
PY_BIN="${POLARIS_PY_BIN:-$HOME/miniconda3/envs/polaris-py311/bin}"
GIT="git -c user.name=polaris-harness -c user.email=harness@polaris.invalid"

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

snapshot_workspace() {
  rm -rf "$RUN_DIR"
  mkdir -p "$RUN_DIR"
  cp -r "$REPO/workspace" "$RUN_DIR/workspace"
  cp "$REPO/prompt.md" "$RUN_DIR/prompt.md"
  $GIT -C "$RUN_DIR/workspace" init -q
  printf '__pycache__/\n.pytest_cache/\n*.pyc\n' > "$RUN_DIR/workspace/.git/info/exclude"
  $GIT -C "$RUN_DIR/workspace" add -A
  $GIT -C "$RUN_DIR/workspace" commit -q -m "unpatched baseline"
  log "fresh workspace at $RUN_DIR/workspace"
}

run_agent() {
  log "agent starting (timeout $TIMEOUT, model ${GEMINI_MODEL:-default})"
  local start end
  start=$(date +%s)
  set +e
  (
    cd "$RUN_DIR/workspace"
    PATH="$PY_BIN:$PATH" timeout "$TIMEOUT" agy --dangerously-skip-permissions \
      ${GEMINI_MODEL:+--model "$GEMINI_MODEL"} \
      --output-format stream-json --print-timeout "$TIMEOUT" \
      -p "$(cat "$RUN_DIR/prompt.md")" < /dev/null
  ) > "$RUN_DIR/agent_events.jsonl" 2> "$RUN_DIR/agent_stderr.log"
  AGENT_EXIT=$?
  set -e
  end=$(date +%s)
  AGENT_SECONDS=$((end - start))
  log "agent finished: exit=$AGENT_EXIT after ${AGENT_SECONDS}s"
}

capture_diff() {
  $GIT -C "$RUN_DIR/workspace" add -A
  $GIT -C "$RUN_DIR/workspace" diff --cached > "${OUT}.diff"
  log "diff: ${OUT}.diff ($(wc -l < "${OUT}.diff") lines)"
}

grade() {
  docker build -q -t polaris-chronos-eval "$REPO" > /dev/null
  docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$RUN_DIR/workspace:/eval/workspace" polaris-chronos-eval > "${OUT}_score.json"
  log "score: $(grep '"final_score"' "${OUT}_score.json" | tr -d ' ,')"
}

write_meta() {
  local model
  model=$(grep -o '"model":"[^"]*"' "$RUN_DIR/agent_events.jsonl" | head -1 | cut -d'"' -f4)
  cat > "${OUT}_meta.json" <<JSON
{
  "attempt": ${ATTEMPT_NUM},
  "agent": "antigravity-cli $(agy --version 2>/dev/null | tail -1)",
  "model": "${GEMINI_MODEL:-${model:-cli default}}",
  "agent_python": "$("$PY_BIN/python" --version 2>&1)",
  "started_utc": "$(date -u -d "@${START_EPOCH}" +%Y-%m-%dT%H:%M:%SZ)",
  "agent_wall_seconds": ${AGENT_SECONDS},
  "agent_exit_code": ${AGENT_EXIT},
  "agent_timeout": "${TIMEOUT}",
  "prompt": "prompt.md (verbatim)",
  "approval_mode": "dangerously-skip-permissions (all tool calls auto-approved)",
  "grader": "docker polaris-chronos-eval, agent workspace mounted at /eval/workspace"
}
JSON
  log "meta: ${OUT}_meta.json"
}

START_EPOCH=$(date +%s)
snapshot_workspace
run_agent
capture_diff
grade
write_meta
