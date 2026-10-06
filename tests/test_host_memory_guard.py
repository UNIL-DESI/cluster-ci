"""Targeted unit tests for Host Memory Guard (Bug 8 Grace-Blackwell GB10 Guard).

Tests simulated meminfo, trigger / non-trigger conditions, unreadable/missing files,
fail-fast validation, environment overrides, and marker file generation.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from src.runner.host_memory_guard import (
    DEFAULT_HOST_MEMORY_RESERVE_GB,
    ENV_HOST_MEMORY_RESERVE_GB,
    check_host_memory_headroom,
    format_kill_message,
    get_configured_reserve_gb,
    parse_meminfo_content,
    read_host_meminfo,
    run_host_memory_watchdog,
    write_host_guard_marker,
)


SAMPLE_MEMINFO_GB10_HEALTHY = """\
MemTotal:       128790528 kB
MemFree:         45120384 kB
MemAvailable:    52428800 kB
Buffers:          1234560 kB
Cached:          10240000 kB
SwapTotal:              0 kB
SwapFree:               0 kB
"""

SAMPLE_MEMINFO_GB10_LOW_MEM = """\
MemTotal:       128790528 kB
MemFree:          1048576 kB
MemAvailable:    10485760 kB
Buffers:           102400 kB
Cached:            512000 kB
SwapTotal:              0 kB
SwapFree:               0 kB
"""

SAMPLE_MEMINFO_MISSING_AVAILABLE = """\
MemTotal:       128790528 kB
MemFree:         45120384 kB
Buffers:          1234560 kB
Cached:          10240000 kB
"""

SAMPLE_MEMINFO_MISSING_TOTAL = """\
MemFree:         45120384 kB
MemAvailable:    52428800 kB
"""

SAMPLE_MEMINFO_CORRUPTED = """\
MemTotal:       INVALID_NUMBER kB
MemAvailable:    52428800 kB
"""


def test_parse_meminfo_nominal():
    parsed = parse_meminfo_content(SAMPLE_MEMINFO_GB10_HEALTHY)
    # 128790528 kB / (1024 * 1024) = 122.825 GiB
    assert parsed["mem_total_gib"] == pytest.approx(122.825, rel=1e-3)
    # 52428800 kB / (1024 * 1024) = 50.0 GiB
    assert parsed["mem_available_gib"] == pytest.approx(50.0, rel=1e-3)
    assert parsed["mem_used_gib"] == pytest.approx(72.825, rel=1e-3)


def test_parse_meminfo_fail_fast_missing_available():
    with pytest.raises(RuntimeError, match="MemAvailable.*missing in /proc/meminfo"):
        parse_meminfo_content(SAMPLE_MEMINFO_MISSING_AVAILABLE)


def test_parse_meminfo_fail_fast_missing_total():
    with pytest.raises(RuntimeError, match="MemTotal.*missing in /proc/meminfo"):
        parse_meminfo_content(SAMPLE_MEMINFO_MISSING_TOTAL)


def test_parse_meminfo_fail_fast_corrupted():
    with pytest.raises(RuntimeError, match="corrupted MemTotal"):
        parse_meminfo_content(SAMPLE_MEMINFO_CORRUPTED)


def test_read_meminfo_file_not_found(tmp_path: Path):
    non_existent = tmp_path / "non_existent_meminfo"
    with pytest.raises(RuntimeError, match="does not exist"):
        read_host_meminfo(str(non_existent))


def test_get_configured_reserve_gb_default(monkeypatch):
    monkeypatch.delenv(ENV_HOST_MEMORY_RESERVE_GB, raising=False)
    assert get_configured_reserve_gb() == DEFAULT_HOST_MEMORY_RESERVE_GB
    assert get_configured_reserve_gb() == 12.0


def test_get_configured_reserve_gb_custom(monkeypatch):
    monkeypatch.setenv(ENV_HOST_MEMORY_RESERVE_GB, "24.5")
    assert get_configured_reserve_gb() == 24.5

    # Invalid non-numeric value
    monkeypatch.setenv(ENV_HOST_MEMORY_RESERVE_GB, "invalid")
    with pytest.raises(ValueError, match="must be a valid float"):
        get_configured_reserve_gb()

    # Invalid non-positive value
    monkeypatch.setenv(ENV_HOST_MEMORY_RESERVE_GB, "-5.0")
    with pytest.raises(ValueError, match="must be strictly positive"):
        get_configured_reserve_gb()


def test_check_memory_headroom_safe(tmp_path: Path):
    meminfo_file = tmp_path / "meminfo"
    meminfo_file.write_text(SAMPLE_MEMINFO_GB10_HEALTHY, encoding="utf-8")

    # Available = 50.0 GiB, Reserve = 12.0 GiB -> Safe
    is_safe, avail_gb, reserve_gb = check_host_memory_headroom(
        reserve_gb=12.0, meminfo_path=str(meminfo_file)
    )
    assert is_safe is True
    assert avail_gb == pytest.approx(50.0, rel=1e-3)
    assert reserve_gb == 12.0


def test_check_memory_headroom_breach(tmp_path: Path):
    meminfo_file = tmp_path / "meminfo"
    # In low mem: Available = 10.0 GiB
    meminfo_file.write_text(SAMPLE_MEMINFO_GB10_LOW_MEM, encoding="utf-8")

    # Available = 10.0 GiB < Reserve = 12.0 GiB -> Breach!
    is_safe, avail_gb, reserve_gb = check_host_memory_headroom(
        reserve_gb=12.0, meminfo_path=str(meminfo_file)
    )
    assert is_safe is False
    assert avail_gb == pytest.approx(10.0, rel=1e-3)
    assert reserve_gb == 12.0


def test_format_kill_message():
    msg = format_kill_message(10.456, 12.0)
    assert msg == "killed by host memory guard: MemAvailable=10.46 GiB < reserve 12.00 GiB"


def test_write_host_guard_marker(tmp_path: Path):
    marker_file = tmp_path / "marker.json"
    written = write_host_guard_marker(
        marker_file,
        container_name="job-container-test",
        mem_available_gib=10.25,
        reserve_gib=12.0,
        reason="killed by host memory guard: MemAvailable=10.25 GiB < reserve 12.00 GiB",
    )
    assert written == marker_file
    assert marker_file.exists()

    data = json.loads(marker_file.read_text(encoding="utf-8"))
    assert data["status"] == "killed"
    assert data["container"] == "job-container-test"
    assert data["mem_available_gib"] == 10.25
    assert data["reserve_gib"] == 12.0
    assert data["exit_code"] == 137
    assert "MemAvailable=10.25 GiB < reserve 12.00 GiB" in data["reason"]


def test_watchdog_safe_completion(tmp_path: Path):
    meminfo_file = tmp_path / "meminfo"
    meminfo_file.write_text(SAMPLE_MEMINFO_GB10_HEALTHY, encoding="utf-8")
    marker_file = tmp_path / "test.marker"

    # Container stays within limits, max_ticks reached -> returns 0, no marker written
    exit_code = run_host_memory_watchdog(
        "mock-container",
        reserve_gb=12.0,
        poll_interval_sec=0.01,
        marker_file=str(marker_file),
        meminfo_path=str(meminfo_file),
        max_ticks=3,
        stop_runner=False,
    )
    assert exit_code == 0
    assert not marker_file.exists()


def test_watchdog_breach_triggers_kill(tmp_path: Path, capsys):
    meminfo_file = tmp_path / "meminfo"
    meminfo_file.write_text(SAMPLE_MEMINFO_GB10_LOW_MEM, encoding="utf-8")
    marker_file = tmp_path / "breach.marker"

    killed_info = {}

    def mock_on_kill(container, avail, reserve):
        killed_info["container"] = container
        killed_info["avail"] = avail
        killed_info["reserve"] = reserve

    exit_code = run_host_memory_watchdog(
        "faulty-gb10-job",
        reserve_gb=12.0,
        poll_interval_sec=0.01,
        marker_file=str(marker_file),
        meminfo_path=str(meminfo_file),
        max_ticks=5,
        on_kill=mock_on_kill,
        stop_runner=False,
    )

    assert exit_code == 137
    assert killed_info["container"] == "faulty-gb10-job"
    assert killed_info["avail"] == pytest.approx(10.0, rel=1e-3)
    assert killed_info["reserve"] == 12.0

    # Marker file must exist and contain exact trace
    assert marker_file.exists()
    data = json.loads(marker_file.read_text(encoding="utf-8"))
    assert data["status"] == "killed"
    assert data["exit_code"] == 137

    captured = capsys.readouterr()
    assert "killed by host memory guard: MemAvailable=10.00 GiB < reserve 12.00 GiB" in captured.err


def test_watchdog_fail_fast_on_missing_meminfo(tmp_path: Path):
    non_existent = tmp_path / "missing_meminfo"
    with pytest.raises(RuntimeError, match="does not exist"):
        run_host_memory_watchdog(
            "test-container",
            reserve_gb=12.0,
            poll_interval_sec=0.01,
            meminfo_path=str(non_existent),
            stop_runner=False,
        )
