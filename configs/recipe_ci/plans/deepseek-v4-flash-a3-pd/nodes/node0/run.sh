#!/usr/bin/env bash
set -euo pipefail

# DeepSeek V4 Flash A3 Prefill topology from the Recipe: DP4 x TP4 = 16 NPUs.
exec python3 \
    "$RECIPE_VLLM_ASCEND_ROOT/examples/external_online_dp/launch_online_dp.py" \
    --dp-size 4 \
    --tp-size 4 \
    --dp-size-local 4 \
    --dp-rank-start 0 \
    --dp-address "$RECIPE_LOCAL_IP" \
    --dp-rpc-port 12321 \
    --vllm-start-port "$RECIPE_SERVICE_PORT_START"
