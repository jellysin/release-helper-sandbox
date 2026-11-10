"""Replay bounded release-run waits without contacting GitHub."""

import json
import os
import re
import shutil
import subprocess  # nosec B404
import sys
import tempfile
import textwrap
import time
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
import time
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
answer = answers[index]
code, stdout, stderr = answer[:3]
if len(answer) == 4:
    time.sleep(answer[3])
sys.stdout.write(stdout)
sys.stderr.write(stderr)
sys.exit(code)
"""


@unittest.skipUnless(BASH, "bash is not on PATH; shell scripts require bash")
@unittest.skipUnless(shutil.which("timeout"), "timeout is not on PATH; wait reads require a deadline")
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
            WAIT_SECONDS="1800",
            WAIT_READ_SECONDS="30",
            FIND_READ_SECONDS="30",
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
        path = (SCRIPTS / script).resolve()
        self.assertTrue(path.is_file() and path.is_relative_to(SCRIPTS))
        # Fixed bash from shutil.which, argument list, no shell, repository scripts under test, stub commands on PATH.
        result = subprocess.run(  # nosec B603  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
            [BASH, str(path), *arguments],
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
        self.assertEqual(result.stderr, "run 123 did not complete within 180 polls or 1800 seconds\n")
        self.assertEqual(calls, [VIEW] * 180)

    def test_wait_hung_read(self):
        """Time out a hung read and recover within the per-read budget."""
        self.environment["WAIT_READ_SECONDS"] = "1"
        started = time.monotonic()
        result, calls = self.replay("wait-run.sh", [(*PENDING, 5), SUCCESS], "123")
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "success\n")
        self.assertIn("timed out after 1s (1/3 in a row)", result.stderr)
        self.assertLess(elapsed, 4)
        self.assertEqual(calls, [VIEW] * 2)

    def test_wait_wall_clock_stop(self):
        """Stop slow successful reads at the wall-clock budget before poll exhaustion."""
        self.environment["WAIT_SECONDS"] = "2"
        result, calls = self.replay("wait-run.sh", [(*PENDING, 1.2)] * 5, "123")
        self.assertEqual(result.returncode, 1)
        self.assertIn("within 180 polls or 2 seconds", result.stderr)
        self.assertGreater(len(calls), 0)
        self.assertLessEqual(len(calls), 3)
        self.assertEqual(calls, [VIEW] * len(calls))

    def test_wait_invalid_budgets(self):
        """Reject zero and nonnumeric time budgets before invoking gh."""
        for name in ("WAIT_SECONDS", "WAIT_READ_SECONDS"):
            for value in ("0", "x"):
                with self.subTest(name=name, value=value):
                    self.environment.update(WAIT_SECONDS="1800", WAIT_READ_SECONDS="30")
                    self.environment[name] = value
                    result, calls = self.replay("wait-run.sh", [], "123")
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("usage:", result.stderr.lower())
                    self.assertEqual(calls, [])

    def test_find_head_sha(self):
        """Select the exact head SHA with the dispatched run list arguments."""
        sha = "a" * 40
        result, calls = self.replay("find-run.sh", [(0, "55\n", "")], "ci.yml", "headSha", sha)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "55\n")
        self.assertEqual(
            calls,
            [
                [
                    "run",
                    "list",
                    "--workflow",
                    "ci.yml",
                    "--event",
                    "workflow_dispatch",
                    "--limit",
                    "20",
                    "--json",
                    "databaseId,headSha",
                    "--jq",
                    f'[.[] | select(.headSha == "{sha}")][0].databaseId // empty',
                ]
            ],
        )

    def test_find_hung_list(self):
        """Time out a hung list read and find the run on the next poll."""
        self.environment["FIND_READ_SECONDS"] = "1"
        started = time.monotonic()
        result, calls = self.replay(
            "find-run.sh", [(0, "", "", 5), (0, "123\n", "")], "prepare-release.yml", "displayTitle", "Prepare v1"
        )
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "123\n")
        self.assertIn("timed out after 1s (poll 1/24)", result.stderr)
        self.assertLess(elapsed, 4)
        self.assertEqual(calls, [LIST] * 2)

    def test_find_invalid_read_seconds(self):
        """Reject zero and nonnumeric list deadlines before invoking gh."""
        for value in ("0", "x"):
            with self.subTest(value=value):
                self.environment["FIND_READ_SECONDS"] = value
                result, calls = self.replay("find-run.sh", [], "ci.yml", "headSha", "a" * 40)
                self.assertEqual(result.returncode, 2)
                self.assertIn("usage:", result.stderr.lower())
                self.assertIn("FIND_READ_SECONDS", result.stderr)
                self.assertEqual(calls, [])

    def test_find_invalid_head_sha(self):
        """Reject head SHAs outside the forty lowercase hexadecimal format."""
        for sha in ("a" * 39, "a" * 41, "g" * 40, "A" * 40):
            with self.subTest(sha=sha):
                result, calls = self.replay("find-run.sh", [], "ci.yml", "headSha", sha)
                self.assertEqual(result.returncode, 2)
                self.assertIn("usage:", result.stderr.lower())
                self.assertEqual(calls, [])

    def test_find_unknown_field(self):
        """Reject fields outside the allow-list before invoking gh."""
        result, calls = self.replay("find-run.sh", [], "ci.yml", "headBranch", "main")
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr.lower())
        self.assertEqual(calls, [])

    def test_find_invalid_title(self):
        """Reject empty titles and jq string escape characters before invoking gh."""
        for title in ("", 'Prepare "v1"', "Prepare \\v1"):
            with self.subTest(title=title):
                result, calls = self.replay("find-run.sh", [], "ci.yml", "displayTitle", title)
                self.assertEqual(result.returncode, 2)
                self.assertIn("usage:", result.stderr.lower())
                self.assertEqual(calls, [])

    def test_find_never_appears(self):
        """Fail discovery after exactly twenty-four unmatched head-SHA reads."""
        sha = "a" * 40
        result, calls = self.replay("find-run.sh", [(0, "", "")] * 24, "ci.yml", "headSha", sha)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, f"no ci.yml run with headSha '{sha}' appeared\n")
        self.assertEqual(len(calls), 24)

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
        self.assertEqual(result.stderr, "no prepare-release.yml run with displayTitle 'Prepare v1' appeared\n")
        self.assertEqual(calls, [LIST] * 24)

    def test_watch_three_failed_views(self):
        """Propagate the waiter's consecutive-read failure."""
        result, calls = self.watch([(0, "123\n", "")] + [FAILED_READ] * 3)
        self.assertEqual(result.returncode, 1)
        self.assertIn("gh run view 123 failed 3 times in a row\n", result.stderr)
        self.assertNotIn("concluded:", result.stdout)
        self.assertEqual(calls, [LIST] + [VIEW] * 3)


@unittest.skipUnless(BASH, "bash is not on PATH; shell scripts require bash")
@unittest.skipUnless(shutil.which("timeout"), "timeout is not on PATH; wait reads require a deadline")
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
        shutil.copy2(SCRIPTS / "find-run.sh", scripts / "find-run.sh")
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

    def replay(self, waits, rerun=False, lists=None, head=None):
        """Run the real block with strict bash and return its recorded argv lists."""
        head = f"{self.BRANCH} {self.COMMIT}\n" if head is None else head
        answers = [(0, "", ""), (0, head, "")]
        answers.extend(lists if lists is not None else [(0, "55\n", "")])
        answers.extend(waits[:1])
        if rerun:
            answers.append((0, "", ""))
        answers.extend(waits[1:])
        answers.append((0, f"{self.COMMIT}\n", ""))
        (self.directory / "answers.json").write_text(json.dumps(answers))
        (self.directory / "calls.jsonl").write_text("")
        for name in ("wait-run.sh", "find-run.sh"):
            path = (SCRIPTS / name).resolve()
            self.assertTrue(path.is_file() and path.is_relative_to(SCRIPTS))
        # Fixed bash from shutil.which, argument list, no shell, repository scripts under test, stub commands on PATH.
        result = subprocess.run(  # nosec B603  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
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
                ["pr", "view", "408", "--json", "headRefName,headRefOid", "--jq", r'"\(.headRefName) \(.headRefOid)"'],
                [
                    "run",
                    "list",
                    "--workflow",
                    "ci.yml",
                    "--event",
                    "workflow_dispatch",
                    "--limit",
                    "20",
                    "--json",
                    "databaseId,headSha",
                    "--jq",
                    f'[.[] | select(.headSha == "{self.COMMIT}")][0].databaseId // empty',
                ],
                ["run", "view", "55", *VIEW[3:]],
                ["pr", "view", "408", "--json", "mergeCommit", "--jq", ".mergeCommit.oid"],
            ],
        )

    def test_run_registration_delayed(self):
        """Wait for the current head's run to register before polling run 55."""
        result, gh, python, git = self.replay([SUCCESS], lists=[(0, "", ""), (0, "55\n", "")])
        self.assert_merged(result, python, git)
        self.assertEqual(gh[2], gh[3])
        self.assertEqual(gh[4], ["run", "view", "55", *VIEW[3:]])
        self.assertEqual(len(gh), 6)

    def test_run_never_found(self):
        """Fail closed without merging or invoking python when CI never registers."""
        result, gh, python, git = self.replay([], lists=[(0, "", "")] * 24)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"no ci.yml run with headSha '{self.COMMIT}' appeared", result.stderr)
        self.assertEqual(len(gh), 26)
        self.assertEqual(python, [])
        self.assertEqual(git, [])
        self.assertEqual(self.output.read_text(), "")

    def test_head_commit_unreadable(self):
        """Stop by name before any run lookup when the release PR head commit is unreadable."""
        result, gh, python, git = self.replay([], lists=[], head=f"{self.BRANCH}\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("release PR #408 has no readable head commit: ''", result.stderr)
        self.assertEqual(len(gh), 2)
        self.assertEqual(python, [])
        self.assertEqual(git, [])
        self.assertEqual(self.output.read_text(), "")

    def test_head_read_has_deadline(self):
        """Require a thirty-second deadline on the merge step's head read."""
        self.assertIn('$(timeout 30 gh pr view "$NUMBER" --json headRefName,headRefOid --jq ', self.merge_block())

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

    def test_run_reads_have_deadlines(self):
        """Allow a run list or view only after timeout or as the start of an echo diagnostic."""
        read = re.compile(r"\bgh\s+run\s+(?:list|view)\b")
        allowed = re.compile(r"(?:\btimeout\s+\S+\s+|^\s*echo\s+\")$")
        for path in sorted(SCRIPTS.glob("*.sh")):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                for match in read.finditer(line):
                    self.assertRegex(
                        line[: match.start()],
                        allowed,
                        f"{path.relative_to(ROOT)}:{number}: run read lacks timeout",
                    )

    def test_no_raw_run_watch(self):
        """Reject unguarded gh watch calls in every workflow and script."""
        paths = tuple((ROOT / ".github" / "workflows").glob("*.yml")) + tuple(SCRIPTS.glob("*"))
        for index in range(len(paths)):
            path = paths[index]
            if path.is_file():
                with self.subTest(path=str(path.relative_to(ROOT))):
                    self.assertNotRegex(path.read_text(), r"\bgh\s+run\s+watch\b")

    def test_wait_budget_within_cycle_headroom(self):
        """Keep poll and wall-clock budgets within the cycle timeout's headroom."""
        script = (SCRIPTS / "wait-run.sh").read_text()
        cycle = (ROOT / ".github" / "workflows" / "cycle.yml").read_text()
        polls = re.search(r"^WAIT_POLLS=(\d+)$", script, re.MULTILINE)
        interval = re.search(r"^\s*sleep (\d+)$", script, re.MULTILINE)
        seconds = re.search(r'^WAIT_SECONDS="\$\{WAIT_SECONDS:-(\d+)\}"$', script, re.MULTILINE)
        read_seconds = re.search(r'^WAIT_READ_SECONDS="\$\{WAIT_READ_SECONDS:-(\d+)\}"$', script, re.MULTILINE)
        headroom = re.search(r"# land\.py's WORST_CASE_SECONDS .* plus (\d+) for the other steps", cycle)
        self.assertIsNotNone(polls)
        self.assertIsNotNone(interval)
        self.assertIsNotNone(seconds)
        self.assertIsNotNone(read_seconds)
        self.assertIsNotNone(headroom)
        self.assertLessEqual(int(polls[1]) * int(interval[1]), int(headroom[1]) * 60)
        self.assertLessEqual(int(seconds[1]), int(headroom[1]) * 60)
        self.assertLess(int(read_seconds[1]), int(seconds[1]))

    def test_waiter_executable(self):
        """Require execute permission for direct invocation by the callers."""
        self.assertTrue((SCRIPTS / "wait-run.sh").stat().st_mode & 0o111)
        self.assertTrue((SCRIPTS / "find-run.sh").stat().st_mode & 0o111)

    def test_no_single_latest_run_selection(self):
        """Reject selecting one latest run from the reused release branch."""
        cycle = (ROOT / ".github" / "workflows" / "cycle.yml").read_text()
        self.assertNotRegex(cycle, r"--limit\s+1\b")
