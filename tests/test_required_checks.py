"""Replay fixture proving every required context in main.json is reported.

Validates that all required status check contexts declared in
.github/rulesets/main.json are reported by workflows triggered on pull_request.
"""

from dataclasses import dataclass
import itertools
import json
from pathlib import Path
import re
import shutil
import tempfile
from typing import Optional
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
MAIN_RULESET_PATH = REPO_ROOT / ".github" / "rulesets" / "main.json"


@dataclass(frozen=True)
class MatrixSpan:
    """Span of a matrix key definition in the workflow file."""

    key: str
    values: list[str]
    start_line: int
    end_line: int
    indent: int


@dataclass
class JobDef:
    """Representation of a job parsed from a workflow."""

    job_id: str
    name: Optional[str] = None
    matrix: dict[str, MatrixSpan] = None

    def __post_init__(self) -> None:
        if self.matrix is None:
            self.matrix = {}


@dataclass
class WorkflowDef:
    """Representation of a parsed workflow."""

    name: str
    triggers_pr: bool
    jobs: dict[str, JobDef]


def check_unsupported_tokens(lines: list[str]) -> None:
    """Refuse multi-document YAML and anchors/aliases."""
    for idx, raw in enumerate(lines):
        line = raw.strip()
        if line.startswith("---") or line.startswith("..."):
            raise ValueError(f"Multi-document YAML unsupported at line {idx + 1}")
        if re.search(r"(?:^|\s)[&*][A-Za-z0-9_-]+", line):
            raise ValueError(f"YAML anchors/aliases unsupported at line {idx + 1}")


def parse_job_name(raw_val: str) -> str:
    """Parse and validate scalar job name, permitting only matrix expressions."""
    val = raw_val.split("#", 1)[0].strip()
    if (val.startswith("'") and val.endswith("'")) or (
        val.startswith('"') and val.endswith('"')
    ):
        val = val[1:-1].strip()
    expressions = re.findall(r"\$\{\{\s*([^}]+?)\s*\}\}", val)
    for expr in expressions:
        if not re.fullmatch(r"matrix\.[A-Za-z0-9_-]+", expr.strip()):
            raise ValueError(f"Unsupported expression in job name: {expr}")
    return val


def _parse_inline_trigger(rest: str, pr_events: set[str]) -> bool:
    clean = rest.split("#", 1)[0].strip()
    if clean.startswith("[") and clean.endswith("]"):
        items = {x.strip().strip("'\"") for x in clean[1:-1].split(",") if x.strip()}
        return bool(items & pr_events)
    return clean.strip("'\"") in pr_events


def _check_trigger_line(line: str, indent: int, pr_events: set[str]) -> bool:
    stripped = line.strip()
    if stripped.startswith("-"):
        item = stripped[1:].strip().split("#", 1)[0].strip().strip("'\"")
        return item in pr_events
    if indent == 2 and ":" in stripped:
        key = stripped.split(":", 1)[0].strip().strip("'\"")
        return key in pr_events
    return False


def parse_on_trigger(lines: list[str]) -> bool:
    """Determine whether workflow triggers on pull_request or pull_request_target."""
    pr_events = {"pull_request", "pull_request_target"}
    in_on = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_on = line.startswith("on:")
            if in_on and line[3:].strip():
                return _parse_inline_trigger(line[3:].strip(), pr_events)
        elif in_on and _check_trigger_line(line, indent, pr_events):
            return True
    return False


def _parse_matrix_flow(
    key: str, rest: str, idx: int, indent: int
) -> tuple[MatrixSpan, int]:
    clean = rest.split("#", 1)[0].strip()
    if clean.startswith("[") and clean.endswith("]"):
        items = [x.strip().strip("'\"") for x in clean[1:-1].split(",") if x.strip()]
        if not items:
            raise ValueError(f"Empty flow matrix for key {key}")
        span = MatrixSpan(
            key=key,
            values=items,
            start_line=idx,
            end_line=idx + 1,
            indent=indent,
        )
        return span, idx + 1
    raise ValueError(f"Unsupported matrix value format for key {key}: {rest}")


def _parse_matrix_block_items(
    lines: list[str], idx: int, indent: int
) -> tuple[list[str], int]:
    values: list[str] = []
    j = idx + 1
    total = len(lines)
    for _ in range(j, total):
        if j >= total:
            break
        sub = lines[j]
        sub_stripped = sub.strip()
        if not sub_stripped or sub_stripped.startswith("#"):
            j += 1
            continue
        sub_indent = len(sub) - len(sub.lstrip(" "))
        if sub_indent <= indent or not sub_stripped.startswith("-"):
            break
        val = sub_stripped[1:].strip().split("#", 1)[0].strip().strip("'\"")
        values.append(val)
        j += 1
    return values, j


def _parse_matrix_key(
    lines: list[str], idx: int, key: str, rest: str, indent: int
) -> tuple[MatrixSpan, int]:
    if rest:
        return _parse_matrix_flow(key, rest, idx, indent)
    values, end_line = _parse_matrix_block_items(lines, idx, indent)
    if not values:
        raise ValueError(f"Empty block matrix for key {key}")
    span = MatrixSpan(
        key=key,
        values=values,
        start_line=idx,
        end_line=end_line,
        indent=indent,
    )
    return span, end_line


def parse_matrix_block(
    lines: list[str], start_idx: int
) -> tuple[dict[str, MatrixSpan], int]:
    """Parse matrix keys and values under strategy.matrix."""
    matrix: dict[str, MatrixSpan] = {}
    i = start_idx
    total = len(lines)
    for _ in range(start_idx, total):
        if i >= total:
            break
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent < 8:
            break
        if indent == 8 and ":" in stripped:
            key, rest = stripped.split(":", 1)
            key = key.strip()
            if key in ("include", "exclude"):
                raise ValueError(f"Matrix {key} is unsupported")
            span, next_i = _parse_matrix_key(lines, i, key, rest.strip(), indent)
            matrix[key] = span
            i = next_i
            continue
        raise ValueError(f"Unexpected matrix indentation at line {i + 1}")
    return matrix, i


def _handle_job_body_line(
    lines: list[str], i: int, current_job: JobDef
) -> int:
    line = lines[i]
    stripped = line.strip()
    indent = len(line) - len(line.lstrip(" "))
    if indent == 4 and stripped.startswith("name:"):
        current_job.name = parse_job_name(stripped[5:])
        return i + 1
    if indent == 6 and stripped == "matrix:":
        spans, next_i = parse_matrix_block(lines, i + 1)
        current_job.matrix.update(spans)
        return next_i
    return i + 1


def _handle_job_line(
    lines: list[str],
    i: int,
    jobs: dict[str, JobDef],
    current: Optional[JobDef],
) -> tuple[Optional[JobDef], int]:
    line = lines[i]
    indent = len(line) - len(line.lstrip(" "))
    if indent == 2 and line.startswith("  ") and line.strip().endswith(":"):
        job_id = line.strip()[:-1].strip()
        new_job = JobDef(job_id=job_id)
        jobs[job_id] = new_job
        return new_job, i + 1
    if current is not None:
        next_i = _handle_job_body_line(lines, i, current)
        return current, next_i
    return current, i + 1


def parse_jobs(lines: list[str]) -> dict[str, JobDef]:
    """Parse jobs dictionary from workflow lines."""
    jobs: dict[str, JobDef] = {}
    in_jobs = False
    current_job: Optional[JobDef] = None
    i = 0
    total = len(lines)
    for _ in range(total):
        if i >= total:
            break
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_jobs = line.startswith("jobs:")
            current_job = None
            i += 1
            continue
        if in_jobs:
            current_job, i = _handle_job_line(lines, i, jobs, current_job)
        else:
            i += 1
    return jobs


def parse_workflow_lines(lines: list[str]) -> WorkflowDef:
    """Parse a workflow from raw lines."""
    check_unsupported_tokens(lines)
    name = ""
    for line in lines:
        stripped = line.strip()
        if not line.startswith(" ") and stripped.startswith("name:"):
            name = parse_job_name(stripped[5:])
            break
    triggers_pr = parse_on_trigger(lines)
    jobs = parse_jobs(lines)
    return WorkflowDef(name=name, triggers_pr=triggers_pr, jobs=jobs)


def parse_workflow_string(content: str, filename: str = "<memory>") -> WorkflowDef:
    """Parse a workflow from a string."""
    lines = content.splitlines()
    return parse_workflow_lines(lines)


def parse_workflow_file(path: Path) -> WorkflowDef:
    """Parse a workflow file from disk."""
    content = path.read_text(encoding="utf-8")
    lines = content.splitlines()
    return parse_workflow_lines(lines)


def get_job_reported_contexts(job: JobDef) -> list[str]:
    """Compute status check context names reported by a job."""
    if not job.matrix:
        return [job.name if job.name is not None else job.job_id]

    keys = list(job.matrix.keys())
    value_lists = [job.matrix[k].values for k in keys]
    contexts: list[str] = []
    for combo in itertools.product(*value_lists):
        combo_map = dict(zip(keys, combo))
        if job.name is not None and re.search(r"\$\{\{\s*matrix\.", job.name):
            ctx = job.name
            for k, v in combo_map.items():
                pattern = rf"\$\{{\{{\s*matrix\.{re.escape(k)}\s*\}}\}}"
                ctx = re.sub(pattern, v, ctx)
            contexts.append(ctx)
        else:
            base = job.name if job.name is not None else job.job_id
            contexts.append(f"{base} ({', '.join(combo)})")
    return contexts


def get_workflow_reported_contexts(workflow: WorkflowDef) -> set[str]:
    """Return status check contexts reported on pull_request by this workflow."""
    if not workflow.triggers_pr:
        return set()
    contexts: set[str] = set()
    for job in workflow.jobs.values():
        contexts.update(get_job_reported_contexts(job))
    return contexts


def get_all_reported_contexts(workflows_dir: Path) -> set[str]:
    """Scan workflows dir and return all contexts reported on pull_request."""
    reported: set[str] = set()
    for path in sorted(workflows_dir.glob("*.y*ml")):
        wf = parse_workflow_file(path)
        reported.update(get_workflow_reported_contexts(wf))
    return reported


def get_required_contexts(ruleset_path: Path) -> set[str]:
    """Extract required status check contexts from a ruleset JSON file."""
    with open(ruleset_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    contexts: set[str] = set()
    for rule in data.get("rules", []):
        if rule.get("type") == "required_status_checks":
            checks = rule.get("parameters", {}).get("required_status_checks", [])
            for c in checks:
                if "context" in c:
                    contexts.add(c["context"])
    if not contexts:
        raise ValueError(f"No required status checks found in {ruleset_path}")
    return contexts


def verify_required_checks(ruleset_path: Path, workflows_dir: Path) -> set[str]:
    """Verify that all required checks are reported by pull_request workflows.

    Returns the set of missing contexts (empty set when all are reported).
    """
    required = get_required_contexts(ruleset_path)
    reported = get_all_reported_contexts(workflows_dir)
    return required - reported


def rewrite_matrix_span(content: str, span: MatrixSpan, new_values: list[str]) -> str:
    """Rewrite a matrix key definition structurally using the reader's span."""
    lines = content.splitlines(keepends=True)
    indent_str = " " * span.indent
    formatted_vals = ", ".join(new_values)
    new_line = f"{indent_str}{span.key}: [{formatted_vals}]\n"
    rewritten_lines = lines[: span.start_line] + [new_line] + lines[span.end_line :]
    return "".join(rewritten_lines)


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
        yaml_content = (
            "name: Push Only\n"
            "on:\n"
            "  push:\n"
            "    branches: [main]\n"
            "jobs:\n"
            "  build:\n"
            "    name: Build\n"
        )
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

        yaml_bad_name = (
            "name: Test\n"
            "on: [pull_request]\n"
            "jobs:\n"
            "  build:\n"
            "    name: Build ${{ env.TARGET }}\n"
        )
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


if __name__ == "__main__":
    unittest.main()
