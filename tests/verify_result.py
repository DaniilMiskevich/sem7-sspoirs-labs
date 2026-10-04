#!/usr/bin/env python3
"""Reconstruct full A and B from the same seeds, check rank files match A @ B."""
import sys
from pathlib import Path

import numpy as np


def build_full_matrices(size, nprocs, seed):
    rows = size // nprocs
    a_full = np.zeros((size, size))
    b_full = np.zeros((size, size))
    for rank in range(nprocs):
        a_full[rank * rows:(rank + 1) * rows, :] = np.random.default_rng(seed + rank).standard_normal((rows, size))
        b_full[:, rank * rows:(rank + 1) * rows] = np.random.default_rng(seed + nprocs + rank).standard_normal((size, rows))
    return a_full, b_full


def main():
    size, nprocs, seed, out_dir = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), Path(sys.argv[4])
    a_full, b_full = build_full_matrices(size, nprocs, seed)
    expected = a_full @ b_full
    rows = size // nprocs
    c_full = np.concatenate([np.load(out_dir / f"rank_{r}.npy") for r in range(nprocs)])
    max_err = np.max(np.abs(c_full - expected))
    print(f"max_abs_error={max_err:.3e}")
    sys.exit(0 if max_err < 1e-9 else 1)


if __name__ == "__main__":
    main()
