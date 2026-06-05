"""
Cluster-CI Stress Test — Verify CUDA memory protection on unified memory (GB10/Grace).

This script tests 3 things:
1. What torch.cuda.get_device_properties(0).total_mem reports on unified memory
2. Whether torch.cuda.set_per_process_memory_fraction() actually enforces limits
3. Whether /proc/meminfo correctly reflects CUDA allocations

Usage: python3 stress_test_cuda.py
"""

import sys
import os


def test_cuda_properties():
    """Test 1: What does CUDA report as total memory on GB10?"""
    print("=" * 60)
    print("TEST 1: CUDA Device Properties")
    print("=" * 60)

    import torch

    if not torch.cuda.is_available():
        print("CUDA not available!")
        return False

    props = torch.cuda.get_device_properties(0)
    total_bytes = props.total_mem
    total_gb = total_bytes / (1024 ** 3)

    print(f"  Device: {props.name}")
    print(f"  total_mem: {total_bytes} bytes ({total_gb:.1f} GB)")

    # Read system RAM for comparison
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                sys_ram_kb = int(line.split()[1])
                sys_ram_gb = sys_ram_kb / (1024 ** 2)
                print(f"  System RAM: {sys_ram_gb:.1f} GB")
                break

    if total_gb < 1.0:
        print("  RESULT: total_mem is ~0 -> CUDA guard fraction will NOT work!")
        return False
    elif total_gb > 100:
        print(f"  RESULT: total_mem reports system RAM ({total_gb:.0f} GB) -> CUDA guard SHOULD work")
        return True
    else:
        print(f"  RESULT: total_mem reports {total_gb:.0f} GB (dedicated VRAM?)")
        return True


def test_memory_fraction():
    """Test 2: Does set_per_process_memory_fraction actually enforce limits?"""
    print()
    print("=" * 60)
    print("TEST 2: Memory Fraction Enforcement")
    print("=" * 60)

    import torch

    # Set a very small fraction (5% of total) to test enforcement
    props = torch.cuda.get_device_properties(0)
    total_gb = props.total_mem / (1024 ** 3)

    if total_gb < 1.0:
        print("  SKIP: total_mem is ~0, fraction enforcement is meaningless")
        return False

    # Set limit to 2 GB (small enough to test easily)
    target_limit_gb = 2.0
    fraction = target_limit_gb / total_gb
    print(f"  Setting fraction: {fraction:.4f} ({target_limit_gb:.0f} GB / {total_gb:.0f} GB)")

    try:
        torch.cuda.set_per_process_memory_fraction(fraction, device=0)
        print(f"  set_per_process_memory_fraction({fraction:.4f}) -> OK")
    except Exception as e:
        print(f"  set_per_process_memory_fraction FAILED: {e}")
        return False

    # Try to allocate 1 GB (should succeed)
    print("  Allocating 1 GB (should succeed)...")
    try:
        t1 = torch.zeros(256 * 1024 * 1024, dtype=torch.float32, device="cuda")  # 1 GB
        print(f"  1 GB allocated OK. Memory used: {torch.cuda.memory_allocated() / 1024**3:.2f} GB")
        del t1
        torch.cuda.empty_cache()
    except torch.cuda.OutOfMemoryError:
        print("  1 GB allocation FAILED (OOM) — fraction is too restrictive or broken")
        return False

    # Try to allocate 4 GB (should FAIL if fraction is enforced)
    print("  Allocating 4 GB (should FAIL with OOM if fraction works)...")
    try:
        t2 = torch.zeros(1024 * 1024 * 1024, dtype=torch.float32, device="cuda")  # 4 GB
        actual_gb = torch.cuda.memory_allocated() / 1024 ** 3
        print(f"  4 GB allocated OK ({actual_gb:.1f} GB used) — FRACTION NOT ENFORCED!")
        del t2
        torch.cuda.empty_cache()
        return False
    except torch.cuda.OutOfMemoryError:
        print("  4 GB allocation correctly FAILED with OOM -> FRACTION IS ENFORCED!")
        return True
    except RuntimeError as e:
        if "out of memory" in str(e).lower() or "CUDA" in str(e):
            print(f"  4 GB allocation FAILED with: {e}")
            print("  -> FRACTION IS ENFORCED!")
            return True
        else:
            print(f"  Unexpected error: {e}")
            return False


def test_meminfo_reflects_cuda():
    """Test 3: Does /proc/meminfo reflect CUDA allocations?"""
    print()
    print("=" * 60)
    print("TEST 3: /proc/meminfo reflects CUDA allocations")
    print("=" * 60)

    import torch

    def get_available_ram_gb():
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1024 ** 2)
        return 0

    # Reset any previous fraction
    # Note: can't reset fraction, so we just test with whatever is set

    before_gb = get_available_ram_gb()
    print(f"  MemAvailable before: {before_gb:.1f} GB")

    # Allocate 1 GB on CUDA
    try:
        t = torch.zeros(256 * 1024 * 1024, dtype=torch.float32, device="cuda")  # 1 GB
    except Exception as e:
        print(f"  Cannot allocate 1 GB for test: {e}")
        print("  (This might be because test 2 set a 2GB fraction limit)")
        return None

    import time
    time.sleep(1)  # Let meminfo settle

    after_gb = get_available_ram_gb()
    diff_gb = before_gb - after_gb
    print(f"  MemAvailable after 1GB CUDA alloc: {after_gb:.1f} GB")
    print(f"  Difference: {diff_gb:.2f} GB")

    del t
    torch.cuda.empty_cache()

    if diff_gb > 0.5:
        print(f"  RESULT: /proc/meminfo DOES reflect CUDA allocations (delta: {diff_gb:.1f} GB) -> Watchdog WILL work!")
        return True
    else:
        print(f"  RESULT: /proc/meminfo does NOT reflect CUDA allocations (delta: {diff_gb:.1f} GB) -> Watchdog BROKEN!")
        return False


if __name__ == "__main__":
    print("Cluster-CI CUDA Memory Protection Stress Test")
    print("Machine:", os.uname().nodename)
    print()

    r1 = test_cuda_properties()
    r2 = test_memory_fraction()
    r3 = test_meminfo_reflects_cuda()

    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  CUDA total_mem works:          {'YES' if r1 else 'NO'}")
    print(f"  Memory fraction enforced:      {'YES' if r2 else 'NO' if r2 is not None else 'SKIP'}")
    print(f"  /proc/meminfo reflects CUDA:   {'YES' if r3 else 'NO' if r3 is not None else 'SKIP'}")

    if r1 and r2 and r3:
        print("\n  ALL PROTECTIONS VERIFIED ✅")
    elif r3:
        print("\n  GPU Watchdog (/proc/meminfo) WORKS ✅")
        if not r2:
            print("  CUDA Guard (fraction) DOES NOT WORK ❌ — watchdog is the only protection!")
    else:
        print("\n  CRITICAL: NO RELIABLE PROTECTION ❌")

    sys.exit(0 if (r1 and r2 and r3) else 1)
