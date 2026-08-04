#!/usr/bin/env python3
"""Data model for the hand-written Recipe CI intermediate plan."""

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
    served_name: str


@dataclass(frozen=True)
class Readiness:
    port_start: int
    count: int = 1
    health_path: str = "/health"


@dataclass(frozen=True)
class Node:
    id: str
    index: int
    launch: str
    readiness: Readiness


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


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlanError(f"{field} must be a mapping")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanError(f"{field} must be a non-empty string")
    return value


def _positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise PlanError(f"{field} must be a positive integer")
    return value


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise PlanError(f"File not found: {path}")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise PlanError(f"Invalid YAML in {path}: {error}") from error
    return _mapping(value, str(path))


def _script(path: Path, value: Any, field: str) -> str:
    script = _string(value, field)
    if not (path.parent / script).is_file():
        raise PlanError(f"script not found: {script}")
    return script


def _steps(path: Path, value: Any, field: str) -> list[ScriptStep]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PlanError(f"{field} must be a list")

    steps: list[ScriptStep] = []
    for position, item in enumerate(value):
        item_raw = _mapping(item, f"{field}[{position}]")
        steps.append(
            ScriptStep(
                id=_string(item_raw.get("id"), f"{field}[{position}].id"),
                script=_script(
                    path,
                    item_raw.get("script"),
                    f"{field}[{position}].script",
                ),
                timeout_seconds=_positive_int(
                    item_raw.get("timeout_seconds", 300),
                    f"{field}[{position}].timeout_seconds",
                ),
            )
        )
    return steps


def load_plan(path: Path) -> Plan:
    """Load the first intermediate format, without interpreting Recipe YAML."""
    path = path.resolve()
    raw = _read_yaml(path)
    if raw.get("api_version") != "recipe-ci/v1":
        raise PlanError("api_version must be recipe-ci/v1")
    if raw.get("kind") != "MultiNodePlan":
        raise PlanError("kind must be MultiNodePlan")

    metadata = _mapping(raw.get("metadata"), "metadata")
    model_raw = _mapping(raw.get("model"), "model")
    nodes_raw = raw.get("nodes")
    if not isinstance(nodes_raw, list) or len(nodes_raw) < 2:
        raise PlanError("nodes must contain at least two entries")

    nodes: list[Node] = []
    node_ids: set[str] = set()
    launch_scripts: set[str] = set()
    for index, item in enumerate(nodes_raw):
        node_raw = _mapping(item, f"nodes[{index}]")
        node_id = _string(node_raw.get("id"), f"nodes[{index}].id")
        launch = _script(path, node_raw.get("launch"), f"nodes[{index}].launch")
        readiness_raw = _mapping(node_raw.get("readiness"), f"nodes[{index}].readiness")
        if node_id in node_ids:
            raise PlanError(f"duplicate node id: {node_id}")
        if launch in launch_scripts:
            raise PlanError(f"each node must have its own launch script: {launch}")
        nodes.append(
            Node(
                id=node_id,
                index=index,
                launch=launch,
                readiness=Readiness(
                    port_start=_positive_int(
                        readiness_raw.get("port_start"),
                        f"nodes[{index}].readiness.port_start",
                    ),
                    count=_positive_int(
                        readiness_raw.get("count", 1),
                        f"nodes[{index}].readiness.count",
                    ),
                    health_path=_string(
                        readiness_raw.get("health_path", "/health"),
                        f"nodes[{index}].readiness.health_path",
                    ),
                ),
            )
        )
        node_ids.add(node_id)
        launch_scripts.add(launch)

    gateway: Gateway | None = None
    gateway_raw = raw.get("gateway")
    if gateway_raw is not None:
        gateway_mapping = _mapping(gateway_raw, "gateway")
        gateway = Gateway(
            launch=_script(path, gateway_mapping.get("launch"), "gateway.launch"),
            port=_positive_int(gateway_mapping.get("port"), "gateway.port"),
            health_path=_string(
                gateway_mapping.get("health_path", "/healthcheck"),
                "gateway.health_path",
            ),
        )

    evaluations_raw = _mapping(raw.get("evaluations", {}), "evaluations")
    return Plan(
        path=path,
        name=_string(metadata.get("name"), "metadata.name"),
        model=Model(
            id=_string(model_raw.get("id"), "model.id"),
            served_name=_string(model_raw.get("served_name"), "model.served_name"),
        ),
        nodes=nodes,
        gateway=gateway,
        checks=_steps(path, raw.get("checks", []), "checks"),
        evaluations=Evaluations(
            accuracy=_steps(
                path, evaluations_raw.get("accuracy", []), "evaluations.accuracy"
            ),
            performance=_steps(
                path,
                evaluations_raw.get("performance", []),
                "evaluations.performance",
            ),
        ),
    )


def load_hosts(path: Path, plan: Plan) -> dict[str, Host]:
    raw = _read_yaml(path.resolve())
    if raw.get("version") != 1:
        raise PlanError("hosts version must be 1")
    hosts_raw = _mapping(raw.get("hosts"), "hosts")
    expected = {node.id for node in plan.nodes}
    if set(hosts_raw) != expected:
        raise PlanError(f"hosts must contain exactly these nodes: {sorted(expected)}")

    hosts: dict[str, Host] = {}
    for node_id, value in hosts_raw.items():
        host_raw = _mapping(value, f"hosts.{node_id}")
        interface = host_raw.get("interface")
        if interface is not None:
            interface = _string(interface, f"hosts.{node_id}.interface")
        hosts[node_id] = Host(
            address=_string(host_raw.get("address"), f"hosts.{node_id}.address"),
            interface=interface,
        )
    return hosts
