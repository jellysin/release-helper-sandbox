#!/usr/bin/env bash
# watch-run.sh WORKFLOW TITLE: find the run of WORKFLOW this cycle just dispatched (by its run-name)
# and wait for it; exit non-zero when it is not found within two minutes or does not succeed.
set -euo pipefail
workflow="$1"
title="$2"
run=""
for _ in $(seq 1 24); do
  run="$(gh run list --workflow "$workflow" --event workflow_dispatch --limit 20 \
    --json databaseId,displayTitle --jq "[.[] | select(.displayTitle == \"$title\")][0].databaseId // empty")"
  [[ -n "$run" ]] && break
  sleep 5
done
[[ -n "$run" ]] || { echo "no $workflow run named '$title' appeared" >&2; exit 1; }
echo "$workflow run $run: $GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$run"
gh run watch "$run" --exit-status --interval 10 > /dev/null
gh run view "$run" --json conclusion --jq '"concluded: \(.conclusion)"'
