from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts/recipe_ci/install_aisbench.sh"
CONSTRAINTS = ROOT / "scripts/recipe_ci/aisbench-constraints.txt"


class AisbenchInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.upstream = self.root / "upstream"
        self.cache_root = self.root / "cache"
        self.environment_file = self.root / "aisbench.env"
        self.constraints = self.root / "constraints.txt"
        self.constraints.write_text("\n", encoding="utf-8")

        self.upstream.mkdir()
        (self.upstream / "README.md").write_text("fixture\n", encoding="utf-8")

        subprocess.run(["git", "init", "-q", str(self.upstream)], check=True)
        subprocess.run(
            ["git", "-C", str(self.upstream), "config", "user.name", "Recipe CI"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.upstream),
                "config",
                "user.email",
                "recipe-ci@example.invalid",
            ],
            check=True,
        )
        subprocess.run(["git", "-C", str(self.upstream), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.upstream), "commit", "-qm", "fixture"],
            check=True,
        )
        self.commit = subprocess.check_output(
            ["git", "-C", str(self.upstream), "rev-parse", "HEAD"], text=True
        ).strip()
        subprocess.run(
            ["git", "-C", str(self.upstream), "tag", "fixture-tag"], check=True
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "AIS_BENCH_CACHE_ROOT": str(self.cache_root),
                "AIS_BENCH_CONSTRAINTS": str(self.constraints),
                "AIS_BENCH_EXPECTED_COMMIT": self.commit,
                "AIS_BENCH_PYTHON": sys.executable,
                "AIS_BENCH_TAG": "fixture-tag",
                "AIS_BENCH_URL": str(self.upstream),
                "PIP_NO_BUILD_ISOLATION": "1",
                "PIP_NO_INDEX": "1",
            }
        )
        return environment

    def run_installer(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(INSTALLER), "--env-file", str(self.environment_file)],
            env=self.environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def expected_key(self) -> str:
        constraints_hash = hashlib.sha256(
            self.constraints.read_bytes()
        ).hexdigest()[:16]
        return (
            f"{self.commit}-py{sys.version_info.major}.{sys.version_info.minor}-"
            f"{platform.machine()}-{constraints_hash}"
        )

    def populate_valid_cache(self) -> Path:
        cache_directory = self.cache_root / self.expected_key()
        source = cache_directory / "source"
        command = cache_directory / "venv/bin/ais_bench"
        source.parent.mkdir(parents=True)
        subprocess.run(
            ["git", "clone", "-q", str(self.upstream), str(source)], check=True
        )
        command.parent.mkdir(parents=True)
        command.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        command.chmod(0o755)
        (cache_directory / "READY").write_text(
            self.expected_key() + "\n", encoding="utf-8"
        )
        return cache_directory

    def output_environment(self) -> dict[str, str]:
        return dict(
            line.split("=", 1)
            for line in self.environment_file.read_text(encoding="utf-8").splitlines()
        )

    def test_installer_is_executable_and_uses_the_constraints_file(self) -> None:
        self.assertTrue(os.access(INSTALLER, os.X_OK))
        self.assertIn("--constraint", INSTALLER.read_text(encoding="utf-8"))
        self.assertEqual(
            CONSTRAINTS.read_text(encoding="utf-8").splitlines()[-1],
            "opencv-python-headless==4.11.0.86",
        )

    def test_cache_key_includes_every_runtime_input(self) -> None:
        expected_key = self.expected_key()
        self.populate_valid_cache()

        result = self.run_installer()

        self.assertEqual(result.returncode, 0, result.stdout)
        output = self.output_environment()
        self.assertEqual(output["RECIPE_AISBENCH_CACHE_KEY"], expected_key)
        self.assertEqual(
            Path(output["RECIPE_AISBENCH_BIN"]),
            self.cache_root / expected_key / "venv/bin/ais_bench",
        )
        self.assertEqual(
            Path(output["RECIPE_AISBENCH_ROOT"]),
            self.cache_root / expected_key / "source",
        )
        self.assertTrue(os.access(output["RECIPE_AISBENCH_BIN"], os.X_OK))
        self.assertEqual(
            (self.cache_root / expected_key / "READY").read_text(
                encoding="utf-8"
            ).strip(),
            expected_key,
        )

    def test_valid_cache_is_reused_without_installing_again(self) -> None:
        self.populate_valid_cache()
        first = self.run_installer()
        self.assertEqual(first.returncode, 0, first.stdout)
        output = self.output_environment()
        marker = Path(output["RECIPE_AISBENCH_BIN"])
        first_mtime = marker.stat().st_mtime_ns

        second = self.run_installer()

        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertIn("Reusing AISBench cache", second.stdout)
        self.assertEqual(marker.stat().st_mtime_ns, first_mtime)

    def test_installation_is_verified_before_atomic_publication(self) -> None:
        text = INSTALLER.read_text(encoding="utf-8")

        self.assertIn("mktemp -d", text)
        self.assertIn("flock 9", text)
        self.assertIn("_ais_bench_entrypoint.py", text)
        self.assertNotIn('venv/bin/ais_bench.py"', text)
        self.assertIn('"$staging_directory/venv/bin/ais_bench" -h', text)
        self.assertIn('mv "$staging_directory" "$cache_directory"', text)
        self.assertLess(
            text.index('"$staging_directory/venv/bin/ais_bench" -h'),
            text.index('mv "$staging_directory" "$cache_directory"'),
        )
        self.assertNotIn("--editable", text)

    def test_clone_is_pinned_without_runtime_retry_logic(self) -> None:
        text = INSTALLER.read_text(encoding="utf-8")

        self.assertIn("git -c http.version=HTTP/1.1 clone", text)
        self.assertNotIn("for attempt", text)
        self.assertIn(
            'actual_commit=$(git -C "$staging_directory/source" rev-parse HEAD)',
            text,
        )
        self.assertIn("AIS_BENCH_EXPECTED_COMMIT", text)


if __name__ == "__main__":
    unittest.main()
