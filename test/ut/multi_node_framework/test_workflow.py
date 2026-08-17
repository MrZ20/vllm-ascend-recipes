from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
MANUAL_WORKFLOW = ROOT / ".github/workflows/verify_multi_node.yaml"
REUSABLE_WORKFLOW = ROOT / ".github/workflows/_verify_multi_node.yaml"
LWS_TEMPLATE = ROOT / "test/recipe/multi_node/scripts/k8s/lws.yaml.tmpl"
LWS_RENDERER = ROOT / "test/recipe/multi_node/scripts/k8s/render_lws.py"
LWS_ADAPTER = ROOT / "test/recipe/multi_node/scripts/k8s/run_lws.sh"
RUN_SCRIPT = ROOT / "test/recipe/multi_node/scripts/run.sh"


class MultiNodeWorkflowTests(unittest.TestCase):
    def test_entry_workflow_runs_the_lightweight_plan_manually_and_on_prs(self) -> None:
        text = MANUAL_WORKFLOW.read_text(encoding="utf-8")
        value = yaml.load(text, Loader=yaml.BaseLoader)

        self.assertEqual(set(value["on"]), {"pull_request", "workflow_dispatch"})
        self.assertEqual(value["on"]["pull_request"]["branches"], ["main"])
        self.assertEqual(value["on"]["workflow_dispatch"], "")
        self.assertEqual(set(value["jobs"]), {"verify"})
        job = value["jobs"]["verify"]
        self.assertEqual(job["name"], "multi-node (${{ matrix.name }})")
        self.assertEqual(job["uses"], "./.github/workflows/_verify_multi_node.yaml")
        self.assertEqual(
            job["strategy"]["matrix"]["include"],
            [
                {
                    "name": "deepseek-v2-lite-pd-2n2c",
                    "plan": (
                        "test/recipe/multi_node/configs/"
                        "deepseek-v2-lite-pd-2n2c/plan.yaml"
                    ),
                },
            ],
        )
        self.assertEqual(job["with"], {"plan": "${{ matrix.plan }}"})
        self.assertIn("github.event.pull_request.head.repo.full_name", job["if"])
        self.assertIn("secrets.KUBECONFIG_B64", text)
        self.assertEqual(
            job["secrets"],
            {
                "KUBECONFIG_B64": "${{ secrets.KUBECONFIG_B64 }}",
                "OBS_AK": "${{ secrets.OBS_AK }}",
                "OBS_SK": "${{ secrets.OBS_SK }}",
            },
        )
        self.assertNotIn("model_path", text)
        self.assertNotIn("evaluation", text)
        self.assertNotIn("timeout_seconds", text)
        self.assertNotIn("kubectl", text)
        self.assertNotIn("LeaderWorkerSet", text)

    def test_reusable_workflow_derives_and_manages_every_plan_node(self) -> None:
        text = REUSABLE_WORKFLOW.read_text(encoding="utf-8")
        value = yaml.load(text, Loader=yaml.BaseLoader)

        self.assertEqual(set(value["on"]), {"workflow_call"})
        self.assertEqual(
            set(value["on"]["workflow_call"]["inputs"]),
            {"plan"},
        )
        self.assertEqual(
            set(value["on"]["workflow_call"]["secrets"]),
            {"KUBECONFIG_B64", "OBS_AK", "OBS_SK"},
        )
        self.assertEqual(
            value["on"]["workflow_call"]["secrets"]["OBS_AK"]["required"],
            "false",
        )
        self.assertEqual(
            value["on"]["workflow_call"]["secrets"]["OBS_SK"]["required"],
            "false",
        )
        self.assertEqual(set(value["jobs"]), {"run"})
        job = value["jobs"]["run"]
        self.assertEqual(job["name"], "verify")
        self.assertNotIn("CONTROLLER_IMAGE", job["env"])
        self.assertIn('"image": os.environ["RUNTIME_IMAGE"]', text)
        self.assertIn("kubectl apply", text)
        self.assertIn("kubectl delete", text)
        self.assertIn("test/recipe/multi_node/scripts/k8s/lws.yaml.tmpl", text)
        self.assertIn("test/recipe/multi_node/scripts/k8s/render_lws.py", text)
        self.assertNotIn("lws.yaml.jinja2", text)
        self.assertIn('"$MULTI_NODE_RUN_ROOT/source/"', text)
        self.assertNotIn("inputs.ref", text)
        self.assertNotIn("git rev-parse HEAD", text)
        self.assertIn("len(plan.nodes)", text)
        self.assertIn("plan.resources.npu_per_node", text)
        self.assertIn("python3 -m pip install pyyaml", text)
        self.assertNotIn("- name: Prepare AISBench", text)
        self.assertIn("linux-aarch64-a2b4-1", text)
        self.assertIn("vllm-ascend-vllm-ascend-recipes", text)
        self.assertIn("vllm-ascend-vllm-ascend-recipes-gy001", text)
        self.assertEqual(job["timeout-minutes"], "180")
        self.assertIn("POD_START_TIMEOUT_SECONDS: 900", text)
        self.assertIn("STARTUP_TIMEOUT_SECONDS: 1800", text)
        self.assertIn("RUN_TIMEOUT_SECONDS: 7200", text)
        self.assertEqual(job["env"]["MULTI_NODE_UPLOAD_K8S_DIAGNOSTICS"], "false")
        self.assertNotIn("/proc/sys/kernel/random/uuid", text)
        self.assertIn('unique_suffix="${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"', text)
        self.assertNotIn('lws_name="recipe-', text)
        self.assertNotIn("vars.MULTI_NODE_", text)
        self.assertNotIn("MULTI_NODE_MODEL_PATH", text)
        self.assertNotIn("MULTI_NODE_EVALUATION", text)
        self.assertNotIn("MULTI_NODE_A3_", text)
        self.assertIn("/tmp/lws-pods.txt", text)
        self.assertNotIn("/tmp/multi-node-pods.txt", text)
        self.assertIn('"$MULTI_NODE_RUN_ROOT/pod-status/node${index}.exit"', text)
        self.assertNotIn("state.terminated.exitCode", text)
        self.assertIn('for index in "${!pods[@]}"', text)
        self.assertNotIn("LEADER_POD", text)
        self.assertNotIn("WORKER_POD", text)
        self.assertIn('kubectl logs -f "$pod"', text)
        self.assertIn('> >(sed -u "s/^/[node${index}] /") 2>&1 &', text)
        self.assertIn('wait "$pid"', text)
        self.assertIn("FAILURE_SETTLE_TIMEOUT_SECONDS: 120", text)
        self.assertIn('failure_settle_deadline=""', text)
        self.assertIn('"$all_finished" == true', text)
        self.assertNotIn('node_failed=true', text)
        self.assertIn("waiting up to ${FAILURE_SETTLE_TIMEOUT_SECONDS}s", text)
        self.assertIn("id: run_framework", text)
        self.assertIn('kubectl wait "${pod_resources[@]}"', text)
        self.assertIn('--for=create --timeout="${remaining}s"', text)
        self.assertIn('--for=condition=Ready --timeout="${remaining}s"', text)
        self.assertEqual(text.count('kubectl wait "${pod_resources[@]}"'), 2)
        self.assertNotIn("--timeout=20m", text)
        self.assertNotIn("failure-diagnostics.log", text)
        self.assertNotIn('| sed -u "s/^/[node${index}] /"', text)
        self.assertNotIn("controller-logs", text)
        self.assertNotIn("pod-logs", text)
        self.assertNotIn("/tmp/multi-node-node", text)
        self.assertIn("Print Kubernetes summary", text)
        self.assertIn("LeaderWorkerSet status", text)
        self.assertIn("Pod status", text)
        self.assertIn("Kubernetes event reasons", text)
        self.assertIn("Collect Kubernetes diagnostics for upload", text)
        self.assertIn("env.MULTI_NODE_UPLOAD_K8S_DIAGNOSTICS == 'true'", text)
        self.assertIn("final-lws.yaml", text)
        self.assertIn("final-pods.yaml", text)
        self.assertIn("events.log", text)
        self.assertIn('k8s_directory="$MULTI_NODE_BUNDLE/k8s"', text)
        self.assertIn('cp "$MULTI_NODE_RUN_ROOT/lws.yaml"', text)
        self.assertIn('cp "$MULTI_NODE_RUN_ROOT/lws-values.json"', text)
        self.assertNotIn("MESSAGE:.message", text)
        self.assertLess(
            text.index("Print Kubernetes summary"),
            text.index("Collect Kubernetes diagnostics for upload"),
        )
        self.assertLess(
            text.index("Collect Kubernetes diagnostics for upload"),
            text.index("Delete LeaderWorkerSet"),
        )

        # Pod placement, addresses, and visible devices are supplied by LWS/K8s,
        # rather than duplicated as per-node GitHub runner configuration.
        self.assertNotIn("MULTI_NODE_HOSTS_YAML", text)
        self.assertNotIn("NODE0_RUNNER_LABELS", text)
        self.assertNotIn("NODE1_RUNNER_LABELS", text)
        self.assertNotIn("NODE0_DEVICES", text)
        self.assertNotIn("NODE1_DEVICES", text)
        self.assertNotRegex(text, r"\b(?:10|172|192)\.\d+\.\d+\.\d+\b")

    def test_lws_template_supports_arbitrary_plan_node_count(self) -> None:
        values = {
            "lws_name": "recipe-deepseek-v4-123-1",
            "namespace": "vllm-project",
            "image": "example.invalid/vllm-ascend:test-a2",
            "run_root": "/root/.cache/multi-node/123-1",
            "plan": "test/recipe/multi_node/configs/deepseek-v2-lite-pd-2n2c/plan.yaml",
            "node_count": "4",
            "npu_per_node": "2",
            "startup_timeout_seconds": "1800",
            "run_timeout_seconds": "7200",
            "pvc_name": "multi-node-pvc",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values_path = root / "values.json"
            output_path = root / "lws.yaml"
            values_path.write_text(json.dumps(values), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(LWS_RENDERER),
                    "--template",
                    str(LWS_TEMPLATE),
                    "--values",
                    str(values_path),
                    "--output",
                    str(output_path),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            text = output_path.read_text(encoding="utf-8")
        self.assertNotIn("{{", text)

        resources = list(yaml.safe_load_all(text))
        self.assertEqual([item["kind"] for item in resources], ["LeaderWorkerSet"])
        lws = resources[0]
        template = lws["spec"]["leaderWorkerTemplate"]
        self.assertEqual(template["size"], 4)

        leader = template["leaderTemplate"]["spec"]["containers"][0]
        worker = template["workerTemplate"]["spec"]["containers"][0]
        self.assertEqual(leader["command"], worker["command"])
        self.assertEqual(leader["name"], "runner")
        self.assertEqual(worker["name"], "runner")
        self.assertEqual(leader["command"][:2], ["bash", "-c"])
        self.assertIn("/test/recipe/multi_node/scripts/k8s/run_lws.sh", leader["command"][2])
        self.assertIn(
            "pod-status/node${LWS_WORKER_INDEX}.exit", leader["command"][2]
        )
        self.assertIn("exec sleep infinity", leader["command"][2])
        self.assertEqual(leader["env"], worker["env"])
        self.assertEqual(leader["resources"], worker["resources"])
        self.assertEqual(leader["resources"]["requests"]["huawei.com/ascend-1980"], 2)
        self.assertEqual(leader["resources"]["limits"]["huawei.com/ascend-1980"], 2)
        self.assertEqual(leader["resources"]["requests"]["cpu"], 8)
        self.assertEqual(leader["resources"]["requests"]["memory"], "128Gi")
        self.assertTrue(template["leaderTemplate"]["spec"]["hostNetwork"])
        self.assertTrue(template["workerTemplate"]["spec"]["hostNetwork"])
        self.assertEqual(
            template["leaderTemplate"]["spec"]["dnsPolicy"],
            "ClusterFirstWithHostNet",
        )
        self.assertEqual(
            template["workerTemplate"]["spec"]["dnsPolicy"],
            "ClusterFirstWithHostNet",
        )
        self.assertEqual(
            template["leaderTemplate"]["spec"]["terminationGracePeriodSeconds"],
            30,
        )
        self.assertEqual(
            template["workerTemplate"]["spec"]["terminationGracePeriodSeconds"],
            30,
        )
        self.assertEqual(
            template["leaderTemplate"]["spec"]["nodeSelector"],
            {"node.kubernetes.io/npu.chip.name": "910B4"},
        )
        self.assertEqual(
            template["workerTemplate"]["spec"]["nodeSelector"],
            template["leaderTemplate"]["spec"]["nodeSelector"],
        )
        self.assertTrue(leader["securityContext"]["privileged"])
        self.assertNotIn(
            "nodeAffinity", template["leaderTemplate"]["spec"]["affinity"]
        )
        self.assertEqual(
            template["leaderTemplate"]["spec"]["tolerations"],
            template["workerTemplate"]["spec"]["tolerations"],
        )
        self.assertEqual(
            template["leaderTemplate"]["spec"]["tolerations"][0],
            {
                "key": "dedicated",
                "operator": "Equal",
                "value": "night",
                "effect": "NoSchedule",
            },
        )
        anti_affinity = template["leaderTemplate"]["spec"]["affinity"][
            "podAntiAffinity"
        ]["requiredDuringSchedulingIgnoredDuringExecution"][0]
        self.assertEqual(anti_affinity["topologyKey"], "kubernetes.io/hostname")
        self.assertEqual(
            anti_affinity["labelSelector"]["matchLabels"]["multi-node-run"],
            "recipe-deepseek-v4-123-1",
        )
        self.assertEqual(
            template["workerTemplate"]["metadata"]["labels"]["multi-node-run"],
            "recipe-deepseek-v4-123-1",
        )
        volumes = {
            item["name"]: item
            for item in template["leaderTemplate"]["spec"]["volumes"]
        }
        self.assertEqual(
            volumes["shared-volume"]["persistentVolumeClaim"]["claimName"],
            "multi-node-pvc",
        )
        self.assertEqual(
            volumes["driver-tools"]["hostPath"]["path"],
            "/usr/local/Ascend/driver",
        )
        self.assertEqual(volumes["worklogs"]["emptyDir"], {})
        self.assertEqual(volumes["shm-volume"]["emptyDir"]["sizeLimit"], "16Gi")
        env = {item["name"]: item["value"] for item in leader["env"]}
        self.assertEqual(env["MULTI_NODE_NODE_COUNT"], "4")
        self.assertEqual(env["MULTI_NODE_RUN_ROOT"], "/root/.cache/multi-node/123-1")
        self.assertNotIn("MULTI_NODE_INSTALL_AISBENCH", env)
        self.assertNotIn("MULTI_NODE_INSTALL_MOONCAKE", env)
        self.assertNotIn("AIS_BENCH_ROOT", env)
        self.assertNotIn("MULTI_NODE_AISBENCH_ROOT", env)
        self.assertEqual(
            env["AIS_BENCH_ENVIRONMENT_IDENTITY"],
            "runtime=example.invalid/vllm-ascend:test-a2",
        )
        self.assertNotIn("AIS_BENCH_URL", env)
        self.assertEqual(
            env["PIP_INDEX_URL"],
            "http://cache-service.nginx-pypi-cache.svc.cluster.local/pypi/simple",
        )
        self.assertEqual(
            env["PIP_TRUSTED_HOST"],
            "cache-service.nginx-pypi-cache.svc.cluster.local",
        )
        self.assertNotIn("MULTI_NODE_IMAGE", env)
        self.assertNotIn("GITHUB_SHA", env)
        self.assertNotIn("MULTI_NODE_VISIBLE_DEVICES", env)
        self.assertNotIn("VLLM_ASCEND_ROOT", env)
        self.assertNotIn("MULTI_NODE_AISBENCH_ACCURACY_DATASET_DIR", env)
        self.assertNotIn("MULTI_NODE_AISBENCH_PERFORMANCE_DATASET_DIR", env)
        self.assertNotIn("MULTI_NODE_INTERFACE", env)

        adapter = LWS_ADAPTER.read_text(encoding="utf-8")
        self.assertIn('bash "$SCRIPT_DIR/../install_aisbench.sh"', adapter)
        self.assertIn('--env-file "$aisbench_environment_tmp"', adapter)
        self.assertIn("if ((LWS_WORKER_INDEX == 0)); then", adapter)
        self.assertIn('while [[ ! -s "$aisbench_environment" ]]', adapter)
        self.assertIn(
            'mv "$aisbench_environment_tmp" "$aisbench_environment"', adapter
        )
        self.assertIn(
            "export MULTI_NODE_AISBENCH_BIN MULTI_NODE_AISBENCH_CACHE_KEY MULTI_NODE_AISBENCH_SOURCE",
            adapter,
        )
        self.assertLess(
            adapter.index("install_aisbench.sh"),
            adapter.index('exec bash "$SCRIPT_DIR/../run.sh"'),
        )

        workflow = REUSABLE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('kubectl delete leaderworkerset "$LWS_NAME"', workflow)
        self.assertIn("--ignore-not-found=true --wait=false", workflow)
        self.assertIn("tar -czf /tmp/multi-node-bundle.tar.gz", workflow)
        self.assertIn("uses: actions/checkout@v7", workflow)
        self.assertNotIn("uses: actions/checkout@v4", workflow)
        self.assertIn("uses: ascend-gha-runners/artifact/upload@v0.3", workflow)
        self.assertNotIn("obsutil", workflow)
        self.assertNotIn("OBS_BUCKET", workflow)
        self.assertNotIn("OBS_ENDPOINT", workflow)
        self.assertIn("uses: actions/upload-artifact@v7", workflow)
        self.assertNotIn("uses: actions/upload-artifact@v4", workflow)
        self.assertIn("compression-level: 0", workflow)
        self.assertNotIn("Neither OBS nor GitHub artifact upload succeeded", workflow)
        self.assertNotIn("Report Multi-node framework bundle upload status", workflow)
        self.assertIn("steps.upload_obs.outcome != 'success'", workflow)
        self.assertIn("steps.upload_obs.outcome == 'success'", workflow)

    def test_lws_aisbench_install_failure_is_broadcast_to_workers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory)
            fake_bin = run_root / "bin"
            fake_bin.mkdir()
            timeout = fake_bin / "timeout"
            timeout.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
[[ "${1:-}" != "--foreground" ]] || shift
[[ $# -gt 0 ]] || exit 2
shift
exec "$@"
""",
                encoding="utf-8",
            )
            timeout.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "LWS_LEADER_ADDRESS": "leader.group.namespace.svc",
                    "MULTI_NODE_NODE_COUNT": "2",
                    "MULTI_NODE_RUN_ROOT": str(run_root),
                    "MULTI_NODE_STARTUP_TIMEOUT_SECONDS": "30",
                    "PATH": f"{fake_bin}:{environment['PATH']}",
                }
            )
            environment.pop("AIS_BENCH_ENVIRONMENT_IDENTITY", None)
            environment.pop("MULTI_NODE_VALIDATE_ONLY", None)

            leader_environment = environment | {"LWS_WORKER_INDEX": "0"}
            leader = subprocess.run(
                ["bash", str(LWS_ADAPTER)],
                env=leader_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            failure_file = run_root / "aisbench.env.failed"
            self.assertEqual(leader.returncode, 1, leader.stdout)
            self.assertTrue(failure_file.is_file())
            self.assertIn(
                "AISBench preparation failed on node0 with exit code 1",
                failure_file.read_text(encoding="utf-8"),
            )

            worker_environment = environment | {"LWS_WORKER_INDEX": "1"}
            worker = subprocess.run(
                ["bash", str(LWS_ADAPTER)],
                env=worker_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            self.assertEqual(worker.returncode, 1, worker.stdout)
            self.assertIn("AISBench preparation failed on node0", worker.stdout)

    def test_common_run_script_uses_only_generic_local_runtime_inputs(self) -> None:
        text = RUN_SCRIPT.read_text(encoding="utf-8")
        adapter = LWS_ADAPTER.read_text(encoding="utf-8")

        self.assertIn('node_id="node${MULTI_NODE_NODE_INDEX}"', text)
        self.assertIn(': "${MULTI_NODE_CLUSTER_IPS:', text)
        self.assertNotIn("LWS_", text)
        self.assertIn("LWS_LEADER_ADDRESS", adapter)
        self.assertIn("LWS_WORKER_INDEX", adapter)
        self.assertIn("MULTI_NODE_STARTUP_TIMEOUT_SECONDS:-1800", adapter)
        self.assertEqual(adapter.count("startup_deadline="), 1)
        self.assertIn(
            "MULTI_NODE_STARTUP_TIMEOUT_SECONDS=$(remaining_startup_seconds)",
            adapter,
        )
        self.assertIn("awk 'NR == 1 {print $1}' || true", adapter)
        self.assertIn("Waiting for LWS DNS", adapter)
        self.assertIn("export MULTI_NODE_NODE_INDEX=$LWS_WORKER_INDEX", adapter)
        self.assertIn("export MULTI_NODE_CLUSTER_IPS", adapter)
        self.assertIn('exec bash "$SCRIPT_DIR/../run.sh"', adapter)
        self.assertNotIn("npu-smi info", text)
        self.assertIn('python3 -u "$SCRIPT_DIR/runner.py"', text)
        self.assertNotIn("install_mooncake", text)
        self.assertFalse((ROOT / "test/recipe/multi_node/scripts/k8s/run_node.sh").exists())
        self.assertNotIn("pytest", text)
        self.assertNotIn("pkill", text)
        self.assertNotIn("killall", text)

    def test_lws_renderer_rejects_missing_and_unexpected_values(self) -> None:
        sys.path.insert(0, str(LWS_RENDERER.parent))
        try:
            from render_lws import render_template
        finally:
            sys.path.pop(0)

        template = (
            "apiVersion: leaderworkerset.x-k8s.io/v1\n"
            "kind: LeaderWorkerSet\n"
            "metadata:\n"
            "  name: {{ name }}\n"
        )
        with self.assertRaisesRegex(ValueError, "missing LWS template values: name"):
            render_template(template, {})
        with self.assertRaisesRegex(
            ValueError, "unexpected LWS template values: extra"
        ):
            render_template(template, {"name": "multi-node", "extra": "value"})

    def test_lws_adapter_translates_dns_and_worker_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake_bin = Path(directory)
            getent = fake_bin / "getent"
            getent.write_text(
                "#!/usr/bin/env bash\n"
                "case \"$2\" in\n"
                "  recipe-case-0.group.namespace.svc.cluster.local) address=10.0.0.1 ;;\n"
                "  recipe-case-0-1.group.namespace) address=10.0.0.2 ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n"
                "printf '%s STREAM host\\n' \"$address\"\n",
                encoding="utf-8",
            )
            getent.chmod(0o755)
            python = fake_bin / "python3"
            python.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'node=%s ips=%s\\n' \"$MULTI_NODE_NODE_INDEX\" "
                '"$MULTI_NODE_CLUSTER_IPS"\n',
                encoding="utf-8",
            )
            python.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{fake_bin}:{environment['PATH']}",
                    "LWS_LEADER_ADDRESS": (
                        "recipe-case-0.group.namespace.svc.cluster.local"
                    ),
                    "LWS_WORKER_INDEX": "1",
                    "MULTI_NODE_NODE_COUNT": "2",
                    "MULTI_NODE_PLAN": "unused-by-fake-python.yaml",
                    "MULTI_NODE_VALIDATE_ONLY": "true",
                }
            )

            result = subprocess.run(
                ["bash", str(LWS_ADAPTER)],
                cwd=ROOT,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("node=1 ips=10.0.0.1,10.0.0.2", result.stdout)

    def test_common_run_script_can_validate_without_cluster_or_npu(self) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "MULTI_NODE_PLAN": (
                    "test/recipe/multi_node/configs/deepseek-v2-lite-pd-2n2c/plan.yaml"
                ),
                "MULTI_NODE_VALIDATE_ONLY": "true",
                "PATH": f"{Path(sys.executable).parent}:{environment['PATH']}",
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
        self.assertIn("Plan: deepseek-v2-lite-pd-2n2c", result.stdout)


if __name__ == "__main__":
    unittest.main()
