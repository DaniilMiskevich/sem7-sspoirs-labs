#!/usr/bin/env python3
"""MPI distributed matrix multiplication using point-to-point (pairwise) ring communication.

MPI_Comm_World: the default communicator containing every process launched by mpirun.
rank: the unique index [0, size) of a process inside a communicator.
An MPI program must begin with MPI_Init() and finish with MPI_Finalize() (mpi4py wraps
both at import/exit, but the program's correctness hinges on this contract).
Non-blocking transfers (Isend/Irecv) let each matrix-block multiply overlap the
point-to-point transfer of the next block, hiding communication latency underneath
computation — the same idea as CUDA streams overlapping kernels with cudaMemcpyAsync.

Layout: A is split into row blocks (one per rank), B into column strips (one per rank).
Each rank ring-exchanges B strips so it can assemble its row block of C = A @ B.
"""
import argparse
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from mpi4py import MPI

COMM_WORLD = MPI.COMM_WORLD


def parse_args():
    parser = argparse.ArgumentParser(description="MPI pairwise matrix multiplication")
    parser.add_argument("--size", type=int, default=3000, help="N x N matrix dimension")
    parser.add_argument("--seed", type=int, default=1, help="random seed base (reproducibility)")
    parser.add_argument("--output-dir", type=Path, default=Path("out"), help="where rank results are saved")
    return parser.parse_args()


def generate_local_blocks(rank, nprocs, size, seed):
    """Build only this rank's A row-block and B column-strip (no full matrices anywhere)."""
    rows = size // nprocs
    a_block = np.random.default_rng(seed + rank).standard_normal((rows, size))
    b_strip = np.random.default_rng(seed + nprocs + rank).standard_normal((size, rows))
    return a_block, b_strip


def partial_product(a_block, b_strip):
    return a_block @ b_strip


def store_partial_product(c_block, a_block, strip, strip_index, width):
    c_block[:, strip_index * width:(strip_index + 1) * width] = partial_product(a_block, strip)


def blocking_ring_exchange(comm, a_block, b_strip):
    """Serial Send/Recv ring: transfer, then compute — no overlap at all.
    Even ranks send before receive, odd ranks receive before send, to break the
    circular dependency that would deadlock a uniform send-first ring."""
    width = b_strip.shape[1]
    c_block = np.zeros((a_block.shape[0], width * comm.Get_size()))
    buf, nxt = b_strip, np.empty_like(b_strip)
    rank, nprocs = comm.Get_rank(), comm.Get_size()
    left, right = (rank - 1) % nprocs, (rank + 1) % nprocs
    for step in range(nprocs):
        if step < nprocs - 1:
            if rank % 2 == 0:
                comm.Send(buf, dest=right)
                comm.Recv(nxt, source=left)
            else:
                comm.Recv(nxt, source=left)
                comm.Send(buf, dest=right)
        store_partial_product(c_block, a_block, buf, (rank - step) % nprocs, width)
        if step < nprocs - 1:
            buf, nxt = nxt, buf
    return c_block


def nonblocking_ring_exchange(comm, a_block, b_strip):
    """Pipelined Isend/Irecv ring: each dot product overlaps the next transfer."""
    width = b_strip.shape[1]
    c_block = np.zeros((a_block.shape[0], width * comm.Get_size()))
    buf, nxt = b_strip, np.empty_like(b_strip)
    rank, nprocs = comm.Get_rank(), comm.Get_size()
    left, right = (rank - 1) % nprocs, (rank + 1) % nprocs
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        for step in range(nprocs):
            if step < nprocs - 1:
                send_req = comm.Isend(buf, dest=right)
                recv_req = comm.Irecv(nxt, source=left)
            store_partial_product(c_block, a_block, buf, (rank - step) % nprocs, width)
            if step < nprocs - 1:
                recv_req.Wait()
                send_req.Wait()
                buf, nxt = nxt, buf
    finally:
        executor.shutdown()
    return c_block


def time_strategy(strategy, comm, a_block, b_strip):
    start = time.perf_counter()
    c_block = strategy(comm, a_block, b_strip)
    return c_block, time.perf_counter() - start


def save_result(rank, c_block, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    np.save(output_dir / f"rank_{rank}.npy", c_block)


def main():
    args = parse_args()
    comm = COMM_WORLD
    rank, nprocs = comm.Get_rank(), comm.Get_size()
    assert nprocs >= 2, f"need at least 2 MPI ranks, got {nprocs}"
    assert args.size % nprocs == 0, "size must be divisible by the number of ranks"

    a_block, b_strip = generate_local_blocks(rank, nprocs, args.size, args.seed)

    c_block, blocking_time = time_strategy(blocking_ring_exchange, comm, a_block, b_strip)
    _, nonblocking_time = time_strategy(nonblocking_ring_exchange, comm, a_block, b_strip)

    save_result(rank, c_block, args.output_dir)

    if rank == 0:
        print(f"size={args.size} procs={nprocs}")
        print(f"blocking    : {blocking_time:.4f}s")
        print(f"nonblocking : {nonblocking_time:.4f}s")
        print(f"speedup     : {blocking_time / nonblocking_time:.3f}x")


if __name__ == "__main__":
    main()
