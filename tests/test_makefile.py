"""Tests for Makefile targets with spaced and boundary DEV_VENV values."""

import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestMakefile(unittest.TestCase):
    def test_spaced_dev_venv_tokens_quoted(self):
        with tempfile.TemporaryDirectory(prefix="spaced path ") as tmpdir:
            spaced_venv = str(Path(tmpdir) / ".venv")
            result = subprocess.run(
                ["make", "-n", "bootstrap", "python-env", "lint", f"DEV_VENV={spaced_venv}"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            found = False
            for raw_line in result.stdout.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                if line.endswith("\\"):
                    line = line[:-1].rstrip()
                tokens = shlex.split(line)
                if spaced_venv in tokens:
                    found = True
                    break
            self.assertTrue(found, f"Expected token {spaced_venv!r} not found in make -n output lines")

    def test_invalid_dev_venv_rejected_by_guard(self):
        dry_run = subprocess.run(
            ["make", "-n", "bootstrap", "DEV_VENV=/tmp/x/notvenv"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        self.assertIn('case "/tmp/x/notvenv" in .venv|*/.venv)', dry_run.stdout)
        self.assertIn("DEV_VENV must name a .venv directory, not /tmp/x/notvenv", dry_run.stdout)

        run_res = subprocess.run(
            ["make", "bootstrap", "DEV_VENV=/tmp/x/notvenv"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(run_res.returncode, 0)
        self.assertIn("must name a .venv directory", run_res.stderr)

    def test_boundary_dot_venv_accepted_by_guard(self):
        result = subprocess.run(
            ["make", "-n", "bootstrap", "DEV_VENV=.venv"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        self.assertIn('case ".venv" in .venv|*/.venv)', result.stdout)


if __name__ == "__main__":
    unittest.main()
