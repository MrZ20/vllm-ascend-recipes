#!/usr/bin/env bash
set -euo pipefail

exec python3 "$RECIPE_REPOSITORY_ROOT/scripts/recipe_ci/aisbench.py" run \
    --config "$RECIPE_STEP_INPUT_FILE"
