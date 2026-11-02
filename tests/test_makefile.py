"""Tests for Makefile targets with spaced, dollar, and boundary DEV_VENV values."""

import os
import shlex
import shutil
import subprocess  # nosec B404
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MAKE = shutil.which("make")


@unittest.skipUnless(MAKE, "make is not installed")
class TestMakefile(unittest.TestCase):
    def test_spaced_dev_venv_tokens_quoted(self):
        with tempfile.TemporaryDirectory(prefix="spaced path ") as tmpdir:
            spaced_venv = str(Path(tmpdir) / ".venv")
            # Fixed argument list, no shell, no untrusted input.
            run_res = subprocess.run(  # nosec B603
                [MAKE, "python-env", f"DEV_VENV={spaced_venv}"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertNotEqual(run_res.returncode, 0)
            self.assertIn(f"{spaced_venv}/bin/python is missing", run_res.stderr)

            dry_run = subprocess.run(  # nosec B603
                [MAKE, "-n", "bootstrap", "python-env", "lint", f"DEV_VENV={spaced_venv}"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            found_var = False
            found_subpath = False
            for raw_line in dry_run.stdout.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                if line.endswith("\\"):
                    line = line[:-1].rstrip()
                tokens = shlex.split(line)
                for tok in tokens:
                    if tok == "$DEV_VENV":
                        found_var = True
                    elif tok.startswith("$DEV_VENV/"):
                        found_subpath = True
            self.assertTrue(found_var, "Expected token '$DEV_VENV' not found in make -n output lines")
            self.assertTrue(
                found_subpath, "Expected token starting with '$DEV_VENV/' not found in make -n output lines"
            )

    def test_dollar_dev_venv_preserved(self):
        with tempfile.TemporaryDirectory(prefix="work$backup") as tmpdir:
            dollar_venv = str(Path(tmpdir) / ".venv")
            dollar_venv_make = dollar_venv.replace("$", "$$")

            cmd_res = subprocess.run(  # nosec B603
                [MAKE, "python-env", f"DEV_VENV={dollar_venv_make}"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertNotEqual(cmd_res.returncode, 0)
            self.assertIn(f"{dollar_venv}/bin/python is missing", cmd_res.stderr)

            env_res = subprocess.run(  # nosec B603
                [MAKE, "python-env"],
                cwd=REPO_ROOT,
                env={**os.environ, "DEV_VENV": dollar_venv},
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertNotEqual(env_res.returncode, 0)
            self.assertIn(f"{dollar_venv}/bin/python is missing", env_res.stderr)

    def test_invalid_dev_venv_rejected_by_guard(self):
        dry_run = subprocess.run(  # nosec B603
            [MAKE, "-n", "bootstrap", "DEV_VENV=/tmp/x/notvenv"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        self.assertIn('case "$DEV_VENV" in .venv|*/.venv)', dry_run.stdout)

        run_res = subprocess.run(  # nosec B603
            [MAKE, "bootstrap", "DEV_VENV=/tmp/x/notvenv"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(run_res.returncode, 0)
        self.assertIn("must name a .venv directory", run_res.stderr)
        self.assertIn("/tmp/x/notvenv", run_res.stderr)

    def test_boundary_dot_venv_accepted_by_guard(self):
        result = subprocess.run(  # nosec B603
            [MAKE, "-n", "bootstrap", "DEV_VENV=.venv"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        self.assertIn('case "$DEV_VENV" in .venv|*/.venv)', result.stdout)


if __name__ == "__main__":
    unittest.main()
