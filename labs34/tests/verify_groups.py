"""Reconstruct A_g @ B_g from the shared input files and check the group rank files match."""
import sys
from pathlib import Path

import numpy as np


def make_group_partition(nprocs, ngroups, seed):
    """Same deterministic partition used by main.py (identical seed on every rank)."""
    cuts = sorted(np.random.default_rng(seed).choice(np.arange(1, nprocs), ngroups - 1, replace=False))
    bounds = [0, *cuts, nprocs]
    sizes = [b - a for a, b in zip(bounds, bounds[1:])]
    return sizes, bounds


def main():
    size, nprocs, seed, ngroups = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
    data_dir = Path(sys.argv[5]) if len(sys.argv) > 5 else Path("shared")

    a = np.load(data_dir / "a_data.npy")
    b = np.load(data_dir / "b_data.npy")
    rows = size // nprocs
    sizes, bounds = make_group_partition(nprocs, ngroups, seed)

    max_err = 0.0
    for g, (start, size_g) in enumerate(zip(bounds, sizes)):
        expected = a[start * rows:(start + size_g) * rows] @ b[:, start * rows:(start + size_g) * rows]
        c = np.concatenate([np.load(f"out/group_{g}/rank_{r}.npy") for r in range(start, start + size_g)])
        max_err = max(max_err, float(np.max(np.abs(c - expected))))

    print(f"max_abs_error={max_err:.3e}")
    sys.exit(0 if max_err < 1e-9 else 1)


if __name__ == "__main__":
    main()
