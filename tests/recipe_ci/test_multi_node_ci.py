from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.recipe_ci.plan import PlanError, load_hosts, load_plan  # noqa: E402


EXAMPLE = ROOT / "configs/recipe_ci/plans/deepseek-v2-lite-pd-2n2c"
GENERIC_DP_EXAMPLE = ROOT / "configs/recipe_ci/plans/qwen3-30b-a3b-dp-2n2c"


def free_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


class PlanTests(unittest.TestCase):
    def test_example_has_two_independent_two_instance_nodes(self) -> None:
        plan = load_plan(EXAMPLE / "plan.yaml")

        self.assertEqual(plan.name, "deepseek-v2-lite-pd-2n2c")
        self.assertEqual(plan.leader.id, "prefill")
        self.assertEqual([node.index for node in plan.nodes], [0, 1])
        self.assertEqual([node.readiness.count for node in plan.nodes], [2, 2])
        self.assertEqual(
            [node.launch for node in plan.nodes],
            ["nodes/prefill/run.sh", "nodes/decode/run.sh"],
        )
        self.assertEqual(plan.gateway.port, 38085)
        self.assertEqual(len(plan.evaluations.accuracy), 1)
        self.assertEqual(len(plan.evaluations.performance), 1)

    def test_each_node_requires_its_own_launch_script(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "example"
            shutil.copytree(EXAMPLE, copied)
            raw = yaml.safe_load((copied / "plan.yaml").read_text(encoding="utf-8"))
            raw["nodes"][1]["launch"] = "nodes/prefill/run.sh"
            (copied / "plan.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")

            with self.assertRaisesRegex(PlanError, "each node must have its own"):
                load_plan(copied / "plan.yaml")

    def test_generic_dp_example_has_one_four_rank_group(self) -> None:
        plan = load_plan(GENERIC_DP_EXAMPLE / "plan.yaml")
        node0_run = (GENERIC_DP_EXAMPLE / plan.nodes[0].launch).read_text(
            encoding="utf-8"
        )
        node1_run = (GENERIC_DP_EXAMPLE / plan.nodes[1].launch).read_text(
            encoding="utf-8"
        )

        self.assertEqual(plan.name, "qwen3-30b-a3b-dp-2n2c")
        self.assertEqual([node.readiness.count for node in plan.nodes], [2, 2])
        self.assertIn("--dp-size 4", node0_run)
        self.assertIn("--dp-rank-start 0", node0_run)
        self.assertIn("--dp-rank-start 2", node1_run)
        self.assertIn('--dp-address "$RECIPE_NODE_0_IP"', node1_run)

    def test_hosts_must_match_plan_nodes(self) -> None:
        plan = load_plan(EXAMPLE / "plan.yaml")
        hosts = load_hosts(EXAMPLE / "hosts.example.yaml", plan)

        self.assertEqual(set(hosts), {"prefill", "decode"})
        self.assertEqual(hosts["prefill"].interface, "eth0")


class LocalRunnerTests(unittest.TestCase):
    def test_two_nodes_complete_gateway_check_and_accuracy_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan_dir = Path(directory)
            control_port = free_port()
            prefill_port = free_port()
            decode_port = free_port()
            gateway_port = free_port()
            self._write_fake_runtime(plan_dir)
            self._write_fake_plan(plan_dir, prefill_port, decode_port, gateway_port)

            hosts_data = {
                "version": 1,
                "hosts": {
                    "prefill": {"address": "127.0.0.1", "interface": "lo"},
                    "decode": {"address": "127.0.0.1", "interface": "lo"},
                },
            }
            hosts_path = plan_dir / "hosts.yaml"
            hosts_path.write_text(
                yaml.safe_dump(hosts_data, sort_keys=False), encoding="utf-8"
            )

            artifact_root = plan_dir / "artifacts"
            command = [
                sys.executable,
                str(ROOT / "scripts/recipe_ci/runner.py"),
                "--plan",
                str(plan_dir / "plan.yaml"),
                "--hosts",
                str(hosts_path),
                "--vllm-ascend-root",
                str(plan_dir / "vllm-ascend"),
                "--control-port",
                str(control_port),
                "--startup-timeout-seconds",
                "20",
                "--run-timeout-seconds",
                "20",
                "--artifact-root",
                str(artifact_root),
                "--evaluation",
                "accuracy",
            ]
            leader = subprocess.Popen(
                [*command, "--node-id", "prefill"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            worker = subprocess.Popen(
                [*command, "--node-id", "decode"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            leader_output, _ = leader.communicate(timeout=30)
            worker_output, _ = worker.communicate(timeout=30)
            worker_log = (
                artifact_root / "local-runner-test/decode/service.log"
            ).read_text(encoding="utf-8")

            self.assertEqual(
                leader.returncode,
                0,
                f"{leader_output}\nworker output:\n{worker_output}\nworker log:\n{worker_log}",
            )
            self.assertEqual(worker.returncode, 0, worker_output)
            self.assertIn("local backends ready", leader_output)
            self.assertIn("starting gateway", leader_output)
            self.assertIn("plan completed", leader_output)
            self.assertIn("plan completed", worker_output)
            leader_artifacts = artifact_root / "local-runner-test" / "prefill"
            self.assertTrue((leader_artifacts / "checks/health.log").is_file())
            self.assertEqual(
                (leader_artifacts / "accuracy/result.txt").read_text(encoding="utf-8"),
                "accuracy-ran\n",
            )

    @staticmethod
    def _write_fake_runtime(plan_dir: Path) -> None:
        required = (
            "examples/external_online_dp/launch_online_dp.py",
            "examples/external_online_dp/dp_load_balance_proxy_server.py",
            "examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py",
        )
        for relative_path in required:
            path = plan_dir / "vllm-ascend" / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# fake runtime tool\n", encoding="utf-8")

    @staticmethod
    def _write_fake_plan(
        plan_dir: Path,
        prefill_port: int,
        decode_port: int,
        gateway_port: int,
    ) -> None:
        for directory in (
            "nodes/prefill",
            "nodes/decode",
            "gateway",
            "checks",
            "evaluations",
        ):
            (plan_dir / directory).mkdir(parents=True)

        (plan_dir / "fake_service.py").write_text(
            """from http.server import BaseHTTPRequestHandler, HTTPServer
import sys

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass

HTTPServer((sys.argv[1], int(sys.argv[2])), Handler).serve_forever()
""",
            encoding="utf-8",
        )
        service_script = (
            'exec python3 "$RECIPE_PLAN_DIR/fake_service.py" '
            '"$RECIPE_LOCAL_IP" "$RECIPE_SERVICE_PORT_START"\n'
        )
        (plan_dir / "nodes/prefill/run.sh").write_text(service_script, encoding="utf-8")
        (plan_dir / "nodes/decode/run.sh").write_text(service_script, encoding="utf-8")
        (plan_dir / "gateway/run.sh").write_text(
            'exec python3 "$RECIPE_PLAN_DIR/fake_service.py" '
            '"$RECIPE_LOCAL_IP" "$RECIPE_GATEWAY_PORT"\n',
            encoding="utf-8",
        )
        (plan_dir / "checks/health.sh").write_text(
            "python3 -c 'import os, urllib.request; "
            'urllib.request.urlopen(os.environ["RECIPE_ENDPOINT"] + "/healthcheck")\'\n',
            encoding="utf-8",
        )
        (plan_dir / "evaluations/accuracy.sh").write_text(
            'echo accuracy-ran > "$RECIPE_ARTIFACT_DIR/result.txt"\n',
            encoding="utf-8",
        )

        plan_data = {
            "api_version": "recipe-ci/v1",
            "kind": "MultiNodePlan",
            "metadata": {"name": "local-runner-test"},
            "model": {"id": "fake/model", "served_name": "fake"},
            "nodes": [
                {
                    "id": "prefill",
                    "launch": "nodes/prefill/run.sh",
                    "readiness": {"port_start": prefill_port},
                },
                {
                    "id": "decode",
                    "launch": "nodes/decode/run.sh",
                    "readiness": {"port_start": decode_port},
                },
            ],
            "gateway": {"launch": "gateway/run.sh", "port": gateway_port},
            "checks": [
                {"id": "health", "script": "checks/health.sh", "timeout_seconds": 5}
            ],
            "evaluations": {
                "accuracy": [
                    {
                        "id": "accuracy",
                        "script": "evaluations/accuracy.sh",
                        "timeout_seconds": 5,
                    }
                ]
            },
        }
        (plan_dir / "plan.yaml").write_text(
            yaml.safe_dump(plan_data, sort_keys=False), encoding="utf-8"
        )


if __name__ == "__main__":
    unittest.main()
