"""Parser for workflow and ruleset files used by the required-check replay test."""

import itertools
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

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
    name: str | None = None
    matrix: dict[str, MatrixSpan] = None

    def __post_init__(self) -> None:
        """Initialize empty matrix mapping when omitted."""
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
        if line.startswith(("---", "...")):
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
    """Return whether inline trigger matches pull request event names."""
    clean = rest.split("#", 1)[0].strip()
    if clean.startswith("[") and clean.endswith("]"):
        items = {x.strip().strip("'\"") for x in clean[1:-1].split(",") if x.strip()}
        return bool(items & pr_events)
    return clean.strip("'\"") in pr_events


def _check_trigger_line(line: str, indent: int, pr_events: set[str]) -> bool:
    """Return whether an indented event line matches pull request event names."""
    stripped = line.strip()
    if indent == 2 and stripped.startswith("-"):
        item = stripped[1:].strip().split("#", 1)[0].strip().strip("'\"")
        return item in pr_events
    if indent == 2 and ":" in stripped:
        key = stripped.split(":", 1)[0].strip().strip("'\"")
        return key in pr_events
    return False


def _validate_branch_glob(branch: str) -> None:
    """Raise ValueError when branch name contains glob wildcard characters."""
    for ch in ("*", "?", "[", "!"):
        if ch in branch:
            raise ValueError(f"Glob pattern in branches unsupported: {branch}")


REQUIRED_PR_TYPES = frozenset({"opened", "synchronize", "reopened"})
PR_EVENT_CHILD_KEYS = frozenset({"branches", "types"})
_PLAIN_FLOW_RE = re.compile(r"^[A-Za-z0-9_.()/ -]+$")


def _skip_ws(raw: str, i: int, n: int) -> int:
    """Return next character index past leading whitespace characters."""
    while i < n and raw[i].isspace():
        i += 1
    return i


def _parse_flow_quoted(raw: str, i: int, n: int) -> tuple[str, int]:
    """Return unquoted text and next offset, or raise ValueError on invalid syntax."""
    quote_char = raw[i]
    end_quote = raw.find(quote_char, i + 1)
    if end_quote == -1:
        raise ValueError(f"Unterminated quote in flow sequence: {raw}")
    content = raw[i + 1 : end_quote]
    j = _skip_ws(raw, end_quote + 1, n)
    if j < n and raw[j] != ",":
        raise ValueError(f"Trailing text after closing quote in flow sequence: {raw}")
    return content, j


def _parse_flow_plain(raw: str, i: int, n: int) -> tuple[str, int]:
    """Return plain scalar and next offset, or raise ValueError on disallowed characters."""
    comma_idx = raw.find(",", i)
    end_idx = n if comma_idx == -1 else comma_idx
    token = raw[i:end_idx].strip()
    if "'" in token or '"' in token:
        raise ValueError(f"Quote inside plain scalar in flow sequence: {raw}")
    if not _PLAIN_FLOW_RE.match(token):
        raise ValueError(f"Invalid characters in plain scalar: {token}")
    return token, end_idx


def _split_flow_sequence(raw: str) -> list[str]:
    """Split comma-separated flow sequence according to allow-list rules."""
    if not raw.strip():
        return []
    items: list[str] = []
    i = 0
    n = len(raw)
    expect_item = True
    while i < n:
        i = _skip_ws(raw, i, n)
        if i >= n:
            break
        if raw[i] == ",":
            raise ValueError(f"Empty item in flow sequence: {raw}")
        if raw[i] in ("'", '"'):
            item, i = _parse_flow_quoted(raw, i, n)
        else:
            item, i = _parse_flow_plain(raw, i, n)
        items.append(item)
        expect_item = False
        i = _skip_ws(raw, i, n)
        if i < n and raw[i] == ",":
            i += 1
            expect_item = True
    if expect_item:
        raise ValueError(f"Empty item at end of flow sequence: {raw}")
    return items


def _parse_list_flow(rest: str) -> list[str]:
    """Return item strings from bracketed flow sequence or single scalar."""
    clean = rest.split("#", 1)[0].strip()
    if clean.startswith("[") and clean.endswith("]"):
        return _split_flow_sequence(clean[1:-1])
    if clean:
        return [clean.strip("'\"")]
    return []


def _parse_list_block(
    lines: list[str], start_idx: int, indent: int
) -> tuple[list[str], int]:
    """Return list items parsed from block sequence lines and next line index."""
    items: list[str] = []
    j = start_idx
    total = len(lines)
    for _ in range(start_idx, total):
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
        items.append(val)
        j += 1
    return items, j


def _parse_list_entry(
    lines: list[str], idx: int, rest: str, indent: int
) -> tuple[list[str], int]:
    """Return parsed list items from flow or block syntax and next line index."""
    clean_rest = rest.split("#", 1)[0].strip()
    if clean_rest:
        return _parse_list_flow(clean_rest), idx + 1
    return _parse_list_block(lines, idx + 1, indent)


def _parse_branches_entry(
    lines: list[str], idx: int, rest: str, indent: int
) -> tuple[list[str], int]:
    """Return branch names and next line index, or raise ValueError on wildcards."""
    branches, next_idx = _parse_list_entry(lines, idx, rest, indent)
    for b in branches:
        _validate_branch_glob(b)
    return branches, next_idx


def _parse_types_entry(
    lines: list[str], idx: int, rest: str, indent: int
) -> tuple[list[str], int]:
    """Return event types and next index, or raise ValueError on missing required types."""
    types_list, next_idx = _parse_list_entry(lines, idx, rest, indent)
    if not REQUIRED_PR_TYPES.issubset(types_list):
        missing = sorted(REQUIRED_PR_TYPES - set(types_list))
        raise ValueError(
            f"types under PR event missing required types {missing} at line {idx + 1}"
        )
    return types_list, next_idx


def _handle_pr_event_child(
    lines: list[str], j: int, stripped: str, indent: int
) -> tuple[bool | None, int]:
    """Return whether main is targeted and next index, or raise ValueError on unknown keys."""
    if ":" not in stripped:
        raise ValueError(f"Unsupported entry in PR event at line {j + 1}: {stripped}")
    key = stripped.split(":", 1)[0].strip().strip("'\"")
    if key not in PR_EVENT_CHILD_KEYS:
        raise ValueError(f"{key} unsupported in PR event at line {j + 1}")
    rest = stripped.split(":", 1)[1].strip()
    if key == "branches":
        branches, next_j = _parse_branches_entry(lines, j, rest, indent)
        return ("main" in branches), next_j
    if key == "types":
        _, next_j = _parse_types_entry(lines, j, rest, indent)
        return None, next_j
    return None, j + 1


def _parse_pr_event_block(lines: list[str], start_idx: int) -> tuple[bool, int]:
    """Parse PR event block. Returns (triggers_main, next_line_idx)."""
    has_branches = False
    triggers_main = False
    j = start_idx + 1
    total = len(lines)
    for _ in range(start_idx + 1, total):
        if j >= total:
            break
        line = lines[j]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            j += 1
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= 2:
            break
        branch_match, j = _handle_pr_event_child(lines, j, stripped, indent)
        if branch_match is not None:
            has_branches = True
            triggers_main = branch_match
    if not has_branches:
        triggers_main = True
    return triggers_main, j


def _parse_on_body_line(
    lines: list[str],
    i: int,
    indent: int,
    stripped: str,
    pr_events: set[str],
    current_triggers: bool,
) -> tuple[bool, int]:
    """Return updated trigger flag and next index, or raise ValueError on inline mapping."""
    if indent == 2 and stripped.startswith("-"):
        return (
            current_triggers or _check_trigger_line(lines[i], indent, pr_events)
        ), i + 1
    if indent == 2 and ":" in stripped:
        key = stripped.split(":", 1)[0].strip().strip("'\"")
        if key in pr_events:
            rest = stripped.split(":", 1)[1].split("#", 1)[0].strip()
            if rest not in ("", "{}", "null", "~"):
                raise ValueError(
                    f"Inline mapping for {key} unsupported at line {i + 1}"
                )
            ev_triggers, next_i = _parse_pr_event_block(lines, i)
            return (current_triggers or ev_triggers), next_i
    return current_triggers, i + 1


def parse_on_trigger(lines: list[str]) -> bool:
    """Determine whether workflow triggers on pull_request or pull_request_target."""
    pr_events = {"pull_request", "pull_request_target"}
    in_on = False
    triggers_pr = False
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
            in_on = line.startswith("on:")
            if in_on and line[3:].strip():
                return _parse_inline_trigger(line[3:].strip(), pr_events)
            i += 1
            continue
        if in_on:
            triggers_pr, i = _parse_on_body_line(
                lines, i, indent, stripped, pr_events, triggers_pr
            )
        else:
            i += 1
    return triggers_pr


def _parse_matrix_flow(
    key: str, rest: str, idx: int, indent: int
) -> tuple[MatrixSpan, int]:
    """Return MatrixSpan and next index, or raise ValueError on malformed values."""
    clean = rest.split("#", 1)[0].strip()
    if clean.startswith("[") and clean.endswith("]"):
        items = _split_flow_sequence(clean[1:-1])
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
    """Return parsed sequence items from matrix block lines and next line index."""
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
    """Return MatrixSpan and next index, or raise ValueError if block values are empty."""
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
    """Return next line index after updating job name or matrix configuration."""
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
    current: JobDef | None,
) -> tuple[JobDef | None, int]:
    """Return active job definition and next line index after processing a job line."""
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
    current_job: JobDef | None = None
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
    total_combinations = math.prod(len(v) for v in value_lists)
    if total_combinations > 256:
        raise ValueError(
            f"Matrix combinations ({total_combinations}) exceed GitHub limit of 256"
        )
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
    with open(ruleset_path, encoding="utf-8") as f:
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
