"""Unit tests for .github/scripts/land.py."""

import importlib.util
import os
import sys
import unittest
import unittest.mock
from pathlib import Path

# land.py reads GITHUB_REPOSITORY at import; set os.environ before import
os.environ.setdefault("GITHUB_REPOSITORY", "jellysin/release-helper-sandbox")

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
        self.assertEqual(land.ci_state(run, []), "pending")

        run_waiting = {"status": "action_required", "conclusion": "action_required"}
        self.assertEqual(land.ci_state(run_waiting, []), "pending")

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


if __name__ == "__main__":
    unittest.main()
