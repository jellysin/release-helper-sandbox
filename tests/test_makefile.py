"""Tests for Makefile targets with spaced, dollar, and boundary DEV_VENV values."""

import os
import shlex
import shutil
import subprocess  # nosec B404
import tempfile
import unittest
from os.path import realpath
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MAKE = shutil.which("make")
GIT = shutil.which("git")


def clean_env(**extra):
    """Return the environment without inherited make or git overrides, plus extra."""
    # A parent make or git hook would otherwise pin DEV_VENV or point git at the
    # enclosing repository.
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"DEV_VENV", "MAKEFLAGS", "MFLAGS", "MAKELEVEL"} and not k.startswith("GIT_")
    }
    env.update(extra)
    return env


def create_python3_stub(stub_dir):
    """Write an executable python3 stub that logs invocations and provisions a python stub upon venv creation."""
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub_py = stub_dir / "python3"
    stub_py.write_text(
        "#!/bin/sh\n"
        'echo "$*" >> "$STUB_LOG"\n'
        'if [ "$2" = "venv" ]; then\n'
        '  mkdir -p "$4/bin"\n'
        "  cat <<'EOF' > \"$4/bin/python\"\n"
        "#!/bin/sh\n"
        'echo "$*" >> "$STUB_LOG"\n'
        "exit 0\n"
        "EOF\n"
        '  chmod +x "$4/bin/python"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub_py.chmod(0o755)


def run_tool(tool, *args, cwd=REPO_ROOT, env=None, check=False):
    """Execute a CLI tool with a sanitized environment and return the completed process."""
    # Fixed make binary from shutil.which, argument list, no shell, test-controlled arguments.
    return subprocess.run(  # nosec B603  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        [tool, *args],
        cwd=cwd,
        env=clean_env() if env is None else env,
        capture_output=True,
        text=True,
        check=check,
        timeout=30,
    )


def run_make(*args, cwd=REPO_ROOT, env=None, check=False):
    """Run make with the given arguments under the sanitized environment."""
    return run_tool(MAKE, *args, cwd=cwd, env=env, check=check)


@unittest.skipUnless(MAKE, "make is not installed")
class TestMakefile(unittest.TestCase):
    def test_spaced_dev_venv_tokens_quoted(self):
        """Verify make properly quotes DEV_VENV paths containing whitespace."""
        with tempfile.TemporaryDirectory(prefix="spaced path ") as tmpdir:
            spaced_venv = str(Path(tmpdir) / ".venv")
            run_res = run_make("python-env", f"DEV_VENV={spaced_venv}")
            self.assertNotEqual(run_res.returncode, 0)
            self.assertIn(f"{spaced_venv}/bin/python is missing", run_res.stderr)

            dry_run = run_make(
                "-n",
                "bootstrap",
                "python-env",
                "lint",
                f"DEV_VENV={spaced_venv}",
                check=True,
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
        """Verify DEV_VENV paths containing literal dollar signs are preserved."""
        with tempfile.TemporaryDirectory(prefix="work$backup") as tmpdir:
            dollar_venv = str(Path(tmpdir) / ".venv")
            dollar_venv_make = dollar_venv.replace("$", "$$")

            cmd_res = run_make("python-env", f"DEV_VENV={dollar_venv_make}")
            self.assertNotEqual(cmd_res.returncode, 0)
            self.assertIn(f"{dollar_venv}/bin/python is missing", cmd_res.stderr)

            env_res = run_make(
                "python-env",
                env=clean_env(DEV_VENV=dollar_venv),
            )
            self.assertNotEqual(env_res.returncode, 0)
            self.assertIn(f"{dollar_venv}/bin/python is missing", env_res.stderr)

    def test_invalid_dev_venv_rejected_by_guard(self):
        """Verify the bootstrap guard rejects DEV_VENV values outside .venv naming rules."""
        dry_run = run_make(
            "-n",
            "bootstrap",
            "DEV_VENV=build/notvenv",
            check=True,
        )
        self.assertIn('case "$DEV_VENV" in .venv|*/.venv)', dry_run.stdout)

        with tempfile.TemporaryDirectory() as tmpdir:
            stub = Path(tmpdir) / "stub"
            log = Path(tmpdir) / "stub.log"
            create_python3_stub(stub)

            run_res = run_make(
                "bootstrap",
                "DEV_VENV=build/notvenv",
                env=clean_env(PATH=f"{stub}:{os.environ['PATH']}", STUB_LOG=str(log)),
            )
            self.assertNotEqual(run_res.returncode, 0)
            self.assertIn("must name a .venv directory", run_res.stderr)
            self.assertIn("build/notvenv", run_res.stderr)
            self.assertFalse(log.exists())

    def test_boundary_dot_venv_accepted_by_guard(self):
        """Verify the bootstrap guard accepts an exact .venv boundary path."""
        result = run_make("-n", "bootstrap", "DEV_VENV=.venv", check=True)
        self.assertIn('case "$DEV_VENV" in .venv|*/.venv)', result.stdout)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            shutil.copy(REPO_ROOT / "Makefile", tmp / "Makefile")
            (tmp / "requirements-dev.txt").write_text("", encoding="utf-8")
            if GIT:
                run_tool(GIT, "init", "-q", str(tmp), check=True)

            stub = tmp / "stub"
            log = tmp / "stub.log"
            create_python3_stub(stub)

            res = run_make(
                "bootstrap",
                "DEV_VENV=.venv",
                cwd=tmp,
                env=clean_env(PATH=f"{stub}:{os.environ['PATH']}", STUB_LOG=str(log)),
            )
            self.assertEqual(res.returncode, 0)
            log_content = log.read_text(encoding="utf-8")
            self.assertIn("-m venv --clear .venv", log_content)
            self.assertIn(
                "-m pip install --quiet --timeout 60 --require-hashes -r requirements-dev.txt",
                log_content,
            )
            self.assertTrue((tmp / ".venv" / "requirements-dev.txt").exists())

    @unittest.skipUnless(GIT, "git is not installed")
    def test_derivation_keeps_whitespace(self):
        """Verify automatic DEV_VENV derivation preserves directory names containing whitespace."""
        for dir_name in ["repo\tname", "repo  name", "repo "]:
            with tempfile.TemporaryDirectory() as tmpdir:
                repo_dir = Path(tmpdir) / dir_name
                repo_dir.mkdir()
                run_tool(GIT, "init", "-q", str(repo_dir), check=True)
                shutil.copy(REPO_ROOT / "Makefile", repo_dir / "Makefile")
                shutil.copy(REPO_ROOT / "requirements-dev.txt", repo_dir / "requirements-dev.txt")
                res = run_make("python-env", cwd=repo_dir)
                self.assertNotEqual(res.returncode, 0)
                expected = f"{realpath(repo_dir)}/.venv/bin/python is missing"
                self.assertIn(expected, res.stderr)


if __name__ == "__main__":
    unittest.main()
