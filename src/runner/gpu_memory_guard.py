"""
GPU Memory Guard — Cluster-CI automatic VRAM limiting.
This script is injected into the container's PYTHONSTARTUP to enforce VRAM limits
at the CUDA driver level BEFORE any user code runs.

It reads the CLUSTER_CI_VRAM_LIMIT_GB environment variable and calls
torch.cuda.set_per_process_memory_fraction() to hard-cap GPU memory allocation.
On unified memory systems (NVIDIA GB10/Grace), this prevents the driver from
allocating beyond the limit, which would otherwise crash the entire system.
"""

import os
import sys


def _apply_vram_guard():
    vram_limit_gb = os.environ.get("CLUSTER_CI_VRAM_LIMIT_GB")
    if not vram_limit_gb:
        return

    try:
        limit = float(vram_limit_gb)
    except ValueError:
        return

    if limit <= 0:
        return

    try:
        import torch

        if not torch.cuda.is_available():
            return

        total_mem = torch.cuda.get_device_properties(0).total_mem / (1024 ** 3)
        fraction = min(limit / total_mem, 1.0)

        if fraction < 1.0:
            torch.cuda.set_per_process_memory_fraction(fraction, device=0)
            print(
                f"[Cluster-CI GPU Guard] VRAM hard-limit applied: "
                f"{limit:.0f}GB / {total_mem:.0f}GB ({fraction:.1%})",
                file=sys.stderr,
            )
    except Exception as e:
        # Never crash user code due to guard failure
        print(f"[Cluster-CI GPU Guard] Warning: could not apply VRAM limit: {e}", file=sys.stderr)


_apply_vram_guard()
