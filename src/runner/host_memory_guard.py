"""Host Memory Guard Module (Cluster-CI v3 - Bug 8 Grace-Blackwell GB10 Guard).

Monitors host memory availability (/proc/meminfo) with a short polling interval
(<= 1s) to prevent host-level OOM lockups on unified memory architectures (GB10, DGX Spark).

On unified memory (Grace-Blackwell NVLink-C2C), CUDA memory allocations bypass
Linux cgroups (Docker --memory limit). This module ensures that when MemAvailable
drops below a configurable safety reserve (default: 12.0 GiB), the offending job
container is immediately terminated with a clear marker file and log trace.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("cluster_ci.host_memory_guard")

# Default safety reserve: 12.0 GiB
# On a 128 GiB Grace-Blackwell GB10 unified memory system, Linux OS daemons,
# containerd/dockerd, page cache reclamation, network buffers, and SSH require
# approximately 8-12 GiB of uncompromised headroom to avoid catastrophic kernel
# memory thrashing (kswapd lockups) and host-wide hard freezing.
DEFAULT_HOST_MEMORY_RESERVE_GB: float = 12.0
DEFAULT_POLL_INTERVAL_SEC: float = 1.0
DEFAULT_MARKER_FILE_NAME: str = "host_guard_killed.marker"

ENV_HOST_MEMORY_RESERVE_GB: str = "HOST_MEMORY_RESERVE_GB"
ENV_WATCHDOG_POLL_INTERVAL: str = "WATCHDOG_POLL_INTERVAL"
ENV_HOST_GUARD_MARKER_FILE: str = "HOST_GUARD_MARKER_FILE"


def get_configured_reserve_gb(default: float = DEFAULT_HOST_MEMORY_RESERVE_GB) -> float:
    """Retrieve host memory safety reserve from environment or return default.

    Fails loudly if the environment variable contains an invalid or non-positive value.
    """
    raw_val = os.environ.get(ENV_HOST_MEMORY_RESERVE_GB)
    if raw_val is None or not raw_val.strip():
        return float(default)

    try:
        val = float(raw_val.strip())
    except ValueError as err:
        raise ValueError(
            f"Invalid {ENV_HOST_MEMORY_RESERVE_GB} environment value '{raw_val}': must be a valid float."
        ) from err

    if val <= 0:
        raise ValueError(
            f"Invalid {ENV_HOST_MEMORY_RESERVE_GB} value ({val}): host reserve must be strictly positive."
        )

    return val


def parse_meminfo_content(content: str) -> Dict[str, float]:
    """Parse /proc/meminfo text and extract MemTotal and MemAvailable in GiB.

    Fail-fast: raises RuntimeError if MemTotal or MemAvailable is missing, unparseable, or <= 0.
    """
    total_kb: Optional[int] = None
    available_kb: Optional[int] = None

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(":")
        if len(parts) < 2:
            continue
        key = parts[0].strip()
        val_str = parts[1].strip().split()[0]  # strip trailing 'kB'

        if key == "MemTotal":
            try:
                total_kb = int(val_str)
            except ValueError as err:
                raise RuntimeError(
                    f"Fail-fast: corrupted MemTotal in meminfo ('{line}')"
                ) from err
        elif key == "MemAvailable":
            try:
                available_kb = int(val_str)
            except ValueError as err:
                raise RuntimeError(
                    f"Fail-fast: corrupted MemAvailable in meminfo ('{line}')"
                ) from err

    if total_kb is None:
        raise RuntimeError("Fail-fast: 'MemTotal:' entry missing in /proc/meminfo")
    if available_kb is None:
        raise RuntimeError(
            "Fail-fast: 'MemAvailable:' entry missing in /proc/meminfo. "
            "Host kernel is missing MemAvailable metric (requires Linux >= 3.14)."
        )

    if total_kb <= 0:
        raise RuntimeError(f"Fail-fast: invalid non-positive MemTotal ({total_kb} kB)")
    if available_kb < 0:
        raise RuntimeError(f"Fail-fast: invalid negative MemAvailable ({available_kb} kB)")

    total_gib = total_kb / (1024 * 1024)
    available_gib = available_kb / (1024 * 1024)
    used_gib = max(0.0, total_gib - available_gib)

    return {
        "mem_total_gib": round(total_gib, 3),
        "mem_available_gib": round(available_gib, 3),
        "mem_used_gib": round(used_gib, 3),
    }


def read_host_meminfo(meminfo_path: str = "/proc/meminfo") -> Dict[str, float]:
    """Read and parse host meminfo file with strict fail-fast validation."""
    target_path = Path(meminfo_path)
    if not target_path.exists():
        raise RuntimeError(
            f"Fail-fast: meminfo file '{meminfo_path}' does not exist. "
            "Host memory guard cannot operate safely without /proc/meminfo."
        )

    try:
        content = target_path.read_text(encoding="utf-8")
    except Exception as err:
        raise RuntimeError(
            f"Fail-fast: unable to read meminfo file '{meminfo_path}': {err}"
        ) from err

    return parse_meminfo_content(content)


def check_host_memory_headroom(
    reserve_gb: Optional[float] = None,
    meminfo_path: str = "/proc/meminfo",
) -> Tuple[bool, float, float]:
    """Check whether host MemAvailable meets or exceeds the required safety reserve.

    Returns:
        (is_safe, mem_available_gib, reserve_gib)
    """
    effective_reserve = (
        get_configured_reserve_gb() if reserve_gb is None else float(reserve_gb)
    )
    if effective_reserve <= 0:
        raise ValueError(f"reserve_gb must be > 0, got {effective_reserve}")

    mem_data = read_host_meminfo(meminfo_path)
    available_gib = mem_data["mem_available_gib"]
    is_safe = available_gib >= effective_reserve

    return is_safe, available_gib, effective_reserve


def format_kill_message(mem_available_gib: float, reserve_gib: float) -> str:
    """Format canonical kill message expected by runner and logs."""
    return f"killed by host memory guard: MemAvailable={mem_available_gib:.2f} GiB < reserve {reserve_gib:.2f} GiB"


def write_host_guard_marker(
    marker_path: str | Path,
    container_name: str,
    mem_available_gib: float,
    reserve_gib: float,
    reason: Optional[str] = None,
    extra_info: Optional[Dict[str, Any]] = None,
) -> Path:
    """Write an explicit JSON marker file recording container termination."""
    target = Path(marker_path)
    msg = reason or format_kill_message(mem_available_gib, reserve_gib)

    payload: Dict[str, Any] = {
        "status": "killed",
        "reason": msg,
        "container": container_name,
        "mem_available_gib": round(mem_available_gib, 3),
        "reserve_gib": round(reserve_gib, 3),
        "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "exit_code": 137,
    }
    if extra_info:
        payload.update(extra_info)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return target


def kill_container_process(
    container_name: str,
    method: str = "kill",
) -> Tuple[bool, str]:
    """Stop or kill the offending container via docker CLI."""
    if not container_name:
        raise ValueError("container_name cannot be empty")

    cmd = ["docker", method, container_name]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if proc.returncode == 0:
            return True, f"Container '{container_name}' successfully stopped ({method})."
        return False, f"docker {method} failed (code {proc.returncode}): {proc.stderr.strip()}"
    except Exception as err:
        return False, f"docker {method} raised exception: {err}"


def run_host_memory_watchdog(
    container_name: str,
    *,
    reserve_gb: Optional[float] = None,
    poll_interval_sec: Optional[float] = None,
    marker_file: Optional[str | Path] = None,
    meminfo_path: str = "/proc/meminfo",
    max_ticks: Optional[int] = None,
    on_kill: Optional[Callable[[str, float, float], None]] = None,
    stop_runner: bool = True,
) -> int:
    """Run host memory watchdog loop.

    Polls /proc/meminfo at interval (default <= 1.0s). If MemAvailable drops below
    reserve_gb:
      1. Logs fatal violation.
      2. Writes marker file.
      3. Kills container.
      4. Returns exit code 137.

    If container naturally exits or max_ticks is reached while memory is safe,
    returns 0.
    """
    effective_reserve = (
        get_configured_reserve_gb() if reserve_gb is None else float(reserve_gb)
    )

    interval = poll_interval_sec
    if interval is None:
        raw_interval = os.environ.get(ENV_WATCHDOG_POLL_INTERVAL)
        interval = (
            float(raw_interval)
            if raw_interval and raw_interval.strip()
            else DEFAULT_POLL_INTERVAL_SEC
        )

    marker_path = Path(
        marker_file
        or os.environ.get(ENV_HOST_GUARD_MARKER_FILE, DEFAULT_MARKER_FILE_NAME)
    )

    logger.info(
        "Host memory watchdog active on '%s': reserve=%.2f GiB, poll=%.2fs",
        container_name,
        effective_reserve,
        interval,
    )

    tick = 0
    while True:
        tick += 1
        is_safe, mem_avail, reserve = check_host_memory_headroom(
            effective_reserve, meminfo_path
        )

        if not is_safe:
            kill_msg = format_kill_message(mem_avail, reserve)
            logger.error("❌ %s", kill_msg)
            sys.stderr.write(f"[Host Memory Guard] ❌ {kill_msg}\n")
            sys.stderr.flush()

            # 1. Write marker file
            write_host_guard_marker(marker_path, container_name, mem_avail, reserve, kill_msg)

            # 2. Invoke optional callback (e.g. for mock testing)
            if on_kill:
                on_kill(container_name, mem_avail, reserve)

            # 3. Stop container if requested
            if stop_runner:
                kill_container_process(container_name, method="kill")

            return 137

        if max_ticks is not None and tick >= max_ticks:
            return 0

        # Check if container is still running
        if stop_runner:
            try:
                proc = subprocess.run(
                    ["docker", "inspect", container_name, "--format", "{{.State.Running}}"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if proc.returncode == 0 and proc.stdout.strip().lower() != "true":
                    logger.info("Container '%s' has finished running.", container_name)
                    return 0
            except Exception:
                pass

        time.sleep(interval)
