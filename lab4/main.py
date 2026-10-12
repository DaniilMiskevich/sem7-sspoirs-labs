#!/usr/bin/env python3
"""MPI distributed matrix multiplication.

Two communication phases:

1. Point-to-point (pairwise) ring: A is split into row blocks (one per rank), B into
   column strips (one per rank). Each rank ring-exchanges B strips to assemble its
   row block of C = A @ B. Blocking (Send/Recv) and non-blocking (Isend/Irecv,
   computation overlapped with the next transfer) variants are timed and compared.

2. Collective group phase (enabled with --groups): all ranks are split into an
   arbitrary number of groups with random sizes. Each group multiplies its own
   sub-matrices: A_g (the A rows owned by the group's ranks) and B_g (the B columns
   owned by the group's ranks). Input data for the phase lives in two shared files
   readable by every node; each rank reads exactly its own portion (by rank) and
   writes its result into the file belonging to its group. Inside a group a
   collective Alltoall gathers the group's B strips, then each rank computes
   A_block @ B_group.

MPI_Comm_World: the default communicator containing every process launched by mpirun.
rank: the unique index [0, size) of a process inside a communicator.
An MPI program must begin with MPI_Init() and finish with MPI_Finalize() (mpi4py wraps
both at import/exit, but the program's correctness hinges on this contract).
"""
import argparse
import time
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from mpi4py import MPI

COMM_WORLD = MPI.COMM_WORLD


def parse_args():
    parser = argparse.ArgumentParser(description="MPI matrix multiplication: pairwise ring + collective groups")
    parser.add_argument("--size", type=int, default=3000, help="N x N matrix dimension")
    parser.add_argument("--seed", type=int, default=1, help="random seed base (reproducibility)")
    parser.add_argument("--output-dir", type=Path, default=Path("out"), help="where results are saved")
    parser.add_argument("--data-dir", type=Path, default=Path("shared"),
                        help="network-shared directory holding the group-phase input files")
    parser.add_argument("--groups", type=int, default=0,
                        help="number of process groups for the collective phase (0 disables it)")
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


def make_group_partition(nprocs, ngroups, seed):
    """Contiguous rank partition of sizes summing to nprocs, drawn identically on every rank."""
    cuts = sorted(np.random.default_rng(seed).choice(np.arange(1, nprocs), ngroups - 1, replace=False))
    bounds = [0, *cuts, nprocs]
    sizes = [b - a for a, b in zip(bounds, bounds[1:])]
    return sizes, bounds


def ensure_input_files(data_dir, size, seed):
    """Rank 0 creates the shared input files once; they stay on the network-readable path."""
    data_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    for name in ("a_data.npy", "b_data.npy"):
        if not (data_dir / name).exists():
            np.save(data_dir / name, rng.standard_normal((size, size)))


def run_group_phase(comm, args, sizes, bounds):
    """Each group multiplies A_g @ B_g via a collective Alltoall of B strips.
    Every rank reads its own file portion by rank and writes its block to its group's folder."""
    rank, nprocs = comm.Get_rank(), comm.Get_size()
    group_id = bisect_right(bounds, rank) - 1
    group_comm = comm.Split(color=group_id, key=rank)
    group_size = group_comm.Get_size()

    ensure_input_files(args.data_dir, args.size, args.seed)
    comm.Barrier()

    rows = args.size // nprocs
    a_block = np.load(args.data_dir / "a_data.npy", mmap_mode="r")[rank * rows:(rank + 1) * rows]
    b_strip = np.ascontiguousarray(np.load(args.data_dir / "b_data.npy", mmap_mode="r")[:, rank * rows:(rank + 1) * rows])

    start = time.perf_counter()
    count = args.size * rows
    b_strips = np.empty((group_size * count,))
    group_comm.Allgather((b_strip, count, MPI.DOUBLE), (b_strips, count, MPI.DOUBLE))
    b_strips = b_strips.reshape(group_size, args.size, rows)
    b_group = b_strips.transpose(1, 0, 2).reshape(args.size, group_size * rows)
    c_block = a_block @ b_group
    local_time = time.perf_counter() - start

    group_time = np.empty(1, dtype=float)
    group_time[0] = local_time
    group_comm.Allreduce(group_time, group_time, op=MPI.MAX)
    group_time = float(group_time[0])

    group_dir = args.output_dir / f"group_{group_id}"
    group_dir.mkdir(parents=True, exist_ok=True)
    np.save(group_dir / f"rank_{rank}.npy", c_block)

    sendbuf = np.array([group_time])
    recvbuf = np.empty(nprocs) if rank == 0 else None
    comm.Gather(sendbuf, root=0, recvbuf=recvbuf)
    return recvbuf


def main():
    args = parse_args()
    comm = COMM_WORLD
    rank, nprocs = comm.Get_rank(), comm.Get_size()
    assert nprocs >= 2, f"need at least 2 MPI ranks, got {nprocs}"
    assert args.size % nprocs == 0, "size must be divisible by the number of ranks"
    if args.groups > 0:
        assert 1 <= args.groups < nprocs, f"groups must be in [1, {nprocs - 1}]"

    # Pairwise (point-to-point) ring phase — original functionality.
    a_block, b_strip = generate_local_blocks(rank, nprocs, args.size, args.seed)
    c_block, blocking_time = time_strategy(blocking_ring_exchange, comm, a_block, b_strip)
    _, nonblocking_time = time_strategy(nonblocking_ring_exchange, comm, a_block, b_strip)
    save_result(rank, c_block, args.output_dir)

    # Collective group phase.
    group_times = None
    if args.groups > 0:
        sizes, bounds = make_group_partition(nprocs, args.groups, args.seed)
        group_times = run_group_phase(comm, args, sizes, bounds)

    if rank == 0:
        print(f"size={args.size} procs={nprocs}")
        print(f"blocking    : {blocking_time:.4f}s")
        print(f"nonblocking : {nonblocking_time:.4f}s")
        print(f"speedup     : {blocking_time / nonblocking_time:.3f}x")
        if group_times is not None:
            wall = max(group_times[bounds[g]] for g in range(args.groups))
            print(f"groups      : {args.groups} (sizes: {' '.join(map(str, sizes))})")
            for g in range(args.groups):
                print(f"group {g}     : {sizes[g]} procs, {group_times[bounds[g]]:.4f}s")
            print(f"group wall  : {wall:.4f}s  (max over groups)")
            print(f"group/pair  : {wall / nonblocking_time:.3f}x  vs nonblocking pairwise")


if __name__ == "__main__":
    main()
