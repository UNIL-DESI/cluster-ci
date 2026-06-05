"""
GPU Memory Guard — Cluster-CI automatic VRAM limiting.
This script is injected into the container's PYTHONSTARTUP to enforce VRAM limits
at the CUDA driver level BEFORE any user code runs.

It reads the CLUSTER_CI_VRAM_LIMIT_GB environment variable and calls
torch.cuda.set_per_process_memory_fraction() to hard-cap GPU memory allocation.

On unified memory systems (NVIDIA GB10/Grace), total_mem from device properties
may return 0 or the full system RAM. We handle both cases by also reading
the system RAM from /proc/meminfo as a fallback.
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

        # Get total GPU memory — may be 0 on unified memory systems
        total_mem_bytes = torch.cuda.get_device_properties(0).total_memory
        total_mem_gb = total_mem_bytes / (1024 ** 3)

        # On unified memory (GB10/Grace), total_mem might be 0 or very small.
        # Fall back to system RAM from /proc/meminfo since it IS the GPU memory.
        if total_mem_gb < 1.0:
            try:
                with open("/proc/meminfo", "r") as f:
                    for line in f:
                        if line.startswith("MemTotal:"):
                            total_mem_kb = int(line.split()[1])
                            total_mem_gb = total_mem_kb / (1024 ** 2)
                            break
                print(
                    f"[Cluster-CI GPU Guard] Unified memory detected. "
                    f"Using system RAM as reference: {total_mem_gb:.0f}GB",
                    file=sys.stderr,
                )
            except Exception:
                # Cannot determine memory — skip guard to avoid breaking user code
                print(
                    "[Cluster-CI GPU Guard] Warning: cannot determine total memory. Guard disabled.",
                    file=sys.stderr,
                )
                return

        if total_mem_gb < 1.0:
            return

        fraction = min(limit / total_mem_gb, 1.0)

        if fraction < 1.0:
            torch.cuda.set_per_process_memory_fraction(fraction, device=0)
            print(
                f"[Cluster-CI GPU Guard] VRAM hard-limit applied: "
                f"{limit:.0f}GB / {total_mem_gb:.0f}GB ({fraction:.1%})",
                file=sys.stderr,
            )
        else:
            print(
                f"[Cluster-CI GPU Guard] Limit ({limit:.0f}GB) >= total ({total_mem_gb:.0f}GB). No cap applied.",
                file=sys.stderr,
            )
    except Exception as e:
        # Never crash user code due to guard failure
        print(f"[Cluster-CI GPU Guard] Warning: could not apply VRAM limit: {e}", file=sys.stderr)


_apply_vram_guard()
