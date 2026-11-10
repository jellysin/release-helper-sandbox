"""Unit tests for .github/scripts/land.py."""

import base64
import importlib.util
import json
import os
import re
import sys
import unittest
import unittest.mock
from pathlib import Path

# land.py reads GITHUB_REPOSITORY at import; set os.environ before import
os.environ.setdefault("GITHUB_REPOSITORY", "jellysin/release-helper-sandbox")
os.environ.setdefault("CYCLE_STARTED", "2026-10-23T00:00:00Z")

# Load land.py via importlib from path
SCRIPT_PATH = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "land.py"
spec = importlib.util.spec_from_file_location("land", SCRIPT_PATH)
land = importlib.util.module_from_spec(spec)
sys.modules["land"] = land
spec.loader.exec_module(land)


class TestCiState(unittest.TestCase):
    def test_run_completed_success(self):
        run = {"status": "completed", "conclusion": "success"}
        self.assertEqual(land.ci_state(run, []), "success")

    def test_run_completed_failure(self):
        run = {"status": "completed", "conclusion": "failure"}
        result = land.ci_state(run, [])
        self.assertNotEqual(result, "success")
        self.assertNotEqual(result, "pending")
        self.assertIn("failure", result)

    def test_run_in_progress_all_jobs_success(self):
        run = {"status": "in_progress", "conclusion": None}
        jobs = [
            {"name": "build", "status": "completed", "conclusion": "success"},
            {"name": "test", "status": "completed", "conclusion": "success"},
        ]
        self.assertEqual(land.ci_state(run, jobs), "success")

    def test_run_in_progress_one_job_failure(self):
        run = {"status": "in_progress", "conclusion": None}
        jobs = [
            {"name": "build", "status": "completed", "conclusion": "success"},
            {"name": "lint", "status": "completed", "conclusion": "failure"},
        ]
        result = land.ci_state(run, jobs)
        self.assertNotEqual(result, "success")
        self.assertNotEqual(result, "pending")
        self.assertIn("lint", result)

    def test_run_in_progress_one_job_in_progress(self):
        run = {"status": "in_progress", "conclusion": None}
        jobs = [
            {"name": "build", "status": "completed", "conclusion": "success"},
            {"name": "lint", "status": "in_progress", "conclusion": None},
        ]
        self.assertEqual(land.ci_state(run, jobs), "pending")

    def test_run_in_progress_no_jobs(self):
        run = {"status": "in_progress", "conclusion": None}
        self.assertEqual(land.ci_state(run, []), "pending")

    def test_skipped_job_counts_as_success(self):
        run = {"status": "in_progress", "conclusion": None}
        jobs = [
            {"name": "optional", "status": "completed", "conclusion": "skipped"},
        ]
        self.assertEqual(land.ci_state(run, jobs), "success")

    def test_neutral_job_counts_as_success(self):
        run = {"status": "in_progress", "conclusion": None}
        jobs = [
            {"name": "neutral_job", "status": "completed", "conclusion": "neutral"},
        ]
        self.assertEqual(land.ci_state(run, jobs), "success")

    def test_action_required_run(self):
        run = {"status": "completed", "conclusion": "action_required"}
        self.assertEqual(land.ci_state(run, []), "action_required")

        run_waiting = {"status": "action_required", "conclusion": "action_required"}
        self.assertEqual(land.ci_state(run_waiting, []), "action_required")

        run_queued = {"status": "queued", "conclusion": None}
        self.assertEqual(land.ci_state(run_queued, []), "pending")

    def test_refuse_100_jobs_as_failure(self):
        run = {"status": "in_progress", "conclusion": None}
        jobs = [{"name": f"job{i}", "status": "completed", "conclusion": "success"} for i in range(100)]
        result = land.ci_state(run, jobs)
        self.assertNotEqual(result, "success")
        self.assertNotEqual(result, "pending")


class TestRunJobs(unittest.TestCase):
    def test_returns_jobs_of_one_page(self):
        page = {"total_count": 2, "jobs": [{"name": "CI"}, {"name": "governance"}]}
        with unittest.mock.patch.object(land, "api", return_value=page):
            self.assertEqual([job["name"] for job in land.run_jobs(1)], ["CI", "governance"])

    def test_refuses_more_than_one_page(self):
        page = {"total_count": 100, "jobs": [{"name": "CI"}] * 100}
        with unittest.mock.patch.object(land, "api", return_value=page):
            with self.assertRaises(SystemExit):
                land.run_jobs(1)


class TestResumeRegex(unittest.TestCase):
    def test_accepts_valid_cycle_change(self):
        match = land.LEFTOVER.match("cycle/123-change")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "123")

    def test_refuses_cycle_catalog(self):
        self.assertIsNone(land.LEFTOVER.match("cycle/123-catalog"))

    def test_refuses_cycle_abc_change(self):
        self.assertIsNone(land.LEFTOVER.match("cycle/abc-change"))

    def test_refuses_release_please(self):
        self.assertIsNone(land.LEFTOVER.match("release-please--branches--main"))


class TestSignedCommit(unittest.TestCase):
    def test_signed_commit_calls_create_commit_on_branch(self):
        commit_res = json.dumps({"data": {"createCommitOnBranch": {"commit": {"oid": "commit123"}}}})
        with unittest.mock.patch.object(land, "gh", return_value=commit_res) as mock_gh:
            oid = land.signed_commit("test-branch", "base123", "feat: test", {"version.txt": b"1.0.0"})
            self.assertEqual(oid, "commit123")
            mock_gh.assert_called_once()
            args, kwargs = mock_gh.call_args
            self.assertEqual(args, ("api", "graphql", "--input", "-"))
            payload = json.loads(kwargs["data"].decode("utf-8"))
            self.assertEqual(payload["variables"]["input"]["branch"]["branchName"], "test-branch")
            self.assertEqual(payload["variables"]["input"]["expectedHeadOid"], "base123")
            self.assertEqual(
                payload["variables"]["input"]["fileChanges"]["additions"],
                [{"path": "version.txt", "contents": base64.b64encode(b"1.0.0").decode()}],
            )


def make_candidate_pr(number=162, ref="cycle/123-change", title="fix: cycle test", sha="oldsha"):
    """Construct a mock pull request dictionary for cycle candidate tests."""
    return {
        "number": number,
        "title": title,
        "user": {"login": land.BOT},
        "head": {"ref": ref, "sha": sha, "repo": {"full_name": land.REPO}},
    }


class TestResumeRebuild(unittest.TestCase):
    def assert_rebuilt(self, patch_called, created_commits, content_bytes):
        """Assert the branch was reset to main and the change re-committed on it once."""
        self.assertEqual(len(patch_called), 1)
        self.assertEqual(
            patch_called[0],
            (
                f"repos/{land.REPO}/git/refs/heads/cycle/123-change",
                ("--method", "PATCH", "-f", "sha=mainsha123456789012", "-F", "force=true"),
            ),
        )
        self.assertEqual(len(created_commits), 1)
        inp = created_commits[0]["variables"]["input"]
        self.assertEqual(inp["branch"]["branchName"], "cycle/123-change")
        self.assertEqual(inp["expectedHeadOid"], "mainsha123456789012")
        self.assertEqual(inp["message"]["headline"], "fix: cycle test")
        self.assertEqual(
            inp["fileChanges"]["additions"],
            [{"path": "version.txt", "contents": base64.b64encode(content_bytes).decode()}],
        )

    def test_resume_behind_pr_rebuilds_branch(self):
        """Verify resuming a BEHIND pull request resets the branch and recommits changes."""
        pr = make_candidate_pr()
        run = {
            "path": ".github/workflows/cycle.yml",
            "repository": {"full_name": land.REPO},
            "created_at": "2026-10-23T00:00:00Z",
        }
        view = {"mergeStateStatus": "BEHIND", "headRefOid": "headsha123"}
        files = [{"filename": "version.txt", "status": "modified"}]
        content_bytes = b"2.0.0\n"
        content_obj = {"encoding": "base64", "content": base64.b64encode(content_bytes).decode()}
        main_ref = {"object": {"sha": "mainsha123456789012"}}
        graphql_resp = json.dumps({"data": {"createCommitOnBranch": {"commit": {"oid": "new_commit_sha"}}}})
        patch_called, created_commits = [], []

        def fake_api(path, *args):
            responses = {
                f"repos/{land.REPO}/pulls?state=open&per_page=100": [pr],
                f"repos/{land.REPO}/actions/runs/123": run,
                f"repos/{land.REPO}/pulls/162/files?per_page=100": files,
                f"repos/{land.REPO}/contents/version.txt?ref=headsha123": content_obj,
                f"repos/{land.REPO}/git/ref/heads/main": main_ref,
            }
            if path in responses:
                return responses[path]
            if path == f"repos/{land.REPO}/git/refs/heads/cycle/123-change":
                patch_called.append((path, args))
                return {}
            raise AssertionError(f"unexpected api call: {path} {args}")

        def fake_gh(*args, data=None):
            if args[:3] == ("pr", "view", "162"):
                return json.dumps(view)
            if args[:2] == ("api", "graphql"):
                created_commits.append(json.loads(data.decode("utf-8")))
                return graphql_resp
            raise AssertionError(f"unexpected gh call: {args}")

        with (
            unittest.mock.patch.object(land, "api", side_effect=fake_api),
            unittest.mock.patch.object(land, "gh", side_effect=fake_gh),
            unittest.mock.patch.object(land, "pull_request_runs", return_value=[]),
            unittest.mock.patch.object(land, "approve_and_wait") as mock_approve,
            unittest.mock.patch.object(land, "merge_when_clean") as mock_merge,
        ):
            land.resume()

            self.assert_rebuilt(patch_called, created_commits, content_bytes)
            mock_approve.assert_called_once_with(162, started="2026-10-23T00:00:00Z", sha="new_commit_sha")
            mock_merge.assert_called_once_with(162)

    def test_file_outside_allowed_raises_system_exit(self):
        pr = {
            "number": 162,
            "title": "fix: cycle test",
            "head": {"ref": "cycle/123-change", "sha": "headsha"},
        }
        files = [{"filename": "unauthorized.txt", "status": "modified"}]
        content_obj = {"encoding": "base64", "content": base64.b64encode(b"hello").decode()}
        main_ref = {"object": {"sha": "mainsha"}}

        def fake_api(path, *args):
            if "files" in path:
                return files
            if "contents" in path:
                return content_obj
            if "git/ref/heads/main" in path:
                return main_ref
            return {}

        with (
            unittest.mock.patch.object(land, "api", side_effect=fake_api) as mock_api,
            unittest.mock.patch.object(
                land, "gh", return_value=json.dumps({"data": {"createCommitOnBranch": {"commit": {"oid": "newsha"}}}})
            ),
        ):
            with self.assertRaises(SystemExit):
                land.rebuild(pr, "headsha")
            patch_calls = [
                c for c in mock_api.call_args_list if len(c.args) > 1 and "--method" in c.args and "PATCH" in c.args
            ]
            self.assertEqual(patch_calls, [])

    def test_status_removed_raises_system_exit(self):
        pr = {
            "number": 162,
            "title": "fix: cycle test",
            "head": {"ref": "cycle/123-change", "sha": "headsha"},
        }
        files = [{"filename": "version.txt", "status": "removed"}]
        with unittest.mock.patch.object(land, "api", return_value=files):
            with self.assertRaises(SystemExit):
                land.rebuild(pr, "headsha")

    def test_clean_or_blocked_pr_with_both_runs_no_rebuild(self):
        pr = {
            "number": 162,
            "title": "fix: cycle test",
            "user": {"login": land.BOT},
            "head": {
                "ref": "cycle/123-change",
                "sha": "old_head_sha",
                "repo": {"full_name": land.REPO},
            },
        }
        run = {
            "path": ".github/workflows/cycle.yml",
            "repository": {"full_name": land.REPO},
            "created_at": "2026-10-23T00:00:00Z",
        }
        view = {"mergeStateStatus": "CLEAN", "headRefOid": "headsha123"}

        def fake_api(path, *args):
            if path == f"repos/{land.REPO}/pulls?state=open&per_page=100":
                return [pr]
            if path == f"repos/{land.REPO}/actions/runs/123":
                return run
            raise AssertionError(f"unexpected api call during CLEAN PR resume: {path} {args}")

        def fake_gh(*args, data=None):
            if args[:3] == ("pr", "view", "162"):
                return json.dumps(view)
            raise AssertionError(f"unexpected gh call during CLEAN PR resume: {args}")

        with (
            unittest.mock.patch.object(land, "api", side_effect=fake_api),
            unittest.mock.patch.object(land, "gh", side_effect=fake_gh),
            unittest.mock.patch.object(
                land, "pull_request_runs", return_value=[{"path": p} for p in land.PR_WORKFLOWS]
            ),
            unittest.mock.patch.object(land, "rebuild") as mock_rebuild,
            unittest.mock.patch.object(land, "approve_and_wait") as mock_approve,
            unittest.mock.patch.object(land, "merge_when_clean") as mock_merge,
        ):
            for state in ("CLEAN", "BLOCKED"):
                with self.subTest(state=state):
                    view["mergeStateStatus"] = state
                    mock_approve.reset_mock()
                    mock_merge.reset_mock()
                    land.resume()
                    mock_rebuild.assert_not_called()
                    mock_approve.assert_called_once_with(162, started="2026-10-23T00:00:00Z", sha=None)
                    mock_merge.assert_called_once_with(162)

    def test_blocked_pr_missing_codeql_rebuilds_once(self):
        pr = make_candidate_pr()
        run = {
            "path": ".github/workflows/cycle.yml",
            "repository": {"full_name": land.REPO},
            "created_at": "2026-10-23T00:00:00Z",
        }
        view = {"mergeStateStatus": "BLOCKED", "headRefOid": "headsha123"}
        with (
            unittest.mock.patch.object(land, "api", side_effect=[[pr], run]),
            unittest.mock.patch.object(land, "gh", return_value=json.dumps(view)),
            unittest.mock.patch.object(
                land, "pull_request_runs", return_value=[{"path": land.PR_WORKFLOWS[0]}]
            ) as runs,
            unittest.mock.patch.object(land, "rebuild", return_value="rebuiltsha") as rebuild,
            unittest.mock.patch.object(land, "approve_and_wait") as approve,
            unittest.mock.patch.object(land, "merge_when_clean") as merge,
            unittest.mock.patch("builtins.print") as output,
        ):
            land.resume()
        runs.assert_called_once_with("headsha123")
        rebuild.assert_called_once_with(pr, "headsha123")
        approve.assert_called_once_with(162, started="2026-10-23T00:00:00Z", sha="rebuiltsha")
        merge.assert_called_once_with(162)
        messages = [call.args[0] for call in output.call_args_list]
        self.assertTrue(any("PR #162" in msg and "codeql.yml" in msg and "headsha123" in msg for msg in messages))

    def test_rebuild_returns_signed_commit_oid(self):
        pr = make_candidate_pr(title="feat: rebuild test")
        files = [{"filename": "version.txt", "status": "modified"}]
        content_obj = {"encoding": "base64", "content": base64.b64encode(b"1.0.1").decode()}
        main_ref = {"object": {"sha": "main_head_sha"}}

        def fake_api(path, *args):
            if "pulls/162/files" in path:
                return files
            if "contents/version.txt" in path:
                return content_obj
            if "git/ref/heads/main" in path:
                return main_ref
            if "git/refs/heads/cycle/123-change" in path:
                return {}
            raise AssertionError(f"unexpected api call: {path}")

        with (
            unittest.mock.patch.object(land, "api", side_effect=fake_api),
            unittest.mock.patch.object(land, "signed_commit", return_value="rebuilt_commit_oid") as mock_commit,
        ):
            result = land.rebuild(pr, "old_head_sha")
            self.assertEqual(result, "rebuilt_commit_oid")
            mock_commit.assert_called_once()

    def test_approve_and_wait_with_lagging_sha_uses_new_sha(self):
        """Verify approve_and_wait polls until the PR head matches the rebuilt SHA."""
        stale_pr = {"number": 162, "head": {"sha": "stalesha"}}
        new_pr = {"number": 162, "head": {"sha": "newsha"}}
        pull_responses = [stale_pr, new_pr]

        def fake_api(path, *args):
            """Return staged pull request responses for the requested path."""
            if path == f"repos/{land.REPO}/pulls/162":
                return pull_responses.pop(0)
            raise AssertionError(f"unexpected api call: {path}")

        run_ci = {
            "id": 42,
            "path": ".github/workflows/ci.yml",
            "status": "in_progress",
            "conclusion": "action_required",
            "html_url": "http://run/42",
        }
        run_codeql = {
            "id": 43,
            "path": ".github/workflows/codeql.yml",
            "status": "in_progress",
            "conclusion": "action_required",
            "html_url": "http://run/43",
        }
        jobs = [{"name": "test", "status": "completed", "conclusion": "success"}]

        with (
            unittest.mock.patch.object(land, "api", side_effect=fake_api),
            unittest.mock.patch.object(land.time, "sleep") as mock_sleep,
            unittest.mock.patch.object(land, "gh") as mock_gh,
            unittest.mock.patch.object(land, "refusal", return_value=None) as mock_refusal,
            unittest.mock.patch.object(land, "pull_request_runs", return_value=[run_ci, run_codeql]) as mock_runs,
            unittest.mock.patch.object(land, "run_jobs", return_value=jobs),
        ):
            land.approve_and_wait(162, started="2026-10-23T00:00:00Z", sha="newsha")

            mock_sleep.assert_called_with(land.PAUSE)
            mock_refusal.assert_called_once_with(new_pr, "2026-10-23T00:00:00Z")
            mock_runs.assert_called_with("newsha")
            for c in mock_runs.call_args_list:
                self.assertEqual(c[0][0], "newsha")
            mock_gh.assert_has_calls(
                [
                    unittest.mock.call("api", "--method", "POST", f"repos/{land.REPO}/actions/runs/42/approve"),
                    unittest.mock.call("api", "--method", "POST", f"repos/{land.REPO}/actions/runs/43/approve"),
                ],
                any_order=True,
            )
            self.assertEqual(mock_gh.call_count, 2)

    def test_approve_and_wait_sha_never_appears_raises_system_exit(self):
        """Verify approve_and_wait exits when the expected head SHA does not appear."""
        stale_pr = {"number": 162, "head": {"sha": "stalesha"}}
        with (
            unittest.mock.patch.object(land, "POLLS", 3),
            unittest.mock.patch.object(land, "api", return_value=stale_pr) as mock_api,
            unittest.mock.patch.object(land.time, "sleep") as mock_sleep,
        ):
            with self.assertRaises(SystemExit) as ctx:
                land.approve_and_wait(162, started="2026-10-23T00:00:00Z", sha="newsha")
            self.assertIn("PR #162: head is stalesha, not the rebuilt newsha", str(ctx.exception))
            self.assertEqual(mock_api.call_count, 3)
            self.assertEqual(mock_sleep.call_count, 3)

    def _wait(self, run_states):
        """Execute approve_and_wait with mocked runs yielding the provided sequence of states."""
        pr = {"number": 7, "head": {"sha": "abc"}}
        runs = [
            [
                dict(state, id=9, path=".github/workflows/ci.yml", html_url="http://run/9"),
                dict(state, id=10, path=".github/workflows/codeql.yml", html_url="http://run/10"),
            ]
            for state in run_states
        ]
        with (
            unittest.mock.patch.object(land, "POLLS", 3),
            unittest.mock.patch.object(land, "RUN_POLLS", 3),
            unittest.mock.patch.object(land, "api", return_value=pr),
            unittest.mock.patch.object(land.time, "sleep"),
            unittest.mock.patch.object(land, "gh"),
            unittest.mock.patch.object(land, "refusal", return_value=None),
            unittest.mock.patch.object(land, "pull_request_runs", side_effect=runs),
            unittest.mock.patch.object(land, "run_jobs", return_value=[]),
        ):
            land.approve_and_wait(7, started="2026-10-23T00:00:00Z")

    def test_approval_that_never_takes_names_action_required(self):
        """Verify failure message identifies action_required when CI approval fails to clear."""
        waiting = {"status": "completed", "conclusion": "action_required"}
        with self.assertRaises(SystemExit) as ctx:
            self._wait([waiting] * 4)
        self.assertIn("PR #7: CI on abc still action_required, approval did not take", str(ctx.exception))

    def test_approval_lag_then_success(self):
        """Verify approve_and_wait tolerates transient action_required state before success."""
        waiting = {"status": "completed", "conclusion": "action_required"}
        done = {"status": "completed", "conclusion": "success"}
        self._wait([waiting, waiting, done])


class TestWaitForRun(unittest.TestCase):
    def test_wait_for_run_empty_poll_then_success(self):
        """Verify wait_for_run handles an initial empty poll before succeeding."""
        run_ci = {
            "id": 42,
            "path": ".github/workflows/ci.yml",
            "status": "completed",
            "conclusion": "success",
            "html_url": "http://run/42",
        }
        run_codeql = {
            "id": 43,
            "path": ".github/workflows/codeql.yml",
            "status": "completed",
            "conclusion": "success",
            "html_url": "http://run/43",
        }
        jobs = [{"name": "CI", "status": "completed", "conclusion": "success"}]
        with (
            unittest.mock.patch.object(land, "pull_request_runs", side_effect=[[], [run_ci, run_codeql]]) as mock_runs,
            unittest.mock.patch.object(land, "run_jobs", return_value=jobs) as mock_jobs,
            unittest.mock.patch.object(land.time, "sleep") as mock_sleep,
            unittest.mock.patch("builtins.print") as mock_print,
        ):
            land.wait_for_run(7, "abc")
            self.assertEqual(mock_runs.call_count, 2)
            self.assertEqual(mock_jobs.call_count, 2)
            mock_jobs.assert_has_calls([unittest.mock.call(42), unittest.mock.call(43)])
            mock_sleep.assert_called_once_with(land.PAUSE)
            self.assertEqual(mock_print.call_count, 2)
            self.assertIn("CI success", mock_print.call_args_list[0][0][0])
            self.assertIn("CI success", mock_print.call_args_list[1][0][0])

    def test_wait_for_run_all_empty_raises_system_exit(self):
        """Verify wait_for_run exits if workflow runs never appear before timeout."""
        with (
            unittest.mock.patch.object(land, "RUN_POLLS", 3),
            unittest.mock.patch.object(land, "pull_request_runs", return_value=[]),
            unittest.mock.patch.object(land.time, "sleep") as mock_sleep,
        ):
            with self.assertRaises(SystemExit) as ctx:
                land.wait_for_run(7, "abc")
            self.assertIn("PR #7: CI on abc did not finish in time", str(ctx.exception))
            self.assertEqual(mock_sleep.call_count, 3)

    def test_wait_for_run_ci_success_codeql_pending_then_success(self):
        """Verify wait_for_run succeeds when CodeQL transitions from pending to success."""
        run_ci = {
            "id": 1,
            "path": ".github/workflows/ci.yml",
            "status": "completed",
            "conclusion": "success",
            "html_url": "http://run/1",
        }
        run_codeql_pending = {
            "id": 2,
            "path": ".github/workflows/codeql.yml",
            "status": "in_progress",
            "conclusion": None,
            "html_url": "http://run/2",
        }
        run_codeql_success = {
            "id": 2,
            "path": ".github/workflows/codeql.yml",
            "status": "completed",
            "conclusion": "success",
            "html_url": "http://run/2",
        }
        runs_poll1 = [run_ci, run_codeql_pending]
        runs_poll2 = [run_ci, run_codeql_success]
        with (
            unittest.mock.patch.object(land, "pull_request_runs", side_effect=[runs_poll1, runs_poll2]) as mock_runs,
            unittest.mock.patch.object(land, "run_jobs", return_value=[]),
            unittest.mock.patch.object(land.time, "sleep") as mock_sleep,
            unittest.mock.patch("builtins.print") as mock_print,
        ):
            land.wait_for_run(7, "abc")
            self.assertEqual(mock_runs.call_count, 2)
            mock_sleep.assert_called_once_with(land.PAUSE)
            self.assertEqual(mock_print.call_count, 2)
            self.assertIn("CI success", mock_print.call_args_list[0][0][0])
            self.assertIn("CI success", mock_print.call_args_list[1][0][0])

    def test_wait_for_run_ci_success_codeql_missing_raises_system_exit(self):
        """Verify wait_for_run exits when a required workflow run never appears."""
        run_ci = {
            "id": 1,
            "path": ".github/workflows/ci.yml",
            "status": "completed",
            "conclusion": "success",
            "html_url": "http://run/1",
        }
        with (
            unittest.mock.patch.object(land, "RUN_POLLS", 2),
            unittest.mock.patch.object(land, "pull_request_runs", return_value=[run_ci]),
            unittest.mock.patch.object(land, "run_jobs", return_value=[]),
            unittest.mock.patch.object(land.time, "sleep"),
        ):
            with self.assertRaises(SystemExit) as ctx:
                land.wait_for_run(7, "abc")
            self.assertIn("PR #7: CI on abc did not finish in time", str(ctx.exception))
            self.assertIn(".github/workflows/codeql.yml", str(ctx.exception))

    def test_wait_for_run_codeql_failure_raises_system_exit(self):
        """Verify wait_for_run exits when a required workflow run fails."""
        run_ci = {
            "id": 1,
            "path": ".github/workflows/ci.yml",
            "status": "completed",
            "conclusion": "success",
            "html_url": "http://run/1",
        }
        run_codeql_fail = {
            "id": 2,
            "path": ".github/workflows/codeql.yml",
            "status": "completed",
            "conclusion": "failure",
            "html_url": "http://run/2",
        }
        with (
            unittest.mock.patch.object(land, "pull_request_runs", return_value=[run_ci, run_codeql_fail]),
            unittest.mock.patch.object(land, "run_jobs", return_value=[]),
        ):
            with self.assertRaises(SystemExit) as ctx:
                land.wait_for_run(7, "abc")
            self.assertIn("http://run/2", str(ctx.exception))

    def test_wait_for_run_approves_later_action_required_run_exactly_once(self):
        """Verify a run turning action_required on a later poll is approved exactly once."""
        run_ci_ok = {
            "id": 10,
            "path": ".github/workflows/ci.yml",
            "status": "completed",
            "conclusion": "success",
            "html_url": "http://run/10",
        }
        run_codeql_pending = {
            "id": 20,
            "path": ".github/workflows/codeql.yml",
            "status": "in_progress",
            "conclusion": None,
            "html_url": "http://run/20",
        }
        run_codeql_action_req = {
            "id": 20,
            "path": ".github/workflows/codeql.yml",
            "status": "in_progress",
            "conclusion": "action_required",
            "html_url": "http://run/20",
        }
        run_codeql_success = {
            "id": 20,
            "path": ".github/workflows/codeql.yml",
            "status": "completed",
            "conclusion": "success",
            "html_url": "http://run/20",
        }
        poll1 = [run_ci_ok, run_codeql_pending]
        poll2 = [run_ci_ok, run_codeql_action_req]
        poll3 = [run_ci_ok, run_codeql_success]
        with (
            unittest.mock.patch.object(land, "RUN_POLLS", 5),
            unittest.mock.patch.object(land, "pull_request_runs", side_effect=[poll1, poll2, poll3]),
            unittest.mock.patch.object(land, "run_jobs", return_value=[]),
            unittest.mock.patch.object(land.time, "sleep"),
            unittest.mock.patch.object(land, "gh") as mock_gh,
        ):
            land.wait_for_run(7, "abc")
            mock_gh.assert_called_once_with("api", "--method", "POST", f"repos/{land.REPO}/actions/runs/20/approve")

    def test_wait_for_run_stuck_action_required_approves_once_and_raises_timeout(self):
        """Verify a run staying action_required is approved only once before timeout."""
        run_ci = {
            "id": 10,
            "path": ".github/workflows/ci.yml",
            "status": "completed",
            "conclusion": "success",
            "html_url": "http://run/10",
        }
        run_codeql_stuck = {
            "id": 20,
            "path": ".github/workflows/codeql.yml",
            "status": "in_progress",
            "conclusion": "action_required",
            "html_url": "http://run/20",
        }
        with (
            unittest.mock.patch.object(land, "RUN_POLLS", 3),
            unittest.mock.patch.object(land, "pull_request_runs", return_value=[run_ci, run_codeql_stuck]),
            unittest.mock.patch.object(land, "run_jobs", return_value=[]),
            unittest.mock.patch.object(land.time, "sleep") as mock_sleep,
            unittest.mock.patch.object(land, "gh") as mock_gh,
        ):
            with self.assertRaises(SystemExit) as ctx:
                land.wait_for_run(7, "abc")
            self.assertIn("still action_required", str(ctx.exception))
            self.assertIn("approval did not take", str(ctx.exception))
            mock_gh.assert_called_once_with("api", "--method", "POST", f"repos/{land.REPO}/actions/runs/20/approve")
            self.assertEqual(mock_sleep.call_count, 3)

    def test_wait_for_run_uses_run_polls(self):
        """Verify wait_for_run bounds iterations using RUN_POLLS rather than POLLS."""
        run_ci = {
            "id": 10,
            "path": ".github/workflows/ci.yml",
            "status": "in_progress",
            "conclusion": None,
            "html_url": "http://run/10",
        }
        with (
            unittest.mock.patch.object(land, "POLLS", 50),
            unittest.mock.patch.object(land, "RUN_POLLS", 3),
            unittest.mock.patch.object(land, "pull_request_runs", return_value=[run_ci]) as mock_runs,
            unittest.mock.patch.object(land, "run_jobs", return_value=[]),
            unittest.mock.patch.object(land.time, "sleep") as mock_sleep,
        ):
            with self.assertRaises(SystemExit):
                land.wait_for_run(7, "abc")
            self.assertEqual(mock_runs.call_count, 3)
            self.assertEqual(mock_sleep.call_count, 3)


class TestPullRequestRuns(unittest.TestCase):
    @staticmethod
    def make_run(run_id, path=".github/workflows/ci.yml", **changes):
        return {"id": run_id, "path": path, "head_sha": "testsha", "event": "pull_request", **changes}

    def test_unfiltered_workflow_endpoints_and_newest_id(self):
        ci, codeql = land.PR_WORKFLOWS
        pages = [
            {"total_count": 3, "workflow_runs": [self.make_run(20), self.make_run(10), self.make_run(99, codeql)]},
            {
                "total_count": 3,
                "workflow_runs": [self.make_run(5, codeql), self.make_run(4, codeql), self.make_run(99)],
            },
        ]
        with unittest.mock.patch.object(land, "api", side_effect=pages) as api:
            runs = land.pull_request_runs("testsha")
        self.assertEqual(
            api.call_args_list,
            [
                unittest.mock.call(f"repos/{land.REPO}/actions/workflows/ci.yml/runs?per_page=100"),
                unittest.mock.call(f"repos/{land.REPO}/actions/workflows/codeql.yml/runs?per_page=100"),
            ],
        )
        self.assertEqual([run["id"] for run in runs], [20, 5])
        self.assertEqual([run["path"] for run in runs], list(land.PR_WORKFLOWS))

    def test_filters_foreign_head_sha(self):
        page = {
            "workflow_runs": [
                self.make_run(10),
                self.make_run(99, head_sha="foreign"),
                self.make_run(100, head_sha=None),
            ]
        }
        with unittest.mock.patch.object(land, "api", return_value=page):
            self.assertEqual([run["id"] for run in land.pull_request_runs("testsha")], [10])

    def test_filters_non_pull_request_events(self):
        page = {
            "workflow_runs": [self.make_run(10), self.make_run(99, event="push"), self.make_run(100, event="schedule")]
        }
        with unittest.mock.patch.object(land, "api", return_value=page):
            self.assertEqual([run["id"] for run in land.pull_request_runs("testsha")], [10])

    def test_filters_foreign_path(self):
        page = {"workflow_runs": [self.make_run(10), self.make_run(99, ".github/workflows/other.yml")]}
        with unittest.mock.patch.object(land, "api", return_value=page):
            self.assertEqual([run["id"] for run in land.pull_request_runs("testsha")], [10])

    def test_missing_workflow_absent_from_result(self):
        pages = [{"workflow_runs": [self.make_run(10)]}, {"workflow_runs": []}]
        with unittest.mock.patch.object(land, "api", side_effect=pages):
            self.assertEqual(land.pull_request_runs("testsha"), [self.make_run(10)])

    def test_missing_workflows_in_allow_list_order(self):
        with unittest.mock.patch.object(land, "pull_request_runs", return_value=[]) as runs:
            self.assertEqual(land.missing_workflows("testsha"), list(land.PR_WORKFLOWS))
        runs.assert_called_once_with("testsha")

    def test_accepts_exactly_run_page(self):
        page = {"total_count": 1000, "workflow_runs": [self.make_run(i) for i in range(land.RUN_PAGE)]}
        with unittest.mock.patch.object(land, "api", return_value=page):
            self.assertEqual([run["id"] for run in land.pull_request_runs("testsha")], [land.RUN_PAGE - 1])

    def test_refuses_more_than_run_page(self):
        page = {"total_count": 1000, "workflow_runs": [self.make_run(i) for i in range(land.RUN_PAGE + 1)]}
        for pages in ([page], [{"workflow_runs": []}, page]):
            with self.subTest(page=len(pages)), unittest.mock.patch.object(land, "api", side_effect=pages):
                with self.assertRaisesRegex(SystemExit, "more than one page"):
                    land.pull_request_runs("testsha")

    def test_pr404_unfiltered_pages_recover_both_action_required_runs(self):
        sha = "0eeb7954944f1d8568e6302064c0a351c21de85f"
        ci = self.make_run(42712679106, head_sha=sha, conclusion="action_required")
        codeql = self.make_run(42712678951, land.PR_WORKFLOWS[1], head_sha=sha, conclusion="action_required")
        pages = {
            f"repos/{land.REPO}/actions/workflows/ci.yml/runs?per_page=100": {"workflow_runs": [ci]},
            f"repos/{land.REPO}/actions/workflows/codeql.yml/runs?per_page=100": {"workflow_runs": [codeql]},
            f"repos/{land.REPO}/actions/runs?event=pull_request&head_sha={sha}&per_page=20": {
                "total_count": 1,
                "workflow_runs": [ci],
            },
        }
        with unittest.mock.patch.object(land, "api", side_effect=pages.__getitem__):
            self.assertEqual(land.pull_request_runs(sha), [ci, codeql])


class TestApproveBothRuns(unittest.TestCase):
    def test_wait_for_run_approves_both_action_required_runs(self):
        """Verify wait_for_run approves both action_required runs."""
        run_ci = {
            "id": 101,
            "path": ".github/workflows/ci.yml",
            "conclusion": "action_required",
            "html_url": "http://run/101",
        }
        run_codeql = {
            "id": 102,
            "path": ".github/workflows/codeql.yml",
            "conclusion": "action_required",
            "html_url": "http://run/102",
        }
        run_ci_ok = dict(run_ci, status="completed", conclusion="success")
        run_codeql_ok = dict(run_codeql, status="completed", conclusion="success")

        with (
            unittest.mock.patch.object(land, "RUN_POLLS", 2),
            unittest.mock.patch.object(
                land, "pull_request_runs", side_effect=[[run_ci, run_codeql], [run_ci_ok, run_codeql_ok]]
            ),
            unittest.mock.patch.object(land, "run_jobs", return_value=[]),
            unittest.mock.patch.object(land.time, "sleep"),
            unittest.mock.patch.object(land, "gh") as mock_gh,
        ):
            land.wait_for_run(88, "sha88")
            mock_gh.assert_has_calls(
                [
                    unittest.mock.call("api", "--method", "POST", f"repos/{land.REPO}/actions/runs/101/approve"),
                    unittest.mock.call("api", "--method", "POST", f"repos/{land.REPO}/actions/runs/102/approve"),
                ],
                any_order=False,
            )
            self.assertEqual(mock_gh.call_count, 2)

    def test_approve_and_wait_delegates_to_wait_for_run_without_approving(self):
        """Verify approve_and_wait delegates to wait_for_run without its own approve loop."""
        pr = {"number": 88, "head": {"sha": "sha88"}}
        run_ci = {"id": 101, "path": ".github/workflows/ci.yml", "head_sha": "sha88", "event": "pull_request"}
        run_codeql = {"id": 102, "path": ".github/workflows/codeql.yml", "head_sha": "sha88", "event": "pull_request"}

        def fake_api(path, *args, **kwargs):
            """Return mock PR or runs payload for requested path."""
            if path == f"repos/{land.REPO}/pulls/88":
                return pr
            if path == f"repos/{land.REPO}/actions/workflows/ci.yml/runs?per_page=100":
                return {"workflow_runs": [run_ci]}
            if path == f"repos/{land.REPO}/actions/workflows/codeql.yml/runs?per_page=100":
                return {"workflow_runs": [run_codeql]}
            raise AssertionError(f"unexpected api call: {path}")

        with (
            unittest.mock.patch.object(land, "POLLS", 2),
            unittest.mock.patch.object(land, "api", side_effect=fake_api),
            unittest.mock.patch.object(land, "refusal", return_value=None),
            unittest.mock.patch.object(land.time, "sleep"),
            unittest.mock.patch.object(land, "gh") as mock_gh,
            unittest.mock.patch.object(land, "wait_for_run") as mock_wait,
        ):
            land.approve_and_wait(88, started="2026-10-23T00:00:00Z")
            mock_gh.assert_not_called()
            mock_wait.assert_called_once_with(88, "sha88")


class TestCycleJobTimeout(unittest.TestCase):
    """Verify cycle.yml job timeout accommodates worst-case land.py waits and step headroom."""

    def test_cycle_job_timeout_accommodates_worst_case_waits(self):
        """Parse cycle.yml timeout-minutes under cycle job and verify bounds."""
        workflow_path = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "cycle.yml"
        content = workflow_path.read_text(encoding="utf-8")
        match = re.search(r"^\s*cycle:.*?^\s*timeout-minutes:\s*(\d+)", content, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match, "timeout-minutes under cycle job not found in cycle.yml")
        timeout_minutes = int(match.group(1))
        self.assertTrue(
            timeout_minutes * 60 >= land.WORST_CASE_SECONDS + 30 * 60 and timeout_minutes <= 360,
            f"cycle job timeout {timeout_minutes}m must satisfy N*60 >= {land.WORST_CASE_SECONDS + 30 * 60} and N <= 360",
        )
        self.assertGreaterEqual(timeout_minutes * 60, land.WORST_CASE_SECONDS + 30 * 60)
        self.assertLessEqual(timeout_minutes, 360)

    def test_worst_case_seconds_grows_when_run_polls_grows(self):
        """Verify worst_case_seconds increases when run_polls grows."""
        base = land.worst_case_seconds(land.POLLS, land.RUN_POLLS, land.PAUSE, land.MAX_LEFTOVERS)
        self.assertEqual(base, land.WORST_CASE_SECONDS)
        larger = land.worst_case_seconds(land.POLLS, land.RUN_POLLS + 1, land.PAUSE, land.MAX_LEFTOVERS)
        self.assertGreater(larger, base)

    def test_worst_case_seconds_counts_every_leftover(self):
        """Verify each leftover PR resume may settle adds one full rebuild wait."""
        one = land.worst_case_seconds(land.POLLS, land.RUN_POLLS, land.PAUSE, 1)
        two = land.worst_case_seconds(land.POLLS, land.RUN_POLLS, land.PAUSE, 2)
        self.assertEqual(two - one, (3 * land.POLLS + land.RUN_POLLS) * land.PAUSE)

    def test_resume_refuses_more_than_max_leftovers(self):
        """Verify resume stops before any wait when more leftovers exist than the bound allows."""
        pulls = [{"number": n} for n in range(land.MAX_LEFTOVERS + 1)]
        candidates = [(pr, str(n)) for n, pr in enumerate(pulls)]
        with (
            unittest.mock.patch.object(land, "api", return_value=pulls),
            unittest.mock.patch.object(land, "leftover_prs", return_value=candidates),
            unittest.mock.patch.object(land, "approve_and_wait") as wait,
        ):
            with self.assertRaises(SystemExit) as ctx:
                land.resume()
        self.assertIn(f"more than {land.MAX_LEFTOVERS} leftover", str(ctx.exception))
        wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()
