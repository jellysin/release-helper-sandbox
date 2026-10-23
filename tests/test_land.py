"""Unit tests for .github/scripts/land.py."""

import base64
import importlib.util
import json
import os
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
        jobs = [
            {"name": f"job{i}", "status": "completed", "conclusion": "success"}
            for i in range(100)
        ]
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
            self.assertEqual(payload["variables"]["input"]["fileChanges"]["additions"], [
                {"path": "version.txt", "contents": base64.b64encode(b"1.0.0").decode()}
            ])


def make_candidate_pr(number=162, ref="cycle/123-change", title="fix: cycle test", sha="oldsha"):
    return {
        "number": number,
        "title": title,
        "user": {"login": land.BOT},
        "head": {"ref": ref, "sha": sha, "repo": {"full_name": land.REPO}},
    }


class TestResumeRebuild(unittest.TestCase):
    def test_resume_behind_pr_rebuilds_branch(self):
        pr = make_candidate_pr()
        run = {"path": ".github/workflows/cycle.yml", "repository": {"full_name": land.REPO}, "created_at": "2026-10-23T00:00:00Z"}
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

        with unittest.mock.patch.object(land, "api", side_effect=fake_api), \
             unittest.mock.patch.object(land, "gh", side_effect=fake_gh), \
             unittest.mock.patch.object(land, "approve_and_wait") as mock_approve, \
             unittest.mock.patch.object(land, "merge_when_clean") as mock_merge:
            land.resume()

            self.assertEqual(len(patch_called), 1)
            self.assertEqual(patch_called[0], (
                f"repos/{land.REPO}/git/refs/heads/cycle/123-change",
                ("--method", "PATCH", "-f", "sha=mainsha123456789012", "-F", "force=true"),
            ))
            self.assertEqual(len(created_commits), 1)
            inp = created_commits[0]["variables"]["input"]
            self.assertEqual(inp["branch"]["branchName"], "cycle/123-change")
            self.assertEqual(inp["expectedHeadOid"], "mainsha123456789012")
            self.assertEqual(inp["message"]["headline"], "fix: cycle test")
            self.assertEqual(inp["fileChanges"]["additions"], [
                {"path": "version.txt", "contents": base64.b64encode(content_bytes).decode()}
            ])
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

        with unittest.mock.patch.object(land, "api", side_effect=fake_api) as mock_api, \
             unittest.mock.patch.object(land, "gh", return_value=json.dumps({"data": {"createCommitOnBranch": {"commit": {"oid": "newsha"}}}})):
            with self.assertRaises(SystemExit):
                land.rebuild(pr, "headsha")
            patch_calls = [
                c for c in mock_api.call_args_list
                if len(c.args) > 1 and "--method" in c.args and "PATCH" in c.args
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

    def test_clean_pr_no_rebuild(self):
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

        with unittest.mock.patch.object(land, "api", side_effect=fake_api), \
             unittest.mock.patch.object(land, "gh", side_effect=fake_gh), \
             unittest.mock.patch.object(land, "rebuild") as mock_rebuild, \
             unittest.mock.patch.object(land, "approve_and_wait") as mock_approve, \
             unittest.mock.patch.object(land, "merge_when_clean") as mock_merge:
            land.resume()

            mock_rebuild.assert_not_called()
            mock_approve.assert_called_once_with(162, started="2026-10-23T00:00:00Z", sha=None)
            mock_merge.assert_called_once_with(162)

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

        with unittest.mock.patch.object(land, "api", side_effect=fake_api), \
             unittest.mock.patch.object(land, "signed_commit", return_value="rebuilt_commit_oid") as mock_commit:
            result = land.rebuild(pr, "old_head_sha")
            self.assertEqual(result, "rebuilt_commit_oid")
            mock_commit.assert_called_once()

    def test_approve_and_wait_with_lagging_sha_uses_new_sha(self):
        stale_pr = {"number": 162, "head": {"sha": "stalesha"}}
        new_pr = {"number": 162, "head": {"sha": "newsha"}}
        pull_responses = [stale_pr, new_pr]

        def fake_api(path, *args):
            if path == f"repos/{land.REPO}/pulls/162":
                return pull_responses.pop(0)
            raise AssertionError(f"unexpected api call: {path}")

        run = {"id": 42, "status": "in_progress", "conclusion": "action_required", "html_url": "http://run/42"}
        jobs = [{"name": "test", "status": "completed", "conclusion": "success"}]

        with unittest.mock.patch.object(land, "api", side_effect=fake_api), \
             unittest.mock.patch.object(land.time, "sleep") as mock_sleep, \
             unittest.mock.patch.object(land, "gh") as mock_gh, \
             unittest.mock.patch.object(land, "refusal", return_value=None) as mock_refusal, \
             unittest.mock.patch.object(land, "pull_request_runs", return_value=[run]) as mock_runs, \
             unittest.mock.patch.object(land, "run_jobs", return_value=jobs):
            land.approve_and_wait(162, started="2026-10-23T00:00:00Z", sha="newsha")

            mock_sleep.assert_called_with(land.PAUSE)
            mock_refusal.assert_called_once_with(new_pr, "2026-10-23T00:00:00Z")
            mock_runs.assert_called_with("newsha")
            for c in mock_runs.call_args_list:
                self.assertEqual(c[0][0], "newsha")
            mock_gh.assert_called_once_with(
                "api", "--method", "POST", f"repos/{land.REPO}/actions/runs/42/approve"
            )

    def test_approve_and_wait_sha_never_appears_raises_system_exit(self):
        stale_pr = {"number": 162, "head": {"sha": "stalesha"}}
        with unittest.mock.patch.object(land, "POLLS", 3), \
             unittest.mock.patch.object(land, "api", return_value=stale_pr) as mock_api, \
             unittest.mock.patch.object(land.time, "sleep") as mock_sleep:
            with self.assertRaises(SystemExit) as ctx:
                land.approve_and_wait(162, started="2026-10-23T00:00:00Z", sha="newsha")
            self.assertIn("PR #162: head is stalesha, not the rebuilt newsha", str(ctx.exception))
            self.assertEqual(mock_api.call_count, 3)
            self.assertEqual(mock_sleep.call_count, 3)

    def _wait(self, run_states):
        pr = {"number": 7, "head": {"sha": "abc"}}
        runs = [[dict(state, id=9, html_url="http://run/9")] for state in run_states]
        with unittest.mock.patch.object(land, "POLLS", 3), \
             unittest.mock.patch.object(land, "api", return_value=pr), \
             unittest.mock.patch.object(land.time, "sleep"), \
             unittest.mock.patch.object(land, "gh"), \
             unittest.mock.patch.object(land, "refusal", return_value=None), \
             unittest.mock.patch.object(land, "pull_request_runs", side_effect=runs), \
             unittest.mock.patch.object(land, "run_jobs", return_value=[]):
            land.approve_and_wait(7, started="2026-10-23T00:00:00Z")

    def test_approval_that_never_takes_names_action_required(self):
        waiting = {"status": "completed", "conclusion": "action_required"}
        with self.assertRaises(SystemExit) as ctx:
            self._wait([waiting] * 4)
        self.assertIn("PR #7: CI on abc still action_required, approval did not take", str(ctx.exception))

    def test_approval_lag_then_success(self):
        waiting = {"status": "completed", "conclusion": "action_required"}
        done = {"status": "completed", "conclusion": "success"}
        self._wait([waiting, waiting, done])


if __name__ == "__main__":
    unittest.main()
