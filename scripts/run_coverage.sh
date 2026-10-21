#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
checkout_root="$(realpath "${2:-$script_dir/..}")"
suite="${1:?Usage: run_coverage.sh backend|frontend [checkout-root] [report-directory]}"
report_dir="$(realpath -m "${3:-$checkout_root/coverage/$suite}")"
mkdir -p "$report_dir"
mode="${4:-current}"
if [[ "$mode" != current && "$mode" != baseline ]]; then
  echo "Unknown measurement mode: $mode" >&2
  exit 2
fi
backend_args=()
frontend_args=()
if [[ "$mode" == baseline ]]; then
  export BASELINE_STATUS_FILE="$report_dir/test-status.json"
  rm -f "$BASELINE_STATUS_FILE" "$report_dir/coverage.json" "$report_dir/coverage-summary.json"
  export PYTHONPATH="$script_dir${PYTHONPATH:+:$PYTHONPATH}"
  backend_args=(-p baseline_pytest)
  frontend_args=(--reporter=default "--reporter=$script_dir/baseline_vitest.mjs")
fi

# Capture only the runner's status; baseline validation never bypasses coverage checks.
set +e
case "$suite" in
  backend)
    cd "$checkout_root/backend" || exit 2
    # Pin both tools for comparable measurements, including pre-coverage target branches.
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 uv run --locked \
      --with pytest-cov==7.0.0 --with coverage==7.16.2 pytest "${backend_args[@]}" \
      --cov=. --cov-branch --cov-config="$script_dir/coverage.ini" \
      --cov-report=term-missing \
      --cov-report="json:$report_dir/coverage.json" \
      --cov-report="html:$report_dir/html"
    ;;
  frontend)
    cd "$checkout_root/frontend" || exit 2
    npm exec -- vitest run "${frontend_args[@]}" --maxWorkers=2 --coverage --coverage.provider=v8 --coverage.reportOnFailure \
      --coverage.include='src/**/*.{js,jsx}' \
      --coverage.exclude='src/**/*.test.{js,jsx}' \
      --coverage.exclude='src/test-setup.js' \
      --coverage.exclude='src/main.jsx' \
      --coverage.reporter=text --coverage.reporter=json-summary --coverage.reporter=html \
      --coverage.reportsDirectory="$report_dir"
    ;;
  *)
    echo "Unknown suite: $suite" >&2
    exit 2
    ;;
esac
status=$?
set -e
if [[ "$mode" == baseline ]]; then
  report=coverage.json
  if [[ "$suite" == frontend ]]; then report=coverage-summary.json; fi
  python3 "$script_dir/validate_baseline.py" --suite "$suite" \
    --status "$BASELINE_STATUS_FILE" --coverage "$report_dir/$report" --exit-code "$status"
else
  exit "$status"
fi
