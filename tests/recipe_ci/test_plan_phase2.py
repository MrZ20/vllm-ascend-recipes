from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.recipe_ci.plan import (  # noqa: E402
    PlanError,
    format_topology_summary,
    load_hosts,
    load_plan,
)


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.plan_directory = Path(self.temporary_directory.name)
        for relative_path in (
            "nodes/node0/run.sh",
            "nodes/node1/run.sh",
            "gateway/run.sh",
            "checks/completion.sh",
            "evaluations/accuracy.sh",
            "evaluations/performance.sh",
        ):
            path = self.plan_directory / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

        self.plan_data: dict[str, Any] = {
            "api_version": "recipe-ci/v1",
            "kind": "MultiNodePlan",
            "metadata": {"name": "phase2-plan"},
            "model": {
                "id": "example/model",
                "cache_path": "example/model",
                "served_name": "example",
            },
            "resources": {"npu_per_node": 2},
            "nodes": [
                {
                    "id": "node0",
                    "role": "prefill",
                    "launch": "nodes/node0/run.sh",
                    "readiness": {
                        "port_start": 7100,
                        "count": 2,
                        "health_path": "/health",
                    },
                },
                {
                    "id": "node1",
                    "role": "decode",
                    "launch": "nodes/node1/run.sh",
                    "readiness": {"port_start": 7200},
                },
            ],
            "gateway": {
                "launch": "gateway/run.sh",
                "port": 38085,
                "health_path": "/healthcheck",
            },
            "checks": [
                {
                    "id": "completion",
                    "script": "checks/completion.sh",
                    "timeout_seconds": 300,
                }
            ],
            "evaluations": {
                "accuracy": [
                    {
                        "id": "accuracy",
                        "script": "evaluations/accuracy.sh",
                        "timeout_seconds": 600,
                    }
                ],
                "performance": [
                    {
                        "id": "performance",
                        "script": "evaluations/performance.sh",
                        "timeout_seconds": 900,
                    }
                ],
            },
        }
        self.hosts_data = {
            "version": 1,
            "hosts": {
                "node0": {"address": "192.0.2.10", "interface": "eth0"},
                "node1": {"address": "192.0.2.11"},
            },
        }

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_yaml(self, name: str, data: Any) -> Path:
        path = self.plan_directory / name
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return path

    def write_plan(self, data: dict[str, Any] | None = None) -> Path:
        return self.write_yaml(
            "plan.yaml", self.plan_data if data is None else data
        )

    def write_hosts(self, data: dict[str, Any] | None = None) -> Path:
        return self.write_yaml(
            "hosts.yaml", self.hosts_data if data is None else data
        )

    def test_loads_executable_fields_and_ignores_generator_metadata(self) -> None:
        data = copy.deepcopy(self.plan_data)
        data["generated_by"] = "recipe compiler"
        data["metadata"]["source_recipe"] = "recipes/example.yaml"

        plan = load_plan(self.write_plan(data))

        self.assertEqual(plan.name, "phase2-plan")
        self.assertEqual(plan.model.cache_path, "example/model")
        self.assertEqual(plan.resources.npu_per_node, 2)
        self.assertEqual([node.id for node in plan.nodes], ["node0", "node1"])
        self.assertEqual(plan.nodes[0].readiness.count, 2)
        self.assertEqual(plan.nodes[1].readiness.count, 1)
        self.assertEqual(plan.gateway.port, 38085)
        self.assertEqual(plan.checks[0].timeout_seconds, 300)
        self.assertEqual(plan.evaluations.accuracy[0].timeout_seconds, 600)

    def test_required_fields_and_container_types_are_checked(self) -> None:
        cases = []
        missing_nodes = copy.deepcopy(self.plan_data)
        del missing_nodes["nodes"]
        cases.append((missing_nodes, "plan is missing fields: nodes"))
        invalid_model = copy.deepcopy(self.plan_data)
        invalid_model["model"] = []
        cases.append((invalid_model, "model must be a mapping"))
        invalid_nodes = copy.deepcopy(self.plan_data)
        invalid_nodes["nodes"] = {}
        cases.append((invalid_nodes, "nodes must contain at least two"))
        invalid_checks = copy.deepcopy(self.plan_data)
        invalid_checks["checks"] = {}
        cases.append((invalid_checks, "checks must be a list"))

        for data, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                PlanError, message
            ):
                load_plan(self.write_plan(data))

    def test_nodes_are_ordered_and_use_independent_existing_scripts(self) -> None:
        one_node = copy.deepcopy(self.plan_data)
        one_node["nodes"] = one_node["nodes"][:1]
        with self.assertRaisesRegex(PlanError, "at least two"):
            load_plan(self.write_plan(one_node))

        wrong_id = copy.deepcopy(self.plan_data)
        wrong_id["nodes"][1]["id"] = "node2"
        with self.assertRaisesRegex(PlanError, r"nodes\[1\]\.id must be node1"):
            load_plan(self.write_plan(wrong_id))

        shared_script = copy.deepcopy(self.plan_data)
        shared_script["nodes"][1]["launch"] = "nodes/node0/run.sh"
        with self.assertRaisesRegex(PlanError, "each node must have its own"):
            load_plan(self.write_plan(shared_script))

        missing_script = copy.deepcopy(self.plan_data)
        missing_script["gateway"]["launch"] = "gateway/missing.sh"
        with self.assertRaisesRegex(PlanError, r"gateway\.launch does not exist"):
            load_plan(self.write_plan(missing_script))

    def test_integer_and_port_values_must_be_executable(self) -> None:
        cases = []
        invalid_npu = copy.deepcopy(self.plan_data)
        invalid_npu["resources"]["npu_per_node"] = 0
        cases.append((invalid_npu, r"resources\.npu_per_node"))
        invalid_timeout = copy.deepcopy(self.plan_data)
        invalid_timeout["checks"][0]["timeout_seconds"] = True
        cases.append((invalid_timeout, r"checks\[0\]\.timeout_seconds"))
        invalid_port = copy.deepcopy(self.plan_data)
        invalid_port["nodes"][0]["readiness"]["port_start"] = 65536
        cases.append((invalid_port, r"readiness\.port_start"))
        overflow = copy.deepcopy(self.plan_data)
        overflow["nodes"][0]["readiness"].update(
            {"port_start": 65535, "count": 2}
        )
        cases.append((overflow, "port range exceeds"))

        for data, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                PlanError, message
            ):
                load_plan(self.write_plan(data))

    def test_gateway_and_leader_readiness_define_one_endpoint(self) -> None:
        conflict = copy.deepcopy(self.plan_data)
        conflict["gateway"]["port"] = 7101
        with self.assertRaisesRegex(PlanError, "conflicts with leader readiness"):
            load_plan(self.write_plan(conflict))

        no_endpoint = copy.deepcopy(self.plan_data)
        del no_endpoint["gateway"]
        del no_endpoint["nodes"][0]["readiness"]
        with self.assertRaisesRegex(PlanError, "leader needs HTTP readiness"):
            load_plan(self.write_plan(no_endpoint))

        direct = copy.deepcopy(self.plan_data)
        del direct["gateway"]
        plan = load_plan(self.write_plan(direct))
        hosts = load_hosts(self.write_hosts(), plan)
        self.assertIn(
            "Endpoint: http://192.0.2.10:7100",
            format_topology_summary(plan, hosts),
        )

    def test_hosts_exactly_match_nodes_and_have_required_text(self) -> None:
        plan = load_plan(self.write_plan())
        hosts = load_hosts(self.write_hosts(), plan)
        self.assertEqual(hosts["node0"].interface, "eth0")
        self.assertIsNone(hosts["node1"].interface)

        missing = copy.deepcopy(self.hosts_data)
        del missing["hosts"]["node1"]
        with self.assertRaisesRegex(PlanError, r"missing=\['node1'\]"):
            load_hosts(self.write_hosts(missing), plan)

        invalid = copy.deepcopy(self.hosts_data)
        invalid["hosts"]["node0"]["address"] = ""
        with self.assertRaisesRegex(PlanError, r"hosts\.node0\.address"):
            load_hosts(self.write_hosts(invalid), plan)

        invalid_version = copy.deepcopy(self.hosts_data)
        invalid_version["version"] = 1.0
        with self.assertRaisesRegex(PlanError, "hosts version must be 1"):
            load_hosts(self.write_hosts(invalid_version), plan)

    def test_topology_summary_is_compact(self) -> None:
        plan = load_plan(self.write_plan())
        summary = format_topology_summary(plan, load_hosts(self.write_hosts(), plan))

        self.assertIn("Plan: phase2-plan (2 nodes, 2 NPUs/node)", summary)
        self.assertIn(
            "node0 @192.0.2.10%eth0: prefill, nodes/node0/run.sh "
            "ports=7100-7101",
            summary,
        )
        self.assertIn("Gateway: gateway/run.sh port=38085", summary)
        self.assertIn("Steps: checks=1, accuracy=1, performance=1", summary)


if __name__ == "__main__":
    unittest.main()
