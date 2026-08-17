#!/usr/bin/env bash
set -euo pipefail

# launch_online_dp.py passes logical device indexes. Map them onto the cards
# selected before starting the Runner, for example 4,5.
IFS=',' read -r -a selected_devices <<< "$MULTI_NODE_VISIBLE_DEVICES"
export ASCEND_RT_VISIBLE_DEVICES="${selected_devices[$1]}"
export OMP_PROC_BIND=false
export OMP_NUM_THREADS=10
export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
export HCCL_BUFFSIZE=256
export VLLM_ASCEND_ENABLE_FLASHCOMM1=0
export VLLM_USE_MODELSCOPE=true

rank_log_directory="$MULTI_NODE_NODE_ARTIFACT_DIR/servers"
mkdir -p "$rank_log_directory"
rank_log="$rank_log_directory/rank-$4.log"

# The upstream external-DP launcher starts all local ranks below one parent
# process. Redirect here so each vllm serve instance has an independent log;
# launcher failures still go to the Runner-managed service.log.
{
    echo "external DP rank=$4 device=$ASCEND_RT_VISIBLE_DEVICES port=$2"
    exec vllm serve "$MULTI_NODE_MODEL_PATH" \
        --host 0.0.0.0 \
        --port "$2" \
        --served-model-name "$MULTI_NODE_SERVED_MODEL_NAME" \
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
} > "$rank_log" 2>&1
