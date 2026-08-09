#!/usr/bin/env python3
"""Load the executable Recipe CI v1 intermediate plan."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


API_VERSION = "recipe-ci/v1"


class PlanError(ValueError):
    """The plan or local hosts file cannot be executed."""


@dataclass(frozen=True)
class Model:
    id: str
    cache_path: str
    served_name: str


@dataclass(frozen=True)
class Resources:
    npu_per_node: int


@dataclass(frozen=True)
class Readiness:
    port_start: int
    count: int = 1
    health_path: str = "/health"


@dataclass(frozen=True)
class Node:
    id: str
    index: int
    role: str
    launch: str
    readiness: Readiness | None


@dataclass(frozen=True)
class Gateway:
    launch: str
    port: int
    health_path: str = "/healthcheck"


@dataclass(frozen=True)
class ScriptStep:
    id: str
    script: str
    timeout_seconds: int


@dataclass(frozen=True)
class Evaluations:
    accuracy: list[ScriptStep]
    performance: list[ScriptStep]


@dataclass(frozen=True)
class Plan:
    path: Path
    name: str
    model: Model
    resources: Resources
    nodes: list[Node]
    gateway: Gateway | None
    checks: list[ScriptStep]
    evaluations: Evaluations

    @property
    def directory(self) -> Path:
        return self.path.parent

    @property
    def leader(self) -> Node:
        return self.nodes[0]

    def node(self, node_id: str) -> Node:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise PlanError(f"Unknown node: {node_id}")


@dataclass(frozen=True)
class Host:
    address: str
    interface: str | None = None


def _mapping(
    value: Any, field: str, required: tuple[str, ...] = ()
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlanError(f"{field} must be a mapping")
    missing = [key for key in required if key not in value]
    if missing:
        raise PlanError(f"{field} is missing fields: {', '.join(missing)}")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanError(f"{field} must be a non-empty string")
    return value


def _positive_int(value: Any, field: str) -> int:
    if type(value) is not int or value < 1:
        raise PlanError(f"{field} must be a positive integer")
    return value


def _port(value: Any, field: str) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise PlanError(f"{field} must be between 1 and 65535")
    return value


def _health_path(value: Any, field: str) -> str:
    path = _text(value, field)
    if not path.startswith("/"):
        raise PlanError(f"{field} must start with '/'")
    return path


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise PlanError(f"File not found: {path}")
    try:
        return _mapping(
            yaml.safe_load(path.read_text(encoding="utf-8")), str(path)
        )
    except yaml.YAMLError as error:
        raise PlanError(f"Invalid YAML in {path}: {error}") from error


def _script(plan_path: Path, value: Any, field: str) -> str:
    script = _text(value, field)
    if not (plan_path.parent / script).is_file():
        raise PlanError(f"{field} does not exist: {script}")
    return script


def _steps(plan_path: Path, value: Any, field: str) -> list[ScriptStep]:
    if not isinstance(value, list):
        raise PlanError(f"{field} must be a list")
    steps = []
    for index, item in enumerate(value):
        item_field = f"{field}[{index}]"
        raw = _mapping(item, item_field, ("id", "script"))
        steps.append(
            ScriptStep(
                id=_text(raw["id"], f"{item_field}.id"),
                script=_script(plan_path, raw["script"], f"{item_field}.script"),
                timeout_seconds=_positive_int(
                    raw.get("timeout_seconds", 300),
                    f"{item_field}.timeout_seconds",
                ),
            )
        )
    return steps


def load_plan(path: Path) -> Plan:
    """Load the final plan directly; compatibility belongs in its generator."""
    path = path.resolve()
    raw = _mapping(
        _read_yaml(path),
        "plan",
        ("api_version", "kind", "metadata", "model", "resources", "nodes"),
    )
    if raw["api_version"] != API_VERSION:
        raise PlanError(f"api_version must be {API_VERSION}")
    if raw["kind"] != "MultiNodePlan":
        raise PlanError("kind must be MultiNodePlan")

    metadata = _mapping(raw["metadata"], "metadata", ("name",))
    model = _mapping(
        raw["model"], "model", ("id", "cache_path", "served_name")
    )
    resources = _mapping(raw["resources"], "resources", ("npu_per_node",))
    nodes_raw = raw["nodes"]
    if not isinstance(nodes_raw, list) or len(nodes_raw) < 2:
        raise PlanError("nodes must contain at least two entries")

    nodes: list[Node] = []
    launch_paths: set[Path] = set()
    for index, item in enumerate(nodes_raw):
        field = f"nodes[{index}]"
        node = _mapping(item, field, ("id", "role", "launch"))
        expected_id = f"node{index}"
        if node["id"] != expected_id:
            raise PlanError(f"{field}.id must be {expected_id}")
        launch = _script(path, node["launch"], f"{field}.launch")
        launch_path = (path.parent / launch).resolve()
        if launch_path in launch_paths:
            raise PlanError(f"each node must have its own launch script: {launch}")
        launch_paths.add(launch_path)

        readiness = None
        if node.get("readiness") is not None:
            ready = _mapping(
                node["readiness"], f"{field}.readiness", ("port_start",)
            )
            port_start = _port(
                ready["port_start"], f"{field}.readiness.port_start"
            )
            count = _positive_int(
                ready.get("count", 1), f"{field}.readiness.count"
            )
            if port_start + count > 65536:
                raise PlanError(f"{field}.readiness port range exceeds 65535")
            readiness = Readiness(
                port_start=port_start,
                count=count,
                health_path=_health_path(
                    ready.get("health_path", "/health"),
                    f"{field}.readiness.health_path",
                ),
            )
        nodes.append(
            Node(
                id=expected_id,
                index=index,
                role=_text(node["role"], f"{field}.role"),
                launch=launch,
                readiness=readiness,
            )
        )

    gateway = None
    if raw.get("gateway") is not None:
        gateway_raw = _mapping(raw["gateway"], "gateway", ("launch", "port"))
        gateway = Gateway(
            launch=_script(path, gateway_raw["launch"], "gateway.launch"),
            port=_port(gateway_raw["port"], "gateway.port"),
            health_path=_health_path(
                gateway_raw.get("health_path", "/healthcheck"),
                "gateway.health_path",
            ),
        )
        leader_readiness = nodes[0].readiness
        if leader_readiness and gateway.port in range(
            leader_readiness.port_start,
            leader_readiness.port_start + leader_readiness.count,
        ):
            raise PlanError("gateway.port conflicts with leader readiness ports")
    elif nodes[0].readiness is None:
        raise PlanError("the leader needs HTTP readiness when gateway is omitted")

    evaluations = _mapping(raw.get("evaluations", {}), "evaluations")
    return Plan(
        path=path,
        name=_text(metadata["name"], "metadata.name"),
        model=Model(
            id=_text(model["id"], "model.id"),
            cache_path=_text(model["cache_path"], "model.cache_path"),
            served_name=_text(model["served_name"], "model.served_name"),
        ),
        resources=Resources(
            npu_per_node=_positive_int(
                resources["npu_per_node"], "resources.npu_per_node"
            )
        ),
        nodes=nodes,
        gateway=gateway,
        checks=_steps(path, raw.get("checks", []), "checks"),
        evaluations=Evaluations(
            accuracy=_steps(
                path, evaluations.get("accuracy", []), "evaluations.accuracy"
            ),
            performance=_steps(
                path,
                evaluations.get("performance", []),
                "evaluations.performance",
            ),
        ),
    )


def load_hosts(path: Path, plan: Plan) -> dict[str, Host]:
    raw = _mapping(_read_yaml(path.resolve()), "hosts file", ("version", "hosts"))
    if type(raw["version"]) is not int or raw["version"] != 1:
        raise PlanError("hosts version must be 1")
    hosts_raw = _mapping(raw["hosts"], "hosts")
    expected = {node.id for node in plan.nodes}
    actual = set(hosts_raw)
    if actual != expected:
        raise PlanError(
            "hosts keys must match plan nodes; "
            f"missing={sorted(expected - actual)}, "
            f"unexpected={sorted(str(key) for key in actual - expected)}"
        )

    hosts = {}
    for node_id, value in hosts_raw.items():
        field = f"hosts.{node_id}"
        host = _mapping(value, field, ("address",))
        interface = host.get("interface")
        hosts[node_id] = Host(
            address=_text(host["address"], f"{field}.address"),
            interface=(
                _text(interface, f"{field}.interface")
                if interface is not None
                else None
            ),
        )
    return hosts


def format_topology_summary(
    plan: Plan, hosts: dict[str, Host] | None = None
) -> str:
    """Print only information useful before starting a local or CI run."""
    lines = [
        f"Plan: {plan.name} ({len(plan.nodes)} nodes, "
        f"{plan.resources.npu_per_node} NPUs/node)",
        f"Model: {plan.model.id} (served as {plan.model.served_name})",
    ]
    for node in plan.nodes:
        host = ""
        if hosts:
            item = hosts[node.id]
            host = f" @{item.address}%{item.interface or 'auto'}"
        ready = ""
        if node.readiness:
            ready = f" ports={node.readiness.port_start}"
            if node.readiness.count > 1:
                ready += f"-{node.readiness.port_start + node.readiness.count - 1}"
        lines.append(f"{node.id}{host}: {node.role}, {node.launch}{ready}")
    if plan.gateway:
        lines.append(f"Gateway: {plan.gateway.launch} port={plan.gateway.port}")
    endpoint_port = (
        plan.gateway.port if plan.gateway else plan.leader.readiness.port_start
    )
    if hosts:
        lines.append(
            f"Endpoint: http://{hosts[plan.leader.id].address}:{endpoint_port}"
        )
    lines.append(
        "Steps: "
        f"checks={len(plan.checks)}, "
        f"accuracy={len(plan.evaluations.accuracy)}, "
        f"performance={len(plan.evaluations.performance)}"
    )
    return "\n".join(lines)
