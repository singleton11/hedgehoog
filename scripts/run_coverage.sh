#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
checkout_root="$(realpath "${2:-$script_dir/..}")"
suite="${1:?Usage: run_coverage.sh backend|frontend [checkout-root] [report-directory]}"
report_dir="$(realpath -m "${3:-$checkout_root/coverage/$suite}")"
mkdir -p "$report_dir"

case "$suite" in
  backend)
    cd "$checkout_root/backend"
    # Pin both tools for comparable measurements, including pre-coverage target branches.
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 uv run --locked \
      --with pytest-cov==7.0.0 --with coverage==7.16.2 pytest \
      --cov=. --cov-branch --cov-config="$script_dir/coverage.ini" \
      --cov-report=term-missing \
      --cov-report="json:$report_dir/coverage.json" \
      --cov-report="html:$report_dir/html"
    ;;
  frontend)
    cd "$checkout_root/frontend"
    npm exec -- vitest run --maxWorkers=2 --coverage --coverage.provider=v8 --coverage.reportOnFailure \
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
