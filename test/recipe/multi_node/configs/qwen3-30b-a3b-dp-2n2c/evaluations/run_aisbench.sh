#!/usr/bin/env bash
set -euo pipefail

exec python3 "$MULTI_NODE_REPOSITORY_ROOT/test/recipe/multi_node/scripts/aisbench.py" \
    --config "$MULTI_NODE_STEP_INPUT_FILE"
