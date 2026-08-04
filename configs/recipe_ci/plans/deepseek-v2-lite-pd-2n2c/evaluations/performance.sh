#!/usr/bin/env bash
set -euo pipefail

cd "$RECIPE_ARTIFACT_DIR"

"${RECIPE_AISBENCH_BIN:-ais_bench}" \
    --config-dir "${RECIPE_AISBENCH_CONFIG_DIR:-$RECIPE_PLAN_DIR/aisbench}" \
    --models "${RECIPE_AISBENCH_PERFORMANCE_MODEL_CONFIG:-vllm_api_stream_chat}" \
    --datasets gsm8k_gen_0_shot_cot_str_perf \
    --mode perf \
    --summarizer default_perf \
    --num-prompts "${RECIPE_AISBENCH_PERFORMANCE_NUM_PROMPTS:-4}" \
    --debug
