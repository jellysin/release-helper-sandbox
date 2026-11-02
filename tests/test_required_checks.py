"""Replay fixture proving every required context in main.json is reported.

Validates that all required status check contexts declared in
.github/rulesets/main.json are reported by workflows triggered on pull_request.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from required_checks_parser import (
    MAIN_RULESET_PATH,
    WORKFLOWS_DIR,
    JobDef,
    MatrixSpan,
    _split_flow_sequence,
    get_all_reported_contexts,
    get_job_reported_contexts,
    get_required_contexts,
    get_workflow_reported_contexts,
    parse_workflow_file,
    parse_workflow_string,
    rewrite_matrix_span,
    verify_required_checks,
)


class TestRequiredChecks(unittest.TestCase):
    """Test suite validating required status checks against PR workflows."""

    def test_main_ruleset_contexts_all_reported(self):
        """Positive test: every required context in main.json is reported."""
        missing = verify_required_checks(MAIN_RULESET_PATH, WORKFLOWS_DIR)
        self.assertEqual(
            missing,
            set(),
            f"Required status checks missing from workflows: {missing}",
        )

    def test_codeql_contexts_required(self):
        """Verify analyze (python) and analyze (actions) are required and reported."""
        required = get_required_contexts(MAIN_RULESET_PATH)
        self.assertIn("analyze (python)", required)
        self.assertIn("analyze (actions)", required)

        codeql_wf = parse_workflow_file(WORKFLOWS_DIR / "codeql.yml")
        reported = get_workflow_reported_contexts(codeql_wf)
        self.assertIn("analyze (python)", reported)
        self.assertIn("analyze (actions)", reported)

    def test_negative_unreported_context(self):
        """Negative test: analyze (go) is not reported by any PR workflow."""
        reported = get_all_reported_contexts(WORKFLOWS_DIR)
        self.assertNotIn("analyze (go)", reported)

    def test_negative_push_only_workflow(self):
        """Negative test: push-only workflow does not report PR checks."""
        yaml_content = "name: Push Only\non:\n  push:\n    branches: [main]\njobs:\n  build:\n    name: Build\n"
        wf = parse_workflow_string(yaml_content, "push_only.yml")
        self.assertFalse(wf.triggers_pr)
        self.assertEqual(get_workflow_reported_contexts(wf), set())

    def test_boundary_name_expansion_and_plain_job(self):
        """Boundary test: matrix expansion in name vs default id, and plain jobs."""
        job_template = JobDef(
            job_id="test",
            name="Test (${{ matrix.language }})",
            matrix={
                "language": MatrixSpan(
                    key="language",
                    values=["python", "actions"],
                    start_line=0,
                    end_line=1,
                    indent=8,
                )
            },
        )
        self.assertEqual(
            set(get_job_reported_contexts(job_template)),
            {"Test (python)", "Test (actions)"},
        )

        job_no_name = JobDef(
            job_id="analyze",
            name=None,
            matrix={
                "language": MatrixSpan(
                    key="language",
                    values=["python", "actions"],
                    start_line=0,
                    end_line=1,
                    indent=8,
                )
            },
        )
        self.assertEqual(
            set(get_job_reported_contexts(job_no_name)),
            {"analyze (python)", "analyze (actions)"},
        )

        job_static_name = JobDef(
            job_id="scan",
            name="CodeQL",
            matrix={"language": job_template.matrix["language"]},
        )
        self.assertEqual(
            set(get_job_reported_contexts(job_static_name)),
            {"CodeQL (python)", "CodeQL (actions)"},
        )

        job_plain_name = JobDef(job_id="checks", name="CI", matrix={})
        self.assertEqual(get_job_reported_contexts(job_plain_name), ["CI"])

        job_plain_no_name = JobDef(job_id="build", name=None, matrix={})
        self.assertEqual(get_job_reported_contexts(job_plain_no_name), ["build"])

    def test_unsupported_matrix_include_exclude_raises(self):
        """Ensure reader raises ValueError on matrix include/exclude."""
        yaml_inc = (
            "name: Test\n"
            "on: [pull_request]\n"
            "jobs:\n"
            "  test:\n"
            "    strategy:\n"
            "      matrix:\n"
            "        language: [python]\n"
            "        include:\n"
            "          - language: actions\n"
        )
        with self.assertRaises(ValueError):
            parse_workflow_string(yaml_inc)

        yaml_exc = (
            "name: Test\n"
            "on: [pull_request]\n"
            "jobs:\n"
            "  test:\n"
            "    strategy:\n"
            "      matrix:\n"
            "        language: [python, actions]\n"
            "        exclude:\n"
            "          - language: actions\n"
        )
        with self.assertRaises(ValueError):
            parse_workflow_string(yaml_exc)

    def test_unsupported_shapes_raise_value_error(self):
        """Ensure reader raises ValueError on multi-doc, anchors, or bad name expressions."""
        with self.assertRaises(ValueError):
            parse_workflow_string("---\nname: doc1\n---\nname: doc2\n")

        with self.assertRaises(ValueError):
            parse_workflow_string("name: test\nenv: &default\n  FOO: bar\n")

        yaml_bad_name = "name: Test\non: [pull_request]\njobs:\n  build:\n    name: Build ${{ env.TARGET }}\n"
        with self.assertRaises(ValueError):
            parse_workflow_string(yaml_bad_name)

    def test_all_real_workflows_parse(self):
        """All real workflows in .github/workflows must parse cleanly."""
        for path in sorted(WORKFLOWS_DIR.glob("*.y*ml")):
            wf = parse_workflow_file(path)
            self.assertIsNotNone(wf)
            self.assertTrue(len(wf.jobs) > 0)

    def test_planted_missing_actions_matrix_fails(self):
        """Planted test: rewrite language matrix via span to omit actions; fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_workflows = Path(tmpdir) / "workflows"
            shutil.copytree(WORKFLOWS_DIR, tmp_workflows)
            codeql_file = tmp_workflows / "codeql.yml"

            wf = parse_workflow_file(codeql_file)
            analyze_job = wf.jobs.get("analyze")
            self.assertIsNotNone(analyze_job)
            lang_span = analyze_job.matrix.get("language")
            self.assertIsNotNone(lang_span)

            content = codeql_file.read_text(encoding="utf-8")
            rewritten = rewrite_matrix_span(content, lang_span, ["python"])
            codeql_file.write_text(rewritten, encoding="utf-8")

            copy_text = codeql_file.read_text(encoding="utf-8")
            self.assertIn("python", copy_text)
            reparsed_wf = parse_workflow_file(codeql_file)
            reparsed_job = reparsed_wf.jobs["analyze"]
            self.assertEqual(reparsed_job.matrix["language"].values, ["python"])

            missing = verify_required_checks(MAIN_RULESET_PATH, tmp_workflows)
            self.assertEqual(missing, {"analyze (actions)"})

    def test_negative_push_branches_pull_request_not_pr_trigger(self):
        """Negative test: push-only workflow with branch named pull_request does not trigger on PR."""
        yaml_content = (
            "name: Push Only\non:\n  push:\n    branches:\n      - pull_request\njobs:\n  build:\n    name: Build\n"
        )
        wf = parse_workflow_string(yaml_content)
        self.assertFalse(wf.triggers_pr)
        self.assertEqual(get_workflow_reported_contexts(wf), set())

    def test_pr_trigger_branch_filters(self):
        """PR trigger respects branches filter: main -> True, dev -> False, block main -> True."""
        wf_flow_main = parse_workflow_string(
            "name: Flow Main\non:\n  pull_request:\n    branches: [main]\njobs:\n  build:\n    name: Build\n"
        )
        self.assertTrue(wf_flow_main.triggers_pr)

        wf_flow_dev = parse_workflow_string(
            "name: Flow Dev\non:\n  pull_request:\n    branches: [dev]\njobs:\n  build:\n    name: Build\n"
        )
        self.assertFalse(wf_flow_dev.triggers_pr)

        wf_block_main = parse_workflow_string(
            "name: Block Main\n"
            "on:\n"
            "  pull_request:\n"
            "    branches:\n"
            "      - dev\n"
            "      - main\n"
            "jobs:\n"
            "  build:\n"
            "    name: Build\n"
        )
        self.assertTrue(wf_block_main.triggers_pr)

    def test_pr_unsupported_branch_filters_raise(self):
        """branches-ignore and glob patterns in PR branches raise ValueError."""
        wf_ignore = "name: Ignore\non:\n  pull_request:\n    branches-ignore: [dev]\njobs:\n  build:\n    name: Build\n"
        with self.assertRaises(ValueError):
            parse_workflow_string(wf_ignore)

        wf_glob = "name: Glob\non:\n  pull_request:\n    branches: ['release/*']\njobs:\n  build:\n    name: Build\n"
        with self.assertRaises(ValueError):
            parse_workflow_string(wf_glob)

        wf_inline = "name: Inline\non:\n  pull_request: {branches: [dev]}\njobs:\n  build:\n    name: Build\n"
        with self.assertRaises(ValueError):
            parse_workflow_string(wf_inline)

        wf_empty = parse_workflow_string("name: Empty\non:\n  pull_request: {}\njobs:\n  build:\n    name: Build\n")
        self.assertTrue(wf_empty.triggers_pr)

    def test_matrix_expansion_limit_and_boundary(self):
        """Matrix expansion > 256 raises ValueError; 256 exactly succeeds."""
        job_343 = JobDef(
            job_id="test",
            name="Test",
            matrix={
                f"k{i}": MatrixSpan(
                    key=f"k{i}",
                    values=[f"v{j}" for j in range(7)],
                    start_line=0,
                    end_line=1,
                    indent=8,
                )
                for i in range(3)
            },
        )
        with self.assertRaises(ValueError):
            get_job_reported_contexts(job_343)

        job_256 = JobDef(
            job_id="test",
            name="Test",
            matrix={
                f"k{i}": MatrixSpan(
                    key=f"k{i}",
                    values=[f"v{j}" for j in range(16)],
                    start_line=0,
                    end_line=1,
                    indent=8,
                )
                for i in range(2)
            },
        )
        contexts_256 = get_job_reported_contexts(job_256)
        self.assertEqual(len(contexts_256), 256)

    def test_pr_unsupported_path_filters_raise(self):
        """paths and paths-ignore under pull_request and pull_request_target raise ValueError."""
        for event in ("pull_request", "pull_request_target"):
            for key in ("paths", "paths-ignore"):
                wf_content = f"name: Test\non:\n  {event}:\n    {key}: ['src/**']\njobs:\n  build:\n    name: Build\n"
                with self.assertRaises(ValueError):
                    parse_workflow_string(wf_content)

    def test_matrix_flow_quoted_scalars_and_unterminated(self):
        """Matrix flow sequence handles quotes and detects unterminated quotes."""
        wf_single = (
            "name: Test\n"
            "on: [pull_request]\n"
            "jobs:\n"
            "  test:\n"
            "    strategy:\n"
            "      matrix:\n"
            '        language: ["python,actions"]\n'
        )
        wf = parse_workflow_string(wf_single)
        self.assertEqual(wf.jobs["test"].matrix["language"].values, ["python,actions"])

        wf_two = (
            "name: Test\n"
            "on: [pull_request]\n"
            "jobs:\n"
            "  test:\n"
            "    strategy:\n"
            "      matrix:\n"
            "        language: [python, 'actions']\n"
        )
        wf = parse_workflow_string(wf_two)
        self.assertEqual(wf.jobs["test"].matrix["language"].values, ["python", "actions"])

        wf_unterminated = (
            "name: Test\n"
            "on: [pull_request]\n"
            "jobs:\n"
            "  test:\n"
            "    strategy:\n"
            "      matrix:\n"
            '        language: ["python, actions]\n'
        )
        with self.assertRaises(ValueError):
            parse_workflow_string(wf_unterminated)

    def test_branches_flow_quoted_comma_not_main(self):
        """Quoted comma in branches flow sequence does not match main."""
        wf = parse_workflow_string(
            'name: Flow Quoted\non:\n  pull_request:\n    branches: ["main,dev"]\njobs:\n  build:\n    name: Build\n'
        )
        self.assertFalse(wf.triggers_pr)

    def test_pr_unsupported_child_keys_raise(self):
        """pull_request child tags: and unknown key foo: raise ValueError."""
        wf_tags = "name: Tags\non:\n  pull_request:\n    tags: [v1]\njobs:\n  build:\n    name: Build\n"
        with self.assertRaises(ValueError):
            parse_workflow_string(wf_tags)

        wf_foo = "name: Foo\non:\n  pull_request:\n    foo: bar\njobs:\n  build:\n    name: Build\n"
        with self.assertRaises(ValueError):
            parse_workflow_string(wf_foo)

    def test_pr_types_validation(self):
        """PR event types validation for flow and block lists."""
        wf_labeled = (
            "name: Labeled Only\non:\n  pull_request:\n    types: [labeled]\njobs:\n  build:\n    name: Build\n"
        )
        with self.assertRaises(ValueError):
            parse_workflow_string(wf_labeled)

        wf_flow_all = (
            "name: Valid Flow Types\n"
            "on:\n"
            "  pull_request:\n"
            "    types: [opened, synchronize, reopened, labeled]\n"
            "jobs:\n"
            "  build:\n"
            "    name: Build\n"
        )
        wf = parse_workflow_string(wf_flow_all)
        self.assertTrue(wf.triggers_pr)

        wf_block_all = (
            "name: Valid Block Types\n"
            "on:\n"
            "  pull_request:\n"
            "    types:\n"
            "      - opened\n"
            "      - synchronize\n"
            "      - reopened\n"
            "jobs:\n"
            "  build:\n"
            "    name: Build\n"
        )
        wf_block = parse_workflow_string(wf_block_all)
        self.assertTrue(wf_block.triggers_pr)

        wf_block_missing = (
            "name: Block Missing Reopened\n"
            "on:\n"
            "  pull_request:\n"
            "    types:\n"
            "      - opened\n"
            "      - synchronize\n"
            "jobs:\n"
            "  build:\n"
            "    name: Build\n"
        )
        with self.assertRaises(ValueError):
            parse_workflow_string(wf_block_missing)

    def test_flow_sequence_allowlist_and_edge_cases(self):
        """Flow sequence parses allowed shapes and rejects invalid shapes."""
        with self.assertRaises(ValueError):
            _split_flow_sequence("it's, main")

        with self.assertRaises(ValueError):
            _split_flow_sequence("'it''s'")

        self.assertEqual(_split_flow_sequence('"a,b", c'), ["a,b", "c"])

        with self.assertRaises(ValueError):
            _split_flow_sequence('"a" x')

        with self.assertRaises(ValueError):
            _split_flow_sequence("a,,b")

    def test_pr_event_non_key_entry_raises(self):
        """A list item directly under pull_request is not an allowed child key."""
        yaml_content = "name: Stray\non:\n  pull_request:\n    - main\njobs:\n  build:\n    name: Build\n"
        with self.assertRaises(ValueError):
            parse_workflow_string(yaml_content)


if __name__ == "__main__":
    unittest.main()
