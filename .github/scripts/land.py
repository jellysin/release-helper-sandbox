"""Land a change on the protected main through a pull request, as the ruleset requires.

    land.py land BRANCH MESSAGE FILE...   commit FILEs (as in the work tree) on a new BRANCH from main
                                         through the forge API (GitHub signs it), open a PR, let its CI
                                         run and squash-merge when CLEAN; print the merge commit
    land.py merge NUMBER                  let CI run on an open release PR and squash-merge it when CLEAN
    land.py resume                        resume and settle leftover cycle PRs that an earlier cycle left open

A pull request the job token opens starts a pull_request CI run that waits for approval
(action_required), and only pull_request runs count as the PR's required checks. This script approves
such a run, fail closed, only when the PR's author is github-actions[bot], its head is in this
repository, its head commit was made in this cycle (CYCLE_STARTED) and every changed path is on the
cycle's allow-list; anything else is reported and left waiting. A refused approval stops the cycle.

Standard library and gh only, with the job's token. Every wait is bounded; any other state fails.
"""

import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = os.environ["GITHUB_REPOSITORY"]
CI_WORKFLOW = "ci.yml"
BOT = "github-actions[bot]"
# Every path a cycle pull request may change: the record of the run, the release PR's version files
# and the catalog. A pull request touching any other path is not approved.
ALLOWED = {
    "cycle/last-run.txt",
    "version.txt",
    "CHANGELOG.md",
    ".release-please-manifest.json",
    "catalog/manifest.json",
}
POLLS, PAUSE = 60, 10  # at most ten minutes for each wait
LEFTOVER = re.compile(r"^cycle/(\d+)-change$")


def gh(*arguments, data=None):
    done = subprocess.run(["gh", *arguments], input=data, capture_output=True, check=False, timeout=120)
    if done.returncode:
        raise SystemExit(f"gh {' '.join(arguments[:2])} failed: {done.stderr.decode().strip()[:300]}")
    return done.stdout.decode().strip()


def api(path, *arguments):
    output = gh("api", path, *arguments)
    return json.loads(output) if output else None


def signed_commit(branch, base, message, files):
    """Commit the files on branch, whose head is base, through createCommitOnBranch; return the commit."""
    additions = [{"path": name, "contents": base64.b64encode(Path(name).read_bytes()).decode()} for name in files]
    headline, _, body = message.partition("\n")
    query = "mutation($input: CreateCommitOnBranchInput!) { createCommitOnBranch(input: $input) { commit { oid } } }"
    variables = {
        "input": {
            "branch": {"repositoryNameWithOwner": REPO, "branchName": branch},
            "expectedHeadOid": base,
            "message": {"headline": headline, "body": body.strip()},
            "fileChanges": {"additions": additions},
        }
    }
    answer = json.loads(
        gh("api", "graphql", "--input", "-", data=json.dumps({"query": query, "variables": variables}).encode())
    )
    return answer["data"]["createCommitOnBranch"]["commit"]["oid"]


def ci_state(run, jobs):
    """Pure evaluation of CI run and jobs state: 'pending', 'success', or a failure string."""
    if run.get("status") == "completed" and run.get("conclusion") != "action_required":
        if run.get("conclusion") == "success":
            return "success"
        return f"concluded {run.get('conclusion')}"
    if len(jobs) >= 100:
        return "100 jobs or more"
    if jobs and all(job.get("status") == "completed" for job in jobs):
        allowed = {"success", "skipped", "neutral"}
        failed = [
            job.get("name", "unnamed")
            for job in jobs
            if job.get("conclusion") not in allowed
        ]
        if not failed:
            return "success"
        return f"failed jobs: {', '.join(failed)}"
    return "pending"


def run_jobs(run_id):
    """The jobs of the run's latest attempt; their states stand in when the run's own status lags."""
    data = api(f"repos/{REPO}/actions/runs/{run_id}/jobs?per_page=100")
    if data["total_count"] >= 100:
        raise SystemExit(f"run {run_id} has {data['total_count']} jobs, more than one page")
    return data["jobs"]


def refusal(pr, started):
    """Why the cycle may not approve CI for this PR, or None when it may."""
    if pr["user"]["login"] != BOT:
        return f"author is {pr['user']['login']}, not {BOT}"
    if pr["head"]["repo"]["full_name"] != REPO:
        return f"head is in {pr['head']['repo']['full_name']}"
    files = api(f"repos/{REPO}/pulls/{pr['number']}/files?per_page=100")
    outside = sorted(item["filename"] for item in files if item["filename"] not in ALLOWED)
    if len(files) >= 100 or outside:
        return "paths outside the cycle's allow-list: " + (", ".join(outside) or "100 files or more")
    committed = api(f"repos/{REPO}/commits/{pr['head']['sha']}")["commit"]["committer"]["date"]
    if committed < started:
        return f"head commit {pr['head']['sha']} was made at {committed}, before this cycle started"
    return None


def pull_request_runs(sha):
    runs = api(f"repos/{REPO}/actions/workflows/{CI_WORKFLOW}/runs?event=pull_request&head_sha={sha}&per_page=5")
    return runs["workflow_runs"]


def approve_and_wait(number, started=None):
    """Approve the PR's waiting pull_request CI run when the PR qualifies, and wait for it to succeed."""
    if started is None:
        started = os.environ["CYCLE_STARTED"]
    pr = api(f"repos/{REPO}/pulls/{number}")
    sha = pr["head"]["sha"]
    reason = refusal(pr, started)
    if reason is not None:
        raise SystemExit(f"PR #{number}: CI not approved, left waiting: {reason}")
    for _ in range(POLLS):
        runs = pull_request_runs(sha)
        if runs:
            break
        time.sleep(PAUSE)
    else:
        raise SystemExit(f"PR #{number}: no pull_request CI run appeared for {sha}")
    for run in runs:
        if run["conclusion"] == "action_required":
            gh("api", "--method", "POST", f"repos/{REPO}/actions/runs/{run['id']}/approve")
            print(f"PR #{number}: approved CI run {run['html_url']}")
    for _ in range(POLLS):
        run = pull_request_runs(sha)[0]
        jobs = run_jobs(run["id"])
        state = ci_state(run, jobs)
        if state == "success":
            lagging = run.get("status") != "completed"
            tag = " (jobs complete, run status lagging)" if lagging else ""
            print(f"PR #{number}: CI success{tag} {run['html_url']}")
            return
        if state != "pending":
            raise SystemExit(f"PR #{number}: CI {state}: {run['html_url']}")
        time.sleep(PAUSE)
    raise SystemExit(f"PR #{number}: CI on {sha} did not finish in time")


def merge_when_clean(number):
    """Squash-merge the PR once the forge reports it CLEAN; return the merge commit."""
    for _ in range(POLLS):
        state = json.loads(gh("pr", "view", str(number), "--json", "mergeStateStatus,state"))
        if state["mergeStateStatus"] == "CLEAN":
            gh("pr", "merge", str(number), "--squash", "--delete-branch")
            merged = json.loads(gh("pr", "view", str(number), "--json", "mergeCommit,state"))
            if merged["state"] != "MERGED":
                raise SystemExit(f"PR #{number} is {merged['state']} after the merge")
            print(f"PR #{number} merged as {merged['mergeCommit']['oid']}")
            return merged["mergeCommit"]["oid"]
        if state["mergeStateStatus"] in ("DIRTY", "BEHIND") or state["state"] != "OPEN":
            raise SystemExit(f"PR #{number} is {state['state']} and {state['mergeStateStatus']}, not mergeable")
        time.sleep(PAUSE)
    raise SystemExit(f"PR #{number} did not become CLEAN in time")


def land(branch, message, files):
    base = api(f"repos/{REPO}/git/ref/heads/main")["object"]["sha"]
    api(f"repos/{REPO}/git/refs", "--method", "POST", "-f", f"ref=refs/heads/{branch}", "-f", f"sha={base}")
    signed_commit(branch, base, message, files)
    url = gh(
        "pr",
        "create",
        "--base",
        "main",
        "--head",
        branch,
        "--title",
        message.partition("\n")[0],
        "--body",
        "Opened by the release cycle; merged by it when CLEAN.",
    )
    number = url.rsplit("/", 1)[1]
    approve_and_wait(number)
    return merge_when_clean(number)


def merge(number):
    approve_and_wait(number)
    return merge_when_clean(number)


def resume():
    """Settle the pull requests earlier cycles left open at a bounded wait, freshness bound to their own cycle."""
    pulls = api(f"repos/{REPO}/pulls?state=open&per_page=100") or []
    candidates = []
    for pr in pulls:
        if pr.get("user", {}).get("login") != BOT:
            continue
        head_repo = pr.get("head", {}).get("repo")
        if not head_repo or head_repo.get("full_name") != REPO:
            continue
        head_ref = pr.get("head", {}).get("ref", "")
        match = LEFTOVER.match(head_ref)
        if match:
            candidates.append((pr, match.group(1)))
    if not candidates:
        print("no leftover cycle PR")
        return
    if len(candidates) > 5:
        raise SystemExit(f"more than 5 leftover cycle PRs ({len(candidates)})")
    for pr, cycle_id in candidates:
        run = api(f"repos/{REPO}/actions/runs/{cycle_id}")
        if not run["path"].endswith(".github/workflows/cycle.yml") or run["repository"]["full_name"] != REPO:
            raise SystemExit(f"PR #{pr['number']}: run {cycle_id} is not a cycle.yml run in {REPO}")
        # created_at is the earlier cycle's first start; a re-run moves run_started_at past its commit.
        started = run["created_at"]
        approve_and_wait(pr["number"], started=started)
        merged = merge_when_clean(pr["number"])
        print(f"PR #{pr['number']} resumed from cycle {cycle_id}, merged as {merged}")


def main(arguments):
    if arguments[:1] == ["land"] and len(arguments) >= 4:
        commit = land(arguments[1], arguments[2], arguments[3:])
    elif arguments[:1] == ["merge"] and len(arguments) == 2:
        commit = merge(arguments[1])
    elif arguments == ["resume"]:
        resume()
        return
    else:
        raise SystemExit(__doc__)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"commit={commit}\n")


if __name__ == "__main__":
    main(sys.argv[1:])
