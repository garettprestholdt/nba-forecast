#!/usr/bin/env bash
# Run a Claude Code agent headless in the current folder, which must hold prompt.txt and the
# agent's input files. Used by the recap (daily.yml, recap-test.yml) and triage (triage.yml).
#
#   AGENT_MODEL   model id (falls back to RECAP_MODEL)
#   AGENT_TOOLS   space-separated allowed tools (default: "Read Write")
#   AGENT_OUTPUT  file the agent is expected to write (default: recap_draft.md)
#
# Shell, edit, and web tools are always disallowed. The agent's JSON log and stderr are always
# printed, so a failure shows its reason.
set -uo pipefail

model="${AGENT_MODEL:-${RECAP_MODEL:-}}"
output="${AGENT_OUTPUT:-recap_draft.md}"
read -r -a tools <<< "${AGENT_TOOLS:-Read Write}"
if [ -z "$model" ]; then
  echo "AGENT_MODEL (or RECAP_MODEL) must be set" >&2
  exit 2
fi

# Describe the token's shape (never its value) so a bad copy-paste is easy to spot.
token="${CLAUDE_CODE_OAUTH_TOKEN:-}"
echo "token check: length ${#token}; \
prefix sk-ant-oat01-: $([[ "$token" == sk-ant-oat01-* ]] && echo yes || echo NO); \
whitespace or line breaks inside: $([[ "$token" =~ [[:space:]] ]] && echo YES || echo no)"
echo "model: ${model}; tools: ${tools[*]}; expected output: ${output}"

npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION:-2.1.304}" --silent

claude -p "$(cat prompt.txt)" \
  --model "$model" \
  --allowedTools "${tools[@]}" \
  --disallowedTools Bash Edit WebFetch WebSearch \
  --output-format json > agent_log.json 2> agent_stderr.txt
status=$?

echo "claude exit code: ${status}"
echo "--- agent log (JSON) ---"
head -c 4000 agent_log.json
echo
echo "--- agent stderr ---"
head -c 2000 agent_stderr.txt
echo
if [ -f "$output" ]; then
  echo "--- ${output} written ($(wc -w < "$output") words) ---"
else
  echo "--- ${output} not written ---"
fi
exit "${status}"
