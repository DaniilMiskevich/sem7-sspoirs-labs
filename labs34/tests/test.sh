#!/usr/bin/env sh
# Quick smoke test: correct results + a visible nonblocking speedup.
set -euo pipefail

cd "$(dirname "$0")/.."

export OPENBLAS_NUM_THREADS=1
export OMP_NUM_THREADS=1

SIZE=${SIZE:-999}
NPROC=${NPROC:-3}
SEED=${SEED:-42}

rm -rf out
RUN_OUTPUT=$(mpirun -n $NPROC python3 main.py --size $SIZE --seed $SEED)
echo "$RUN_OUTPUT"

python3 tests/verify.py $SIZE $NPROC $SEED

# Assert the nonblocking mode is faster than the blocking one (speedup > 1.0).
SPEEDUP=$(echo "$RUN_OUTPUT" | awk '/speedup/{print $3}' | awk -F'x' '{print $1}')
echo "measured speedup: ${SPEEDUP}x"
awk -v s="$SPEEDUP" 'BEGIN{exit !(s > 1.0)}'

# Collective group phase: shared input files, per-group output files.
NGROUPS=${NGROUPS:-2}
rm -rf out shared
RUN_GROUPS=$(mpirun -n $NPROC python3 main.py --size $SIZE --seed $SEED --groups $NGROUPS)
echo "$RUN_GROUPS"
python3 tests/verify_groups.py $SIZE $NPROC $SEED $NGROUPS

echo "everything OK"
