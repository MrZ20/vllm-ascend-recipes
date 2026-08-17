#!/usr/bin/env bash
set -euo pipefail

exec python3 "$MULTI_NODE_REPOSITORY_ROOT/test/recipe/multi_node/scripts/run_online_dp.py" \
    "$MULTI_NODE_VLLM_ASCEND_ROOT/examples/external_online_dp/launch_online_dp.py" \
    --dp-size 2 \
    --tp-size 1 \
    --dp-size-local "$MULTI_NODE_SERVICE_COUNT" \
    --dp-rank-start 0 \
    --dp-address "$MULTI_NODE_LOCAL_IP" \
    --dp-rpc-port 12321 \
    --vllm-start-port "$MULTI_NODE_SERVICE_PORT_START"
