#!/usr/bin/env bash
# wait-run.sh RUN: wait for a run to complete; print its conclusion for the caller to judge.
# Exit non-zero after three consecutive failed reads or the bounded polling budget.
set -euo pipefail
run="${1:-}"
[[ $# -eq 1 && "$run" =~ ^[0-9]+$ ]] || { echo "usage: $0 RUN" >&2; exit 2; }
# 180 polls x 10 seconds = 30 minutes, the cycle job's headroom for the other steps.
WAIT_POLLS=180
failures=0
for ((poll=1; poll<=WAIT_POLLS; poll++)); do
  if state="$(gh run view "$run" --json status,conclusion --jq '"\(.status) \(.conclusion)"')"; then
    failures=0
    read -r status conclusion <<< "$state"
    if [[ "$status" == completed ]]; then
      echo "$conclusion"
      exit 0
    fi
  else
    failures=$((failures + 1))
    echo "gh run view $run failed ($failures/3 in a row)" >&2
    if (( failures >= 3 )); then
      echo "gh run view $run failed 3 times in a row" >&2
      exit 1
    fi
  fi
  sleep 10
done
echo "run $run did not complete within $WAIT_POLLS polls" >&2
exit 1
