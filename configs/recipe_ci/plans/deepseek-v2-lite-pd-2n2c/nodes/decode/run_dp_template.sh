#!/usr/bin/env bash
set -euo pipefail

export ASCEND_RT_VISIBLE_DEVICES="$1"
export OMP_PROC_BIND=false
export OMP_NUM_THREADS=10
export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
export HCCL_BUFFSIZE=256
export VLLM_ASCEND_ENABLE_FLASHCOMM1=0
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
    --trust-remote-code \
    --quantization ascend \
    --enable-expert-parallel \
    --max-model-len 4096 \
    --max-num-seqs 8 \
    --kv-transfer-config '{"kv_connector":"MooncakeConnectorV1","kv_role":"kv_consumer","kv_port":"30200","kv_connector_extra_config":{"prefill":{"dp_size":2,"tp_size":1},"decode":{"dp_size":2,"tp_size":1}}}'
