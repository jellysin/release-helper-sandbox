"""Replay bounded release-run waits without contacting GitHub."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
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


@unittest.skipUnless(BASH, "bash is not on PATH; shell scripts require bash")
class TestMergeStep(unittest.TestCase):
    """Execute the release PR merge step with replayed forge responses."""

    BRANCH = "release-please--branches--main"
    COMMIT = "0123456789abcdef0123456789abcdef01234567"
    FAILURE = (0, "completed failure\n", "")
    RERUN = ["run", "rerun", "55", "--failed"]
    MERGE = [".github/scripts/land.py", "merge", "408"]
    write_executable = TestRunScripts.write_executable

    def setUp(self):
        """Reuse replay stubs and copy the executable waiter into an isolated cwd."""
        TestRunScripts.setUp(self)
        scripts = self.directory / ".github" / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(SCRIPTS / "wait-run.sh", scripts / "wait-run.sh")
        stub = f"#!{sys.executable}\n" + textwrap.dedent("""\
            import json
            import sys
            from pathlib import Path

            command = Path(sys.argv[0])
            with command.with_suffix(".jsonl").open("a") as stream:
                stream.write(json.dumps(sys.argv[1:]) + "\\n")
            if command.name == "git" and sys.argv[1] == "show":
                sys.stdout.write("0.1.125\\n")
            """)
        for name in ("git", "python3"):
            self.write_executable(name, stub)
            (self.directory / f"{name}.jsonl").write_text("")
        self.output = self.directory / "output"
        self.output.write_text("")
        self.environment.update(NUMBER="408", GITHUB_OUTPUT=str(self.output))

    def merge_block(self):
        """Extract the named step's literal run block using indentation alone."""
        name = "Merge the release PR when CLEAN and read the version it records"
        lines = (ROOT / ".github" / "workflows" / "cycle.yml").read_text().splitlines()
        start = next((index for index, line in enumerate(lines) if line.strip() == f"- name: {name}"), None)
        self.assertIsNotNone(start, "merge step not found")
        step_indent = len(lines[start]) - len(lines[start].lstrip())
        block = []
        run_indent = None
        for line in lines[start + 1 :]:
            indent = len(line) - len(line.lstrip())
            if line.strip() and indent <= step_indent:
                break
            if run_indent is None:
                if line.strip() == "run: |":
                    run_indent = indent
            elif line.strip() and indent <= run_indent:
                break
            else:
                block.append(line)
        self.assertIsNotNone(run_indent, "merge step run block not found")
        script = textwrap.dedent("\n".join(block))
        self.assertTrue(script.strip(), "merge step run block is empty")
        return script

    def replay(self, waits, rerun=False):
        """Run the real block with strict bash and return its recorded argv lists."""
        answers = [(0, "", ""), (0, f"{self.BRANCH}\n", ""), (0, "55\n", "")]
        answers.append(waits[0])
        if rerun:
            answers.append((0, "", ""))
        answers.extend(waits[1:])
        answers.append((0, f"{self.COMMIT}\n", ""))
        (self.directory / "answers.json").write_text(json.dumps(answers))
        (self.directory / "calls.jsonl").write_text("")
        result = subprocess.run(
            [BASH, "-e", "-o", "pipefail", "-c", self.merge_block()],
            cwd=self.directory,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        gh = list(map(json.loads, (self.directory / "calls.jsonl").read_text().splitlines()))
        python = list(map(json.loads, (self.directory / "python3.jsonl").read_text().splitlines()))
        git = list(map(json.loads, (self.directory / "git.jsonl").read_text().splitlines()))
        self.assertNotIn("unexpected gh call", result.stderr)
        return result, gh, python, git

    def assert_merged(self, result, python, git):
        """Require one merge invocation and version outputs from the recorded commit."""
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(python, [self.MERGE])
        self.assertEqual(git, [["fetch", "--quiet", "origin", "main"], ["show", f"{self.COMMIT}:version.txt"]])
        self.assertEqual(self.output.read_text().splitlines(), ["tag=v0.1.125", f"commit={self.COMMIT}"])

    def test_first_wait_success(self):
        """Merge immediately after successful dispatched CI without rerunning it."""
        result, gh, python, git = self.replay([SUCCESS])
        self.assert_merged(result, python, git)
        self.assertEqual(result.stderr, "")
        self.assertNotIn(self.RERUN, gh)
        self.assertEqual(
            gh,
            [
                ["pr", "edit", "408", "--add-label", "release-ready"],
                ["pr", "view", "408", "--json", "headRefName", "--jq", ".headRefName"],
                [
                    "run",
                    "list",
                    "--workflow",
                    "ci.yml",
                    "--event",
                    "workflow_dispatch",
                    "--branch",
                    self.BRANCH,
                    "--limit",
                    "1",
                    "--json",
                    "databaseId",
                    "--jq",
                    ".[0].databaseId // empty",
                ],
                ["run", "view", "55", *VIEW[3:]],
                ["pr", "view", "408", "--json", "mergeCommit", "--jq", ".mergeCommit.oid"],
            ],
        )

    def test_rerun_success(self):
        """Rerun failed jobs exactly once and merge only after their success."""
        result, gh, python, git = self.replay([self.FAILURE, SUCCESS], rerun=True)
        self.assert_merged(result, python, git)
        self.assertEqual(result.stderr, "")
        self.assertEqual(gh.count(self.RERUN), 1)
        self.assertEqual(gh[3:6], [["run", "view", "55", *VIEW[3:]], self.RERUN, ["run", "view", "55", *VIEW[3:]]])
        self.assertEqual(len(gh), 7)

    def test_rerun_fails_again(self):
        """Stop before merging or any further forge call when rerun CI fails again."""
        result, gh, python, git = self.replay([self.FAILURE, self.FAILURE], rerun=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"dispatched CI run 55 on {self.BRANCH}: failure", result.stderr)
        self.assertEqual(python, [])
        self.assertEqual(git, [])
        self.assertEqual(self.output.read_text(), "")
        self.assertEqual(gh.count(self.RERUN), 1)
        self.assertEqual(gh[3:], [["run", "view", "55", *VIEW[3:]], self.RERUN, ["run", "view", "55", *VIEW[3:]]])

    def test_transient_read(self):
        """Let the real waiter recover from a failed read before the merge step proceeds."""
        result, gh, python, git = self.replay([FAILED_READ, SUCCESS])
        self.assert_merged(result, python, git)
        self.assertIn("gh run view 55 failed (1/3 in a row)", result.stderr)
        self.assertNotIn(self.RERUN, gh)
        self.assertEqual(gh[3:5], [["run", "view", "55", *VIEW[3:]]] * 2)
        self.assertEqual(len(gh), 6)


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
