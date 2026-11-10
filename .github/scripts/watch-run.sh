#!/usr/bin/env bash
# watch-run.sh WORKFLOW TITLE: find the run of WORKFLOW this cycle just dispatched (by its run-name)
# and wait for it; exit non-zero when it is not found within two minutes or does not succeed.
set -euo pipefail
workflow="$1"
title="$2"
run=""
for ((poll=1; poll<=24; poll++)); do
  if ! run="$(gh run list --workflow "$workflow" --event workflow_dispatch --limit 20 \
    --json databaseId,displayTitle --jq "[.[] | select(.displayTitle == \"$title\")][0].databaseId // empty")"; then
    echo "gh run list failed (poll $poll/24)" >&2
    run=""
  fi
  [[ -n "$run" ]] && break
  sleep 5
done
[[ -n "$run" ]] || { echo "no $workflow run named '$title' appeared" >&2; exit 1; }
echo "$workflow run $run: $GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$run"
conclusion="$("$(dirname "$0")/wait-run.sh" "$run")"
echo "concluded: $conclusion"
[[ "$conclusion" == success ]]
