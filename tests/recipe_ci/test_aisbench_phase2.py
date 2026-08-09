from __future__ import annotations

import argparse
import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.recipe_ci.aisbench import (  # noqa: E402
    accuracy_score,
    performance_metrics,
    preflight,
    render_model_config,
)


class AisbenchResultTests(unittest.TestCase):
    def test_preflight_checks_command_config_dataset_and_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = root / "ais_bench"
            command.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            command.chmod(0o755)
            config = root / "model.py"
            config.write_text("models = []\n", encoding="utf-8")
            dataset = root / "dataset"
            dataset.mkdir()
            artifact = root / "artifact"
            args = argparse.Namespace(
                command=str(command),
                model_config=config,
                dataset_directory=dataset,
                artifact_directory=artifact,
            )
            environment = {
                "RECIPE_ENDPOINT_HOST": "127.0.0.1",
                "RECIPE_ENDPOINT_PORT": "8000",
                "RECIPE_MODEL_PATH": "/models/fake",
                "RECIPE_SERVED_MODEL_NAME": "fake",
            }

            with mock.patch.dict(os.environ, environment, clear=False):
                preflight(args)

            self.assertTrue(artifact.is_dir())

    def test_model_config_template_is_rendered_with_runtime_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "model.py"
            output = root / "rendered/model.py"
            template.write_text(
                "models = [dict("
                "path=__RECIPE_MODEL_PATH__, "
                "model=__RECIPE_SERVED_MODEL_NAME__, "
                "host_ip=__RECIPE_ENDPOINT_HOST__, "
                "host_port=__RECIPE_ENDPOINT_PORT__)]\n",
                encoding="utf-8",
            )
            environment = {
                "RECIPE_MODEL_PATH": "/models/fake",
                "RECIPE_SERVED_MODEL_NAME": "fake",
                "RECIPE_ENDPOINT_HOST": "10.0.0.8",
                "RECIPE_ENDPOINT_PORT": "38085",
            }

            with mock.patch.dict(os.environ, environment, clear=False):
                render_model_config(template, output)

            model = runpy.run_path(str(output))["models"][0]
            self.assertEqual(model["path"], "/models/fake")
            self.assertEqual(model["model"], "fake")
            self.assertEqual(model["host_ip"], "10.0.0.8")
            self.assertEqual(model["host_port"], 38085)

    def test_accuracy_summary_is_translated_and_gated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory)
            summary = (
                artifact
                / "outputs/default/20260809_142027/summary/summary_20260809.csv"
            )
            summary.parent.mkdir(parents=True)
            summary.write_text(
                "dataset,version,metric,mode,total_count,recipe-ci-vllm\n"
                "gsm8k,1,accuracy,gen,8,82.00 (7/8)\n",
                encoding="utf-8",
            )
            score, source = accuracy_score(artifact)
            self.assertEqual(score, 82.0)
            self.assertEqual(source, summary)

            result_file = artifact / "result.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/recipe_ci/aisbench.py"),
                    "accuracy",
                    "--artifact-directory",
                    str(artifact),
                    "--result-file",
                    str(result_file),
                    "--baseline",
                    "85",
                    "--allowed-drop",
                    "2",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            self.assertEqual(result.returncode, 1)
            value = json.loads(result_file.read_text(encoding="utf-8"))
            self.assertEqual(value["status"], "failed")
            self.assertEqual(value["mode"], "gate")
            self.assertEqual(value["metrics"]["accuracy"], 82.0)

    def test_performance_json_and_csv_are_translated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory)
            performance = (
                artifact
                / "outputs/default/20260809_142027/performances/recipe-ci-vllm"
            )
            performance.mkdir(parents=True)
            (performance / "gsm8k.json").write_text(
                json.dumps(
                    {
                        "Request Throughput": {"total": "0.2665 req/s"},
                        "Output Token Throughput": {"total": "8.529 token/s"},
                    }
                ),
                encoding="utf-8",
            )
            (performance / "gsm8k.csv").write_text(
                "Performance Parameters,Stage,Average,Min,Max,Median,P75,P90,P99,N\n"
                "E2EL,total,7503.7 ms,1,2,3,4,5,6,2\n"
                "TTFT,total,100.5 ms,1,2,3,4,5,6,2\n"
                "TPOT,total,20.25 ms,1,2,3,4,5,6,2\n",
                encoding="utf-8",
            )

            metrics, sources = performance_metrics(artifact)

            self.assertEqual(metrics["request_per_second"], 0.2665)
            self.assertEqual(metrics["output_token_per_second"], 8.529)
            self.assertEqual(metrics["e2e_latency_ms"], 7503.7)
            self.assertEqual(metrics["ttft_ms"], 100.5)
            self.assertEqual(metrics["tpot_ms"], 20.25)
            self.assertEqual(
                {path.name for path in sources},
                {"gsm8k.json", "gsm8k.csv"},
            )

    def test_performance_parser_rejects_legacy_fuzzy_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory)
            (artifact / "performance.json").write_text(
                json.dumps({"Request Throughput": "1 req/s"}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "run directory not found"):
                performance_metrics(artifact)


if __name__ == "__main__":
    unittest.main()
