#!/usr/bin/env python3
"""Execute one node from a Recipe CI multi-node intermediate plan."""

from __future__ import annotations

import argparse
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.recipe_ci.coordinator import (  # noqa: E402
    CoordinatorClient,
    CoordinatorError,
    LeaderCoordinator,
)
from scripts.recipe_ci.plan import (  # noqa: E402
    Host,
    Node,
    Plan,
    PlanError,
    ScriptStep,
    load_hosts,
    load_plan,
)


DEFAULT_VLLM_ASCEND_ROOT = Path("/vllm-workspace/vllm-ascend")
REQUIRED_VLLM_ASCEND_TOOLS = (
    Path("examples/external_online_dp/launch_online_dp.py"),
    Path("examples/external_online_dp/dp_load_balance_proxy_server.py"),
    Path("examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py"),
)
DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class RunnerError(RuntimeError):
    """The local node could not complete the plan."""


@dataclass
class ManagedProcess:
    name: str
    process: subprocess.Popen[bytes]
    log_path: Path
    log_file: BinaryIO


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--hosts", type=Path)
    parser.add_argument("--node-id")
    parser.add_argument("--model-path")
    parser.add_argument("--vllm-ascend-root", type=Path)
    parser.add_argument("--control-port", type=int, default=29599)
    parser.add_argument("--startup-timeout-seconds", type=int, default=1800)
    parser.add_argument("--run-timeout-seconds", type=int, default=3600)
    parser.add_argument(
        "--evaluation",
        choices=("none", "accuracy", "performance", "all"),
        default="none",
    )
    parser.add_argument("--artifact-root", type=Path, default=Path("/tmp/recipe-ci"))
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args()


def interface_addresses() -> dict[str, str]:
    """Return Linux interface-to-IPv4 mappings used for local node selection."""
    try:
        result = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return {}

    addresses: dict[str, str] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 4:
            addresses[fields[1]] = fields[3].split("/", 1)[0]
    return addresses


def select_node(plan: Plan, hosts: dict[str, Host], requested: str | None) -> Node:
    if requested:
        return plan.node(requested)

    local_addresses = set(interface_addresses().values())
    local_addresses.update(
        address[4][0]
        for address in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    )
    matches = [node for node in plan.nodes if hosts[node.id].address in local_addresses]
    if len(matches) != 1:
        raise RunnerError("cannot select one local node; pass --node-id explicitly")
    return matches[0]


def select_interface(host: Host) -> str:
    if host.interface:
        return host.interface
    for interface, address in interface_addresses().items():
        if address == host.address:
            return interface
    raise RunnerError("cannot detect the local interface; set it in hosts.yaml")


def resolve_vllm_ascend_root(requested: Path | None) -> Path:
    root = requested or Path(
        os.environ.get("VLLM_ASCEND_ROOT", str(DEFAULT_VLLM_ASCEND_ROOT))
    )
    root = root.resolve()
    for relative_path in REQUIRED_VLLM_ASCEND_TOOLS:
        if not (root / relative_path).is_file():
            raise RunnerError(
                f"vllm-ascend runtime tool not found: {root / relative_path}"
            )
    return root


def _node_env_name(node_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", node_id).upper()


def base_environment(
    plan: Plan,
    node: Node,
    hosts: dict[str, Host],
    interface: str,
    model_path: str,
    vllm_ascend_root: Path,
    control_port: int,
    artifact_root: Path,
) -> dict[str, str]:
    local_ip = hosts[node.id].address
    leader_ip = hosts[plan.leader.id].address
    environment = os.environ.copy()
    environment.update(
        {
            "RECIPE_PLAN_DIR": str(plan.directory),
            "RECIPE_NODE_ID": node.id,
            "RECIPE_NODE_INDEX": str(node.index),
            "RECIPE_LOCAL_IP": local_ip,
            "RECIPE_LOCAL_INTERFACE": interface,
            "RECIPE_LEADER_IP": leader_ip,
            "RECIPE_CONTROL_PORT": str(control_port),
            "RECIPE_MODEL_ID": plan.model.id,
            "RECIPE_MODEL_PATH": model_path,
            "RECIPE_SERVED_MODEL_NAME": plan.model.served_name,
            "RECIPE_SERVICE_PORT_START": str(node.readiness.port_start),
            "RECIPE_SERVICE_COUNT": str(node.readiness.count),
            "RECIPE_VLLM_ASCEND_ROOT": str(vllm_ascend_root),
            "RECIPE_ARTIFACT_ROOT": str(artifact_root),
            "HCCL_IF_IP": local_ip,
            "HCCL_SOCKET_IFNAME": interface,
            "GLOO_SOCKET_IFNAME": interface,
            "TP_SOCKET_IFNAME": interface,
        }
    )
    if plan.gateway:
        environment["RECIPE_GATEWAY_PORT"] = str(plan.gateway.port)
    for plan_node in plan.nodes:
        address = hosts[plan_node.id].address
        environment[f"RECIPE_NODE_{plan_node.index}_IP"] = address
        environment[f"RECIPE_NODE_{_node_env_name(plan_node.id)}_IP"] = address

    no_proxy = environment.get("NO_PROXY", environment.get("no_proxy", "")).split(",")
    no_proxy.extend(host.address for host in hosts.values())
    environment["NO_PROXY"] = ",".join(dict.fromkeys(item for item in no_proxy if item))
    environment["no_proxy"] = environment["NO_PROXY"]
    return environment


def launch_script(
    name: str,
    script: Path,
    environment: dict[str, str],
    log_path: Path,
) -> ManagedProcess:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("wb")
    print(f"[{environment['RECIPE_NODE_ID']}] starting {name}; log: {log_path}")
    process = subprocess.Popen(
        ["bash", script.name],
        cwd=script.parent,
        env=environment,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return ManagedProcess(name, process, log_path, log_file)


def check_processes(processes: list[ManagedProcess]) -> None:
    for item in processes:
        return_code = item.process.poll()
        if return_code is not None:
            raise RunnerError(
                f"{item.name} exited with {return_code}; see {item.log_path}"
            )


def stop_processes(processes: list[ManagedProcess]) -> None:
    for item in reversed(processes):
        if item.process.poll() is None:
            try:
                os.killpg(item.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 10
    for item in reversed(processes):
        if item.process.poll() is None:
            try:
                item.process.wait(max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(item.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                item.process.wait()
        item.log_file.close()


def wait_http_ready(
    url: str,
    timeout: int,
    check_runtime: Callable[[], object],
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        check_runtime()
        try:
            with DIRECT_OPENER.open(url, timeout=2) as response:
                if response.status < 400:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(1)
    raise RunnerError(f"timed out waiting for {url}")


def wait_node_ready(
    node: Node,
    host: Host,
    timeout: int,
    check_runtime: Callable[[], object],
) -> None:
    deadline = time.monotonic() + timeout
    for offset in range(node.readiness.count):
        remaining = max(1, int(deadline - time.monotonic()))
        url = (
            f"http://{host.address}:{node.readiness.port_start + offset}"
            f"{node.readiness.health_path}"
        )
        wait_http_ready(url, remaining, check_runtime)


def run_steps(
    stage: str,
    steps: list[ScriptStep],
    plan: Plan,
    environment: dict[str, str],
    artifact_directory: Path,
) -> None:
    for step in steps:
        step_directory = artifact_directory / stage
        step_directory.mkdir(parents=True, exist_ok=True)
        step_environment = environment.copy()
        step_environment["RECIPE_ARTIFACT_DIR"] = str(step_directory)
        script = plan.directory / step.script
        log_path = step_directory / f"{step.id}.log"
        print(f"[leader] running {stage}: {step.id}; log: {log_path}")
        with log_path.open("wb") as log_file:
            try:
                result = subprocess.run(
                    ["bash", script.name],
                    cwd=script.parent,
                    env=step_environment,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    timeout=step.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise RunnerError(
                    f"{stage} {step.id} timed out; see {log_path}"
                ) from error
        if result.returncode != 0:
            raise RunnerError(
                f"{stage} {step.id} exited with {result.returncode}; see {log_path}"
            )


def run_evaluations(
    selection: str,
    plan: Plan,
    environment: dict[str, str],
    artifact_directory: Path,
) -> None:
    if selection in ("accuracy", "all"):
        run_steps(
            "accuracy",
            plan.evaluations.accuracy,
            plan,
            environment,
            artifact_directory,
        )
    if selection in ("performance", "all"):
        run_steps(
            "performance",
            plan.evaluations.performance,
            plan,
            environment,
            artifact_directory,
        )


def run_node(
    plan: Plan,
    hosts: dict[str, Host],
    node: Node,
    args: argparse.Namespace,
) -> None:
    host = hosts[node.id]
    interface = select_interface(host)
    model_path = (
        args.model_path or os.environ.get("RECIPE_CI_MODEL_PATH") or plan.model.id
    )
    vllm_ascend_root = resolve_vllm_ascend_root(args.vllm_ascend_root)
    artifact_directory = args.artifact_root / plan.name / node.id
    environment = base_environment(
        plan,
        node,
        hosts,
        interface,
        model_path,
        vllm_ascend_root,
        args.control_port,
        args.artifact_root,
    )
    if plan.gateway:
        endpoint_port = plan.gateway.port
    else:
        endpoint_port = plan.leader.readiness.port_start
    environment["RECIPE_ENDPOINT"] = (
        f"http://{hosts[plan.leader.id].address}:{endpoint_port}"
    )

    coordinator: LeaderCoordinator | None = None
    client = CoordinatorClient(hosts[plan.leader.id].address, args.control_port)
    processes: list[ManagedProcess] = []

    try:
        if node.id == plan.leader.id:
            coordinator = LeaderCoordinator(
                [item.id for item in plan.nodes], args.control_port
            )
            coordinator.start()

        service_script = plan.directory / node.launch
        processes.append(
            launch_script(
                "service launcher",
                service_script,
                environment,
                artifact_directory / "service.log",
            )
        )
        wait_node_ready(
            node,
            host,
            args.startup_timeout_seconds,
            lambda: check_processes(processes),
        )

        if coordinator:
            coordinator.state.mark_ready(node.id)
            print(f"[{node.id}] local backends ready; waiting for the other nodes")
            coordinator.wait_ready(
                args.startup_timeout_seconds,
                lambda: check_processes(processes),
            )

            if plan.gateway:
                gateway_script = plan.directory / plan.gateway.launch
                processes.append(
                    launch_script(
                        "gateway",
                        gateway_script,
                        environment,
                        artifact_directory / "gateway.log",
                    )
                )

                def check_leader_runtime() -> None:
                    check_processes(processes)
                    coordinator.raise_if_failed()

                wait_http_ready(
                    environment["RECIPE_ENDPOINT"] + plan.gateway.health_path,
                    args.startup_timeout_seconds,
                    check_leader_runtime,
                )

            run_steps(
                "checks",
                plan.checks,
                plan,
                environment,
                artifact_directory,
            )
            run_evaluations(
                args.evaluation,
                plan,
                environment,
                artifact_directory,
            )
            check_processes(processes)
            coordinator.raise_if_failed()
            coordinator.state.finish("done")
            coordinator.state.mark_completed(node.id)
            coordinator.wait_completed(30)
            coordinator.raise_if_failed()
            print(f"[{node.id}] plan completed")
        else:
            client.mark_ready(node.id, args.startup_timeout_seconds)
            print(f"[{node.id}] local backends ready; waiting for the leader result")
            result = client.wait_terminal(
                args.run_timeout_seconds,
                lambda: check_processes(processes),
            )
            client.mark_completed(node.id)
            if result["status"] != "done":
                raise RunnerError(str(result["message"]))
            print(f"[{node.id}] plan completed")
    except Exception as error:
        if coordinator:
            coordinator.state.finish("failed", str(error))
            coordinator.state.mark_completed(node.id)
            try:
                coordinator.wait_completed(10)
            except CoordinatorError:
                pass
        else:
            try:
                client.mark_failed(node.id, str(error))
                client.mark_completed(node.id)
            except CoordinatorError:
                pass
        raise
    finally:
        stop_processes(processes)
        if coordinator:
            coordinator.close()


def main() -> int:
    args = parse_args()
    try:
        plan = load_plan(args.plan)
        if args.validate_only:
            print(f"valid plan: {plan.name} ({len(plan.nodes)} nodes)")
            return 0
        if not args.hosts:
            raise RunnerError("--hosts is required unless --validate-only is used")
        hosts = load_hosts(args.hosts, plan)
        node = select_node(plan, hosts, args.node_id)
        run_node(plan, hosts, node, args)
        return 0
    except (OSError, PlanError, CoordinatorError, RunnerError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
