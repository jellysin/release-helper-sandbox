#!/usr/bin/env bash
# find-run.sh WORKFLOW FIELD VALUE: find a dispatched run by title or head SHA within 24 polls.
set -euo pipefail
workflow="${1:-}"
field="${2:-}"
value="${3:-}"
valid=false
if [[ $# -eq 3 ]]; then
  case "$field" in
    displayTitle) [[ -n "$value" && "$value" != *\"* && "$value" != *\\* ]] && valid=true ;;
    headSha) [[ "$value" =~ ^[0-9a-f]{40}$ ]] && valid=true ;;
  esac
fi
[[ "$valid" == true ]] || { echo "usage: $0 WORKFLOW {displayTitle|headSha} VALUE" >&2; exit 2; }
run=""
for ((poll=1; poll<=24; poll++)); do
  if ! run="$(gh run list --workflow "$workflow" --event workflow_dispatch --limit 20 \
    --json "databaseId,$field" --jq "[.[] | select(.$field == \"$value\")][0].databaseId // empty")"; then
    echo "gh run list failed (poll $poll/24)" >&2
    run=""
  fi
  if [[ -n "$run" ]]; then
    echo "$run"
    exit 0
  fi
  (( poll == 24 )) || sleep 5
done
echo "no $workflow run with $field '$value' appeared" >&2
exit 1
