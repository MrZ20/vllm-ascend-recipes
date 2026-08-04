#!/usr/bin/env bash
set -euo pipefail

cd "$RECIPE_ARTIFACT_DIR"

ais_bench \
    --models "${RECIPE_AISBENCH_ACCURACY_MODEL_CONFIG:-vllm_api_general_chat}" \
    --datasets gsm8k_gen_0_shot_cot_chat_prompt \
    --mode all \
    --dump-eval-details \
    --debug
