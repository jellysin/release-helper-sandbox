#!/usr/bin/env bash
# watch-run.sh WORKFLOW TITLE: find the run of WORKFLOW this cycle just dispatched (by its run-name)
# and wait for it; exit non-zero when it is not found within two minutes or does not succeed.
set -euo pipefail
workflow="$1"
title="$2"
run="$("$(dirname "$0")/find-run.sh" "$workflow" displayTitle "$title")"
echo "$workflow run $run: $GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$run"
conclusion="$("$(dirname "$0")/wait-run.sh" "$run")"
echo "concluded: $conclusion"
[[ "$conclusion" == success ]]
