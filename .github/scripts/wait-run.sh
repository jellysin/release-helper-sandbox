#!/usr/bin/env bash
# wait-run.sh RUN: wait for a run to complete; print its conclusion for the caller to judge.
# Bound waits by both poll count and wall-clock seconds, with a deadline on each read.
set -euo pipefail
run="${1:-}"
[[ $# -eq 1 && "$run" =~ ^[0-9]+$ ]] || { echo "usage: $0 RUN" >&2; exit 2; }
# 180 polls x 10 seconds = 30 minutes, the cycle job's headroom for the other steps.
WAIT_POLLS=180
WAIT_SECONDS="${WAIT_SECONDS:-1800}"
WAIT_READ_SECONDS="${WAIT_READ_SECONDS:-30}"
[[ "$WAIT_SECONDS" =~ ^[1-9][0-9]*$ && "$WAIT_READ_SECONDS" =~ ^[1-9][0-9]*$ ]] || {
  echo "usage: $0 RUN (WAIT_SECONDS and WAIT_READ_SECONDS must be positive integers)" >&2
  exit 2
}
deadline=$((SECONDS + WAIT_SECONDS))
failures=0
for ((poll=1; poll<=WAIT_POLLS; poll++)); do
  left=$((deadline - SECONDS))
  (( left > 0 )) || break
  limit=$WAIT_READ_SECONDS
  (( left >= limit )) || limit=$left
  if state="$(timeout "$limit" gh run view "$run" --json status,conclusion --jq '"\(.status) \(.conclusion)"')"; then
    failures=0
    read -r status conclusion <<< "$state"
    if [[ "$status" == completed ]]; then
      echo "$conclusion"
      exit 0
    fi
  else
    code=$?
    failures=$((failures + 1))
    if (( code == 124 )); then
      echo "gh run view $run timed out after ${limit}s ($failures/3 in a row)" >&2
    else
      echo "gh run view $run failed ($failures/3 in a row)" >&2
    fi
    if (( failures >= 3 )); then
      echo "gh run view $run failed 3 times in a row" >&2
      exit 1
    fi
  fi
  sleep 10
done
echo "run $run did not complete within $WAIT_POLLS polls or $WAIT_SECONDS seconds" >&2
exit 1
