#!/usr/bin/env bash
set -euo pipefail

response=$(curl --fail --silent --show-error \
    "$MULTI_NODE_ENDPOINT/v1/completions" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MULTI_NODE_SERVED_MODEL_NAME\",\"prompt\":\"The future of AI is\",\"max_tokens\":50,\"temperature\":0}")

python3 -c 'import json, sys; assert json.load(sys.stdin)["choices"]' <<<"$response"
printf '%s\n' '{"status":"passed"}' > "$MULTI_NODE_STEP_RESULT_FILE"
