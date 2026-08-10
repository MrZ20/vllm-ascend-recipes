#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
: "${LWS_WORKER_INDEX:?LWS_WORKER_INDEX is required}"
: "${LWS_LEADER_ADDRESS:?LWS_LEADER_ADDRESS is required}"
: "${RECIPE_CI_NODE_COUNT:?RECIPE_CI_NODE_COUNT is required}"

if [[ ! "$LWS_WORKER_INDEX" =~ ^(0|[1-9][0-9]*)$ ]]; then
    echo "LWS_WORKER_INDEX must be a non-negative integer" >&2
    exit 1
fi
if [[ ! "$RECIPE_CI_NODE_COUNT" =~ ^[1-9][0-9]*$ ]]; then
    echo "RECIPE_CI_NODE_COUNT must be a positive integer" >&2
    exit 1
fi
if ((LWS_WORKER_INDEX >= RECIPE_CI_NODE_COUNT)); then
    echo "LWS_WORKER_INDEX is outside the LWS node range: $LWS_WORKER_INDEX" >&2
    exit 1
fi

IFS='.' read -r leader_name group_name namespace_name _ <<< "$LWS_LEADER_ADDRESS"
if [[ -z "$leader_name" || -z "$group_name" || -z "$namespace_name" ]]; then
    echo "Invalid LWS_LEADER_ADDRESS: $LWS_LEADER_ADDRESS" >&2
    exit 1
fi

resolve_ipv4() {
    local dns=$1
    local address=""
    local deadline=$((SECONDS + ${RECIPE_CI_STARTUP_TIMEOUT_SECONDS:-300}))
    echo "Waiting for LWS DNS: $dns" >&2
    while ((SECONDS < deadline)); do
        address=$(getent ahostsv4 "$dns" 2>/dev/null | awk 'NR == 1 {print $1}' || true)
        if [[ -n "$address" ]]; then
            printf '%s\n' "$address"
            return 0
        fi
        sleep 1
    done
    echo "Unable to resolve LWS DNS: $dns" >&2
    return 1
}

cluster_ips=()
for ((index = 0; index < RECIPE_CI_NODE_COUNT; index++)); do
    if [[ $index -eq 0 ]]; then
        dns_name=$LWS_LEADER_ADDRESS
    else
        dns_name="${leader_name}-${index}.${group_name}.${namespace_name}"
    fi
    cluster_ips+=("$(resolve_ipv4 "$dns_name")")
done

export RECIPE_CI_NODE_INDEX=$LWS_WORKER_INDEX
RECIPE_CI_CLUSTER_IPS=$(IFS=,; echo "${cluster_ips[*]}")
export RECIPE_CI_CLUSTER_IPS
exec bash "$SCRIPT_DIR/../run.sh"
