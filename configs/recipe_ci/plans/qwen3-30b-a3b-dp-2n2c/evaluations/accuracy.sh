#!/usr/bin/env bash
set -euo pipefail

cd "$RECIPE_ARTIFACT_DIR"

"${RECIPE_AISBENCH_BIN:-ais_bench}" \
    --config-dir "${RECIPE_AISBENCH_CONFIG_DIR:-$RECIPE_PLAN_DIR/aisbench}" \
    --models "${RECIPE_AISBENCH_ACCURACY_MODEL_CONFIG:-vllm_api_general_chat}" \
    --datasets gsm8k_gen_0_shot_cot_chat_prompt \
    --mode all \
    --num-prompts "${RECIPE_AISBENCH_ACCURACY_NUM_PROMPTS:-8}" \
    --dump-eval-details \
    --debug
