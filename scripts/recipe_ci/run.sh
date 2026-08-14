#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 0 ]]; then
    echo "run.sh is configured through RECIPE_CI_* environment variables" >&2
    exit 2
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPOSITORY_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
: "${RECIPE_CI_PLAN:?RECIPE_CI_PLAN is required}"

cd "$REPOSITORY_ROOT"

if [[ -f /usr/local/Ascend/ascend-toolkit/set_env.sh ]]; then
    set +u
    # shellcheck source=/dev/null
    source /usr/local/Ascend/ascend-toolkit/set_env.sh
    set -u
fi

if [[ -f /usr/local/Ascend/nnal/atb/set_env.sh ]]; then
    set +u
    # shellcheck source=/dev/null
    source /usr/local/Ascend/nnal/atb/set_env.sh
    set -u
fi

if [[ "${RECIPE_CI_VALIDATE_ONLY:-false}" == "true" ]]; then
    exec python3 -u "$SCRIPT_DIR/runner.py" \
        --plan "$RECIPE_CI_PLAN" \
        --validate-only
fi

: "${RECIPE_CI_NODE_INDEX:?RECIPE_CI_NODE_INDEX is required}"
: "${RECIPE_CI_CLUSTER_IPS:?RECIPE_CI_CLUSTER_IPS is required}"
if [[ ! "$RECIPE_CI_NODE_INDEX" =~ ^(0|[1-9][0-9]*)$ ]]; then
    echo "RECIPE_CI_NODE_INDEX must be a non-negative integer" >&2
    exit 1
fi

node_count=$(PLAN_PATH="$RECIPE_CI_PLAN" \
    PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 - <<'PY'
import os
from pathlib import Path

from plan import load_plan

print(len(load_plan(Path(os.environ["PLAN_PATH"])).nodes))
PY
)
if [[ -n "${RECIPE_CI_NODE_COUNT:-}" && "$RECIPE_CI_NODE_COUNT" != "$node_count" ]]; then
    echo "RECIPE_CI_NODE_COUNT does not match plan.nodes: $RECIPE_CI_NODE_COUNT != $node_count" >&2
    exit 1
fi
export RECIPE_CI_NODE_COUNT=$node_count

if ((RECIPE_CI_NODE_INDEX < 0 || RECIPE_CI_NODE_INDEX >= node_count)); then
    echo "RECIPE_CI_NODE_INDEX is outside the plan node range: $RECIPE_CI_NODE_INDEX" >&2
    exit 1
fi
node_id="node${RECIPE_CI_NODE_INDEX}"
hosts_file="/tmp/recipe-ci-hosts-${RECIPE_CI_NODE_INDEX}.yaml"

cluster_ips=()
IFS=',' read -r -a cluster_ips <<< "$RECIPE_CI_CLUSTER_IPS"

if [[ ${#cluster_ips[@]} -ne $node_count ]]; then
    echo "RECIPE_CI_CLUSTER_IPS count does not match plan.nodes: ${#cluster_ips[@]} != $node_count" >&2
    exit 1
fi
RECIPE_CI_CLUSTER_IPS=$(IFS=,; echo "${cluster_ips[*]}")
export RECIPE_CI_CLUSTER_IPS

{
    echo "version: 1"
    echo "hosts:"
    for ((index = 0; index < node_count; index++)); do
        echo "  node${index}:"
        echo "    address: ${cluster_ips[$index]}"
        if [[ $index -eq $RECIPE_CI_NODE_INDEX && -n "${RECIPE_CI_INTERFACE:-}" ]]; then
            echo "    interface: $RECIPE_CI_INTERFACE"
        fi
    done
} > "$hosts_file"

if [[ -z "${RECIPE_CI_VISIBLE_DEVICES:-}" ]]; then
    if [[ -n "${ASCEND_RT_VISIBLE_DEVICES:-}" ]]; then
        export RECIPE_CI_VISIBLE_DEVICES=$ASCEND_RT_VISIBLE_DEVICES
    elif [[ -n "${ASCEND_VISIBLE_DEVICES:-}" ]]; then
        export RECIPE_CI_VISIBLE_DEVICES=$ASCEND_VISIBLE_DEVICES
    fi
fi
echo "Recipe CI node: index=$RECIPE_CI_NODE_INDEX id=$node_id ip=${cluster_ips[$RECIPE_CI_NODE_INDEX]}"

artifact_root=${RECIPE_CI_ARTIFACT_ROOT:-/tmp/recipe-ci}
# shellcheck disable=SC2329  # Invoked by the EXIT trap.
collect_plogs() {
    [[ -n "${RECIPE_CI_PLOG_ROOT:-}" && -d /root/ascend/log ]] || return 0
    plog_directory="$RECIPE_CI_PLOG_ROOT/$node_id"
    mkdir -p "$plog_directory"
    cp -a /root/ascend/log/. "$plog_directory/" 2>/dev/null || true
}
trap collect_plogs EXIT

runner_pid=""
# shellcheck disable=SC2329  # Invoked by the TERM/INT traps.
forward_signal() {
    if [[ -n "$runner_pid" ]]; then
        kill "-$1" "$runner_pid" 2>/dev/null || true
    elif [[ $1 == TERM ]]; then
        exit 143
    else
        exit 130
    fi
}
trap 'forward_signal TERM' TERM
trap 'forward_signal INT' INT

python3 -u "$SCRIPT_DIR/runner.py" \
    --plan "$RECIPE_CI_PLAN" \
    --hosts "$hosts_file" \
    --node-id "$node_id" \
    --vllm-ascend-root "${VLLM_ASCEND_ROOT:-/vllm-workspace/vllm-ascend}" \
    --control-port "${RECIPE_CI_CONTROL_PORT:-29599}" \
    --startup-timeout-seconds "${RECIPE_CI_STARTUP_TIMEOUT_SECONDS:-1800}" \
    --run-timeout-seconds "${RECIPE_CI_RUN_TIMEOUT_SECONDS:-7200}" \
    --artifact-root "$artifact_root" &
runner_pid=$!

# A signal interrupts bash's wait before the Python Runner necessarily finishes
# its own process-group cleanup. Keep the entrypoint alive until it exits.
runner_status=0
while kill -0 "$runner_pid" 2>/dev/null; do
    wait "$runner_pid" || runner_status=$?
done
wait "$runner_pid" 2>/dev/null || {
    status=$?
    if [[ $status -ne 127 ]]; then
        runner_status=$status
    fi
}
exit "$runner_status"
