#!/usr/bin/env bash
set -euo pipefail

runtime_config_dir=$RECIPE_STEP_ARTIFACT_DIR/aisbench-config
model_config=vllm_api_stream_chat

source "$RECIPE_PLAN_DIR/evaluations/prepare_gsm8k.sh"
python3 "$RECIPE_REPOSITORY_ROOT/scripts/recipe_ci/aisbench.py" render-model-config \
    --template "$RECIPE_PLAN_DIR/aisbench/models/$model_config.py" \
    --output "$runtime_config_dir/models/$model_config.py"

cd "$RECIPE_STEP_ARTIFACT_DIR"

"$RECIPE_AISBENCH_BIN" \
    --config-dir "$runtime_config_dir" \
    --models "$model_config" \
    --datasets gsm8k_gen_0_shot_cot_str_perf \
    --mode perf \
    --summarizer default_perf \
    --num-prompts 1

python3 "$RECIPE_REPOSITORY_ROOT/scripts/recipe_ci/aisbench.py" performance \
    --artifact-directory "$RECIPE_STEP_ARTIFACT_DIR" \
    --result-file "$RECIPE_STEP_RESULT_FILE"
