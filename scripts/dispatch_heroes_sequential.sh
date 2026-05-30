#!/usr/bin/env bash
# Trigger the "Automated builds refresh" workflow for each hero one at a time,
# waiting for each run to finish before starting the next.
#
# Usage:
#   ./scripts/dispatch_heroes_sequential.sh [HERO...]
#
# Examples:
#   ./scripts/dispatch_heroes_sequential.sh              # all heroes in default order
#   ./scripts/dispatch_heroes_sequential.sh Vanessa Dooley
#
# Optional env vars:
#   REPO            GitHub repo slug (default: hearn1/bazaar-builds)
#   PHASE_OVERRIDE  pipeline phase override passed to the workflow (default: none)
#   CLASSIFIER_MODE classifier_mode input (default: none, workflow uses deterministic)

set -euo pipefail

REPO="${REPO:-hearn1/bazaar-builds}"
WORKFLOW="Automated builds refresh"
DEFAULT_HEROES=(Karnok Mak Dooley Vanessa Pygmalien Jules Stelle)

heroes=("${@:-${DEFAULT_HEROES[@]}}")
total=${#heroes[@]}

for i in "${!heroes[@]}"; do
    hero="${heroes[$i]}"
    echo ""
    echo "=== [$(( i + 1 ))/$total] Triggering $hero ==="

    dispatch_args=(-f "hero=$hero")
    [[ -n "${PHASE_OVERRIDE:-}" ]] && dispatch_args+=(-f "phase_override=$PHASE_OVERRIDE")
    [[ -n "${CLASSIFIER_MODE:-}" ]] && dispatch_args+=(-f "classifier_mode=$CLASSIFIER_MODE")

    gh workflow run "$WORKFLOW" --repo "$REPO" "${dispatch_args[@]}"

    # Give the API a moment to register the run before we query for it.
    sleep 5
    run_id=$(gh run list --repo "$REPO" --workflow "$WORKFLOW" --limit 1 \
        --json databaseId --jq '.[0].databaseId')
    echo "Run ID: $run_id"

    gh run watch "$run_id" --repo "$REPO" --exit-status
    echo "=== $hero done ==="
done

echo ""
echo "All $total hero(es) complete."
