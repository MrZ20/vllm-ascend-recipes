#!/usr/bin/env bash
set -euo pipefail

export ASCEND_RT_VISIBLE_DEVICES="$1"
export OMP_PROC_BIND=false
export OMP_NUM_THREADS=10
export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
export HCCL_BUFFSIZE=1024
export VLLM_USE_MODELSCOPE=true

exec vllm serve "$RECIPE_MODEL_PATH" \
    --host 0.0.0.0 \
    --port "$2" \
    --served-model-name "$RECIPE_SERVED_MODEL_NAME" \
    --data-parallel-size "$3" \
    --data-parallel-rank "$4" \
    --data-parallel-address "$5" \
    --data-parallel-rpc-port "$6" \
    --tensor-parallel-size "$7" \
    --max-model-len 4096 \
    --max-num-seqs 8 \
    --trust-remote-code \
    --enable-expert-parallel
