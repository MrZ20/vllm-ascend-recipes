#!/usr/bin/env python3
"""Load the executable Recipe CI v1 intermediate plan."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


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
    inputs: dict[str, Any]


@dataclass(frozen=True)
class Stage:
    id: str
    failure_category: str
    steps: list[ScriptStep]


@dataclass(frozen=True)
class Plan:
    path: Path
    name: str
    model: Model
    resources: Resources
    nodes: list[Node]
    gateway: Gateway | None
    stages: list[Stage]

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


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise PlanError(f"File not found: {path}")
    try:
        return _mapping(
            yaml.safe_load(path.read_text(encoding="utf-8")), str(path)
        )
    except yaml.YAMLError as error:
        raise PlanError(f"Invalid YAML in {path}: {error}") from error


def _decode_readiness(value: dict[str, Any] | None) -> Readiness | None:
    if value is None:
        return None
    return Readiness(
        port_start=value["port_start"],
        count=value.get("count", 1),
        health_path=value.get("health_path", "/health"),
    )


def _decode_step(value: dict[str, Any]) -> ScriptStep:
    return ScriptStep(
        id=value["id"],
        script=value["script"],
        timeout_seconds=value.get("timeout_seconds", 300),
        inputs=_mapping(value.get("inputs", {}), f"step {value['id']}.inputs"),
    )


def _decode_stage(value: dict[str, Any]) -> Stage:
    return Stage(
        id=value["id"],
        failure_category=value["failure_category"],
        steps=[_decode_step(step) for step in value["steps"]],
    )


def load_plan(path: Path) -> Plan:
    """Decode a converter-validated executable intermediate plan."""
    path = path.resolve()
    raw = _read_yaml(path)
    model = raw["model"]
    resources = raw["resources"]
    nodes = [
        Node(
            id=node["id"],
            index=index,
            role=node["role"],
            launch=node["launch"],
            readiness=_decode_readiness(node.get("readiness")),
        )
        for index, node in enumerate(raw["nodes"])
    ]
    gateway_raw = raw.get("gateway")
    gateway = (
        Gateway(
            launch=gateway_raw["launch"],
            port=gateway_raw["port"],
            health_path=gateway_raw.get("health_path", "/healthcheck"),
        )
        if gateway_raw is not None
        else None
    )
    return Plan(
        path=path,
        name=raw["metadata"]["name"],
        model=Model(
            id=model["id"],
            cache_path=model["cache_path"],
            served_name=model["served_name"],
        ),
        resources=Resources(npu_per_node=resources["npu_per_node"]),
        nodes=nodes,
        gateway=gateway,
        stages=[_decode_stage(stage) for stage in raw["stages"]],
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
        "Stages: "
        + ", ".join(f"{stage.id}={len(stage.steps)}" for stage in plan.stages)
    )
    return "\n".join(lines)
