#!/usr/bin/env bash
# Run the recap agent (Claude Code, headless) in the current folder, which must hold prompt.txt
# and facts.json; the agent writes recap_draft.md. Used by daily.yml and recap-test.yml.
#
# The agent's JSON log and stderr are always printed, so a failure shows its reason.
set -uo pipefail

# Describe the token's shape (never its value) so a bad copy-paste is easy to spot.
token="${CLAUDE_CODE_OAUTH_TOKEN:-}"
echo "token check: length ${#token}; \
prefix sk-ant-oat01-: $([[ "$token" == sk-ant-oat01-* ]] && echo yes || echo NO); \
whitespace or line breaks inside: $([[ "$token" =~ [[:space:]] ]] && echo YES || echo no)"

npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION:-2.1.304}" --silent

claude -p "$(cat prompt.txt)" \
  --model "${RECAP_MODEL:?RECAP_MODEL must be set}" \
  --allowedTools Read Write \
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
if [ -f recap_draft.md ]; then
  echo "--- draft written ($(wc -w < recap_draft.md) words) ---"
else
  echo "--- no draft written ---"
fi
exit "${status}"
