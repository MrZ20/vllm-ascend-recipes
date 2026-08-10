#!/usr/bin/env bash
set -euo pipefail

runtime_config_dir=$RECIPE_STEP_ARTIFACT_DIR/aisbench-config
model_config=vllm_api_general_chat

bash "$RECIPE_PLAN_DIR/evaluations/prepare_gsm8k.sh"
python3 "$RECIPE_REPOSITORY_ROOT/scripts/recipe_ci/aisbench.py" render-model-config \
    --template "$RECIPE_PLAN_DIR/aisbench/models/$model_config.py" \
    --output "$runtime_config_dir/models/$model_config.py"

cd "$RECIPE_STEP_ARTIFACT_DIR"

"$RECIPE_AISBENCH_BIN" \
    --config-dir "$runtime_config_dir" \
    --models "$model_config" \
    --datasets gsm8k_gen_0_shot_cot_chat_prompt \
    --mode all \
    --num-prompts 1 \
    --dump-eval-details

python3 "$RECIPE_REPOSITORY_ROOT/scripts/recipe_ci/aisbench.py" accuracy \
    --artifact-directory "$RECIPE_STEP_ARTIFACT_DIR" \
    --result-file "$RECIPE_STEP_RESULT_FILE"
