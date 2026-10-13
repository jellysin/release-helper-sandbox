"""Land a change on the protected main through a pull request, as the ruleset requires.

    land.py land BRANCH MESSAGE FILE...   commit FILEs (as in the work tree) on a new BRANCH from main
                                         through the forge API (GitHub signs it), open a PR, dispatch CI
                                         on the branch, squash-merge when CLEAN; print the merge commit
    land.py merge NUMBER                  wait for CI on an open PR's head and squash-merge it when CLEAN

Standard library and gh only, with the job's token. Every wait is bounded; any other state fails.
"""

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = os.environ["GITHUB_REPOSITORY"]
CI_WORKFLOW = "ci.yml"
POLLS, PAUSE = 60, 10  # at most ten minutes for CI to start and finish, and for the merge state to settle


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


def wait_for_ci(branch, sha):
    """Wait for the CI run on branch at sha to finish; fail unless it succeeded."""
    for _ in range(POLLS):
        runs = api(f"repos/{REPO}/actions/workflows/{CI_WORKFLOW}/runs?branch={branch}&head_sha={sha}&per_page=5")
        run = runs["workflow_runs"][0] if runs["workflow_runs"] else None
        if run and run["status"] == "completed":
            if run["conclusion"] != "success":
                raise SystemExit(f"CI on {branch} at {sha} concluded {run['conclusion']}: {run['html_url']}")
            print(f"CI on {branch}: success {run['html_url']}")
            return
        time.sleep(PAUSE)
    raise SystemExit(f"CI on {branch} at {sha} did not finish in time")


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
    head = signed_commit(branch, base, message, files)
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
    gh("workflow", "run", CI_WORKFLOW, "--ref", branch)
    wait_for_ci(branch, head)
    return merge_when_clean(number)


def merge(number):
    pr = json.loads(gh("pr", "view", str(number), "--json", "headRefName,headRefOid,state"))
    if pr["state"] != "OPEN":
        raise SystemExit(f"PR #{number} is {pr['state']}")
    wait_for_ci(pr["headRefName"], pr["headRefOid"])
    return merge_when_clean(number)


def main(arguments):
    if arguments[:1] == ["land"] and len(arguments) >= 4:
        commit = land(arguments[1], arguments[2], arguments[3:])
    elif arguments[:1] == ["merge"] and len(arguments) == 2:
        commit = merge(arguments[1])
    else:
        raise SystemExit(__doc__)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"commit={commit}\n")


if __name__ == "__main__":
    main(sys.argv[1:])
