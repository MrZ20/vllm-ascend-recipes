from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
MANUAL_WORKFLOW = ROOT / ".github/workflows/recipe_verify_multi_node.yaml"
REUSABLE_WORKFLOW = ROOT / ".github/workflows/_recipe_verify_multi_node.yaml"
LWS_TEMPLATE = ROOT / "scripts/recipe_ci/k8s/lws.yaml.jinja2"
RUN_SCRIPT = ROOT / "scripts/recipe_ci/run.sh"


class MultiNodeWorkflowTests(unittest.TestCase):
    def test_manual_workflow_only_selects_and_calls_the_reusable_workflow(self) -> None:
        text = MANUAL_WORKFLOW.read_text(encoding="utf-8")
        value = yaml.load(text, Loader=yaml.BaseLoader)

        self.assertEqual(set(value["on"]), {"workflow_dispatch"})
        inputs = value["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(
            set(inputs),
            {
                "plan",
                "image",
                "evaluation",
                "ref",
                "startup_timeout_seconds",
                "run_timeout_seconds",
            },
        )
        self.assertEqual(set(value["jobs"]), {"recipe-ci"})
        job = value["jobs"]["recipe-ci"]
        self.assertEqual(job["uses"], "./.github/workflows/_recipe_verify_multi_node.yaml")
        self.assertIn("secrets.KUBECONFIG_B64", text)
        self.assertNotIn("kubectl", text)
        self.assertNotIn("LeaderWorkerSet", text)

    def test_reusable_workflow_derives_and_manages_every_plan_node(self) -> None:
        text = REUSABLE_WORKFLOW.read_text(encoding="utf-8")
        value = yaml.load(text, Loader=yaml.BaseLoader)

        self.assertEqual(set(value["on"]), {"workflow_call"})
        self.assertEqual(
            set(value["on"]["workflow_call"]["inputs"]),
            {
                "plan",
                "image",
                "evaluation",
                "ref",
                "startup_timeout_seconds",
                "run_timeout_seconds",
            },
        )
        self.assertEqual(set(value["jobs"]), {"recipe-ci"})
        self.assertIn("kubectl apply", text)
        self.assertIn("kubectl delete", text)
        self.assertIn("scripts/recipe_ci/k8s/lws.yaml.jinja2", text)
        self.assertIn("$run_root/source/", text)
        self.assertIn("inputs.ref || github.sha", text)
        self.assertIn("git rev-parse HEAD", text)
        self.assertIn("len(load_plan", text)
        self.assertIn("/tmp/recipe-ci-pods.txt", text)
        self.assertIn('for index in "${!pods[@]}"', text)
        self.assertNotIn("LEADER_POD", text)
        self.assertNotIn("WORKER_POD", text)

        # Pod placement, addresses, and visible devices are supplied by LWS/K8s,
        # rather than duplicated as per-node GitHub runner configuration.
        self.assertNotIn("RECIPE_CI_HOSTS_YAML", text)
        self.assertNotIn("NODE0_RUNNER_LABELS", text)
        self.assertNotIn("NODE1_RUNNER_LABELS", text)
        self.assertNotIn("NODE0_DEVICES", text)
        self.assertNotIn("NODE1_DEVICES", text)
        self.assertNotRegex(text, r"\b(?:10|172|192)\.\d+\.\d+\.\d+\b")

    def test_lws_template_supports_more_than_two_identical_recipe_ci_nodes(self) -> None:
        text = LWS_TEMPLATE.read_text(encoding="utf-8")
        replacements = {
            "lws_name": "recipe-deepseek-v4-123-1",
            "namespace": "vllm-project",
            "image": "example.invalid/vllm-ascend:test-a3",
            "commit": "0123456789abcdef0123456789abcdef01234567",
            "run_root": "/root/.cache/recipe-ci/123-1",
            "plan": "configs/recipe_ci/plans/deepseek-v4-flash-a3-pd/plan.yaml",
            "model_path": "/root/.cache/models/deepseek-v4",
            "evaluation": "none",
            "node_count": "4",
            "startup_timeout_seconds": "3600",
            "run_timeout_seconds": "14400",
            "pvc_name": "recipe-ci-pvc",
            "aisbench_dataset_dir": "/root/.cache/datasets/gsm8k",
        }
        for name, replacement in replacements.items():
            text = re.sub(r"{{\s*" + re.escape(name) + r"\s*}}", replacement, text)
        self.assertNotIn("{{", text)

        resources = list(yaml.safe_load_all(text))
        self.assertEqual([item["kind"] for item in resources], ["LeaderWorkerSet", "Service"])
        lws = resources[0]
        template = lws["spec"]["leaderWorkerTemplate"]
        self.assertEqual(template["size"], 4)

        leader = template["leaderTemplate"]["spec"]["containers"][0]
        worker = template["workerTemplate"]["spec"]["containers"][0]
        self.assertEqual(leader["command"], worker["command"])
        self.assertTrue(leader["command"][1].endswith("/scripts/recipe_ci/run.sh"))
        self.assertEqual(leader["env"], worker["env"])
        self.assertEqual(leader["resources"], worker["resources"])
        self.assertEqual(leader["resources"]["requests"]["huawei.com/ascend-1980"], 16)
        self.assertEqual(leader["resources"]["limits"]["huawei.com/ascend-1980"], 16)
        env = {item["name"]: item["value"] for item in leader["env"]}
        self.assertEqual(env["RECIPE_CI_NODE_COUNT"], "4")
        self.assertEqual(env["RECIPE_CI_INSTALL_AISBENCH"], "true")

    def test_one_run_script_accepts_local_ips_or_lws_dns(self) -> None:
        text = RUN_SCRIPT.read_text(encoding="utf-8")

        self.assertIn('node_id="node${LWS_WORKER_INDEX}"', text)
        self.assertIn("RECIPE_CI_CLUSTER_IPS", text)
        self.assertIn("LWS_LEADER_ADDRESS", text)
        self.assertIn("npu-smi info", text)
        self.assertIn('python3 "$SCRIPT_DIR/runner.py"', text)
        self.assertFalse((ROOT / "scripts/recipe_ci/k8s/run_node.sh").exists())
        self.assertNotIn("pytest", text)
        self.assertNotIn("pkill", text)
        self.assertNotIn("killall", text)

    def test_common_run_script_can_validate_without_cluster_or_npu(self) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "RECIPE_CI_PLAN": (
                    "configs/recipe_ci/plans/deepseek-v4-flash-a3-pd/plan.yaml"
                ),
                "RECIPE_CI_VALIDATE_ONLY": "true",
            }
        )
        result = subprocess.run(
            ["bash", str(RUN_SCRIPT)],
            cwd=ROOT,
            env=environment,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertIn("Plan: deepseek-v4-flash-a3-pd", result.stdout)


if __name__ == "__main__":
    unittest.main()
