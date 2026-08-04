#!/usr/bin/env bash
set -euo pipefail

exec python3 \
    "$RECIPE_VLLM_ASCEND_ROOT/examples/external_online_dp/launch_online_dp.py" \
    --dp-size 4 \
    --tp-size 1 \
    --dp-size-local "$RECIPE_SERVICE_COUNT" \
    --dp-rank-start 0 \
    --dp-address "$RECIPE_NODE_0_IP" \
    --dp-rpc-port 12321 \
    --vllm-start-port "$RECIPE_SERVICE_PORT_START"
