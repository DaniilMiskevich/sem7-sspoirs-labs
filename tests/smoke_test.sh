#!/usr/bin/env bash
# Quick smoke test: correct results + a visible nonblocking speedup.
set -euo pipefail

cd "$(dirname "$0")/.."

export OPENBLAS_NUM_THREADS=1
export OMP_NUM_THREADS=1

SIZE=${SIZE:-3600}
NPROCS=${NPROCS:-3}
SIM=${SIM:-0.18}
OUT=out_smoke

rm -rf "$OUT"
RUN_OUTPUT=$(mpirun -n "$NPROCS" --allow-run-as-root \
    python3 src/matrix_mult.py --size "$SIZE" --seed 1 --simulated-comm-seconds-per-step "$SIM" --output-dir "$OUT")
echo "$RUN_OUTPUT"

python3 tests/verify_result.py "$SIZE" "$NPROCS" 1 "$OUT"

# Assert the nonblocking mode is faster than the blocking one (speedup > 1.0).
SPEEDUP=$(echo "$RUN_OUTPUT" | awk '/speedup/{print $3}' | awk -F'x' '{print $1}')
echo "measured speedup: ${SPEEDUP}x"
awk -v s="$SPEEDUP" 'BEGIN{exit !(s > 1.0)}'

rm -rf "$OUT"
echo "smoke test OK"
