"""Replay bounded release-run waits without contacting GitHub."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / ".github" / "scripts"
BASH = shutil.which("bash")
VIEW = ["run", "view", "123", "--json", "status,conclusion", "--jq", r'"\(.status) \(.conclusion)"']
LIST = [
    "run",
    "list",
    "--workflow",
    "prepare-release.yml",
    "--event",
    "workflow_dispatch",
    "--limit",
    "20",
    "--json",
    "databaseId,displayTitle",
    "--jq",
    '[.[] | select(.displayTitle == "Prepare v1")][0].databaseId // empty',
]
PENDING = (0, "in_progress \n", "")
SUCCESS = (0, "completed success\n", "")
FAILED_READ = (1, "", "forge read failed\n")
GH_STUB = """import json
import sys
from pathlib import Path

directory = Path(__file__).parent
answers = json.loads((directory / "answers.json").read_text())
log = directory / "calls.jsonl"
index = len(log.read_text().splitlines())
with log.open("a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
if index >= len(answers):
    sys.stderr.write("unexpected gh call\\n")
    sys.exit(97)
code, stdout, stderr = answers[index]
sys.stdout.write(stdout)
sys.stderr.write(stderr)
sys.exit(code)
"""


@unittest.skipUnless(BASH, "bash is not on PATH; shell scripts require bash")
class TestRunScripts(unittest.TestCase):
    """Execute the shell entry points against file-backed gh and sleep stubs."""

    def setUp(self):
        """Install executable stubs and isolate their replay files."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.write_executable("gh", f"#!{sys.executable}\n{GH_STUB}")
        self.write_executable("sleep", "#!/bin/sh\nexit 0\n")
        self.environment = dict(os.environ)
        self.environment.update(
            PATH=str(self.directory) + os.pathsep + os.environ.get("PATH", ""),
            GITHUB_SERVER_URL="https://github.com",
            GITHUB_REPOSITORY="fixture/repository",
        )

    def write_executable(self, name, content):
        """Write a stub with executable permissions."""
        path = self.directory / name
        path.write_text(content)
        path.chmod(0o755)

    def replay(self, script, answers, *arguments):
        """Run one script from outside the repository with a bounded deadline."""
        (self.directory / "answers.json").write_text(json.dumps(answers))
        log = self.directory / "calls.jsonl"
        log.write_text("")
        result = subprocess.run(
            [BASH, str(SCRIPTS / script), *arguments],
            cwd=self.directory,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        calls = list(map(json.loads, log.read_text().splitlines()))
        self.assertNotIn("unexpected gh call", result.stderr)
        return result, calls

    def watch(self, answers):
        """Replay the workflow-and-title entry point."""
        return self.replay("watch-run.sh", answers, "prepare-release.yml", "Prepare v1")

    def test_wait_in_progress_then_success(self):
        """Return only the conclusion after polling the exact view arguments."""
        result, calls = self.replay("wait-run.sh", [PENDING, PENDING, SUCCESS], "123")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "success\n")
        self.assertEqual(result.stderr, "")
        self.assertEqual(calls, [VIEW] * 3)

    def test_wait_tolerates_two_failed_reads(self):
        """Preserve gh stderr and tolerate two consecutive read failures."""
        result, calls = self.replay("wait-run.sh", [FAILED_READ, FAILED_READ, SUCCESS], "123")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "success\n")
        self.assertEqual(
            result.stderr,
            "forge read failed\ngh run view 123 failed (1/3 in a row)\n"
            "forge read failed\ngh run view 123 failed (2/3 in a row)\n",
        )
        self.assertEqual(calls, [VIEW] * 3)

    def test_wait_good_read_resets_failure_count(self):
        """Count only consecutive failures across an intervening good read."""
        answers = [FAILED_READ, PENDING, FAILED_READ, FAILED_READ, SUCCESS]
        result, calls = self.replay("wait-run.sh", answers, "123")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "success\n")
        self.assertEqual(result.stderr.count("failed (1/3 in a row)"), 2)
        self.assertEqual(result.stderr.count("failed (2/3 in a row)"), 1)
        self.assertEqual(calls, [VIEW] * 5)

    def test_wait_three_failed_reads(self):
        """Stop after the third consecutive failure with a clear diagnostic."""
        result, calls = self.replay("wait-run.sh", [FAILED_READ] * 3, "123")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("gh run view 123 failed (3/3 in a row)\n", result.stderr)
        self.assertTrue(result.stderr.endswith("gh run view 123 failed 3 times in a row\n"))
        self.assertEqual(calls, [VIEW] * 3)

    def test_wait_completed_failure(self):
        """Leave interpretation of an unsuccessful conclusion to the caller."""
        result, calls = self.replay("wait-run.sh", [(0, "completed failure\n", "")], "123")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "failure\n")
        self.assertEqual(calls, [VIEW])

    def test_wait_invalid_run(self):
        """Reject a nonnumeric run before invoking gh."""
        result, calls = self.replay("wait-run.sh", [], "abc")
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr.lower())
        self.assertEqual(calls, [])

    def test_wait_missing_run(self):
        """Report usage for a missing run without invoking gh."""
        result, calls = self.replay("wait-run.sh", [])
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr.lower())
        self.assertEqual(calls, [])

    def test_wait_success_on_poll_180(self):
        """Accept completion on the last permitted poll."""
        result, calls = self.replay("wait-run.sh", [PENDING] * 179 + [SUCCESS], "123")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "success\n")
        self.assertEqual(calls, [VIEW] * 180)

    def test_wait_stops_after_180_polls(self):
        """Fail after exactly 180 incomplete reads without requesting poll 181."""
        result, calls = self.replay("wait-run.sh", [PENDING] * 180 + [SUCCESS], "123")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "run 123 did not complete within 180 polls\n")
        self.assertEqual(calls, [VIEW] * 180)

    def test_watch_list_fails_once(self):
        """Retry a failed list read and resolve the waiter beside the script."""
        result, calls = self.watch([FAILED_READ, (0, "123\n", ""), SUCCESS])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "prepare-release.yml run 123: https://github.com/fixture/repository/actions/runs/123\nconcluded: success\n",
        )
        self.assertEqual(result.stderr, "forge read failed\ngh run list failed (poll 1/24)\n")
        self.assertEqual(calls, [LIST, LIST, VIEW])

    def test_watch_completed_failure(self):
        """Print an unsuccessful conclusion and fail the workflow caller."""
        result, calls = self.watch([(0, "123\n", ""), (0, "completed failure\n", "")])
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stdout.endswith("concluded: failure\n"))
        self.assertEqual(calls, [LIST, VIEW])

    def test_watch_never_appears(self):
        """Bound discovery to exactly 24 list calls."""
        result, calls = self.watch([(0, "", "")] * 24)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "no prepare-release.yml run named 'Prepare v1' appeared\n")
        self.assertEqual(calls, [LIST] * 24)

    def test_watch_three_failed_views(self):
        """Propagate the waiter's consecutive-read failure."""
        result, calls = self.watch([(0, "123\n", "")] + [FAILED_READ] * 3)
        self.assertEqual(result.returncode, 1)
        self.assertIn("gh run view 123 failed 3 times in a row\n", result.stderr)
        self.assertNotIn("concluded:", result.stdout)
        self.assertEqual(calls, [LIST] + [VIEW] * 3)


class TestWaitPolicy(unittest.TestCase):
    """Guard workflow polling policy without running any shell commands."""

    def test_no_raw_run_watch(self):
        """Reject unguarded gh watch calls in every workflow and script."""
        paths = tuple((ROOT / ".github" / "workflows").glob("*.yml")) + tuple(SCRIPTS.glob("*"))
        for index in range(len(paths)):
            path = paths[index]
            if path.is_file():
                with self.subTest(path=str(path.relative_to(ROOT))):
                    self.assertNotRegex(path.read_text(), r"\bgh\s+run\s+watch\b")

    def test_wait_budget_within_cycle_headroom(self):
        """Keep the poll budget within the cycle timeout's stated headroom."""
        script = (SCRIPTS / "wait-run.sh").read_text()
        cycle = (ROOT / ".github" / "workflows" / "cycle.yml").read_text()
        polls = re.search(r"^WAIT_POLLS=(\d+)$", script, re.MULTILINE)
        interval = re.search(r"^\s*sleep (\d+)$", script, re.MULTILINE)
        headroom = re.search(r"# land\.py's WORST_CASE_SECONDS .* plus (\d+) for the other steps", cycle)
        self.assertIsNotNone(polls)
        self.assertIsNotNone(interval)
        self.assertIsNotNone(headroom)
        self.assertLessEqual(int(polls[1]) * int(interval[1]), int(headroom[1]) * 60)

    def test_waiter_executable(self):
        """Require execute permission for direct invocation by the callers."""
        self.assertTrue((SCRIPTS / "wait-run.sh").stat().st_mode & 0o111)
