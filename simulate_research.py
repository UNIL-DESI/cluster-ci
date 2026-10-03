#!/usr/bin/env python3
"""Cluster-CI integrated test pipeline script (simulate_research.py).

Implements both individual DAG stages for Cluster-CI v3 parallel execution
and monolithic sequential execution for classic non-regression test mode:
- prep: Initializes metadata and seed.
- branch_a_step1 / step2: Parameterized branch A stages (foreach 1, 2) producing 50MB cached outputs.
- branch_b_step1 / step2: Branch B stages (Image B, cpus=2, ram_gb=4) producing 50MB cached outputs.
- pack_light_1 / pack_light_2: Lightweight stages (cpus=1, ram_gb=1) for machine packing (A11).
- join: Verifies MD5 checksums of all upstream outputs and exercises GPU/VRAM admission (vram_gb=1).
"""

import argparse
import hashlib
import io
import json
import os
import sys
import time

# Ensure clean UTF-8 stdout/stderr on all platforms (including Windows cp1252)
if sys.stdout and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

TARGET_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB
LIGHT_SIZE_BYTES = 10 * 1024 * 1024   # 10 MB
CHUNK_SIZE = 1024 * 1024              # 1 MB


def get_duration():
    """Return configured stage duration in seconds. Default: 60s (~60s)."""
    val = os.environ.get("TOY_DURATION_SEC")
    if val is not None:
        try:
            return float(val)
        except ValueError:
            pass
    return 60.0


def compute_md5(filepath):
    """Compute MD5 hash of a file efficiently."""
    hasher = hashlib.md5()
    with open(filepath, "rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


def write_heavy_file(filepath, num_mb, seed_byte):
    """Write exactly num_mb MB file with verifiable pattern."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "wb") as f:
        for i in range(num_mb):
            b = bytes([(seed_byte + i) % 256]) * CHUNK_SIZE
            f.write(b)
    return compute_md5(filepath)


def ensure_dirs():
    os.makedirs("artifacts", exist_ok=True)
    os.makedirs("metrics", exist_ok=True)


def run_prep(args=None):
    print("[prep] Running pipeline initialization...")
    ensure_dirs()
    timestamp = time.time()

    with open("artifacts/prep.txt", "w", encoding="utf-8") as f:
        f.write(f"Cluster-CI v3 Integrated Pipeline - Prep\nTimestamp: {timestamp}\n")

    metrics = {
        "stage": "prep",
        "timestamp": timestamp,
        "status": "success",
    }
    with open("metrics/prep.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)
    print("[prep] Initialization complete. artifacts/prep.txt generated.")


def run_branch_a_step1(args):
    item = str(args.item)
    print(f"[branch_a_step1@{item}] Starting stage (Image A, ram_gb=4)...")
    ensure_dirs()
    duration = get_duration()
    print(f"[branch_a_step1@{item}] Sleeping for {duration}s to simulate computational work (~60s)...")
    time.sleep(duration)

    out_path = f"artifacts/branch_a1_{item}.bin"
    seed_byte = 10 + int(item)
    md5_val = write_heavy_file(out_path, 50, seed_byte)
    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(f"[branch_a_step1@{item}] Generated heavy output: {out_path} ({size_mb:.2f} MB, md5: {md5_val})")

    metrics = {
        "stage": f"branch_a_step1@{item}",
        "item": item,
        "size_mb": size_mb,
        "md5": md5_val,
        "duration_s": duration,
        "status": "success",
    }
    with open(f"metrics/branch_a1_{item}.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)
    print(f"[branch_a_step1@{item}] Complete.")


def run_branch_a_step2(args):
    item = str(args.item)
    print(f"[branch_a_step2@{item}] Starting stage (Image A, ram_gb=4)...")
    ensure_dirs()
    in_path = f"artifacts/branch_a1_{item}.bin"
    if not os.path.exists(in_path):
        print(f"[ERROR] Input missing: {in_path}", file=sys.stderr)
        sys.exit(1)

    in_md5 = compute_md5(in_path)
    print(f"[branch_a_step2@{item}] Verified input {in_path} (md5: {in_md5})")

    duration = get_duration()
    print(f"[branch_a_step2@{item}] Sleeping for {duration}s to simulate computational work (~60s)...")
    time.sleep(duration)

    out_path = f"artifacts/branch_a2_{item}.bin"
    seed_byte = 30 + int(item)
    md5_val = write_heavy_file(out_path, 50, seed_byte)
    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(f"[branch_a_step2@{item}] Generated heavy output: {out_path} ({size_mb:.2f} MB, md5: {md5_val})")

    metrics = {
        "stage": f"branch_a_step2@{item}",
        "item": item,
        "size_mb": size_mb,
        "md5": md5_val,
        "duration_s": duration,
        "status": "success",
    }
    with open(f"metrics/branch_a2_{item}.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)
    print(f"[branch_a_step2@{item}] Complete.")


def run_branch_b_step1(args=None):
    print("[branch_b_step1] Starting stage (Image B, cpus=2, ram_gb=4)...")
    ensure_dirs()
    duration = get_duration()
    print(f"[branch_b_step1] Sleeping for {duration}s to simulate computational work (~60s)...")
    time.sleep(duration)

    out_path = "artifacts/branch_b1.bin"
    seed_byte = 50
    md5_val = write_heavy_file(out_path, 50, seed_byte)
    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(f"[branch_b_step1] Generated heavy output: {out_path} ({size_mb:.2f} MB, md5: {md5_val})")

    metrics = {
        "stage": "branch_b_step1",
        "size_mb": size_mb,
        "md5": md5_val,
        "duration_s": duration,
        "status": "success",
    }
    with open("metrics/branch_b1.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)
    print("[branch_b_step1] Complete.")


def run_branch_b_step2(args=None):
    print("[branch_b_step2] Starting stage (Image B, cpus=2, ram_gb=4)...")
    ensure_dirs()
    in_path = "artifacts/branch_b1.bin"
    if not os.path.exists(in_path):
        print(f"[ERROR] Input missing: {in_path}", file=sys.stderr)
        sys.exit(1)

    in_md5 = compute_md5(in_path)
    print(f"[branch_b_step2] Verified input {in_path} (md5: {in_md5})")

    duration = get_duration()
    print(f"[branch_b_step2] Sleeping for {duration}s to simulate computational work (~60s)...")
    time.sleep(duration)

    out_path = "artifacts/branch_b2.bin"
    seed_byte = 70
    md5_val = write_heavy_file(out_path, 50, seed_byte)
    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(f"[branch_b_step2] Generated heavy output: {out_path} ({size_mb:.2f} MB, md5: {md5_val})")

    metrics = {
        "stage": "branch_b_step2",
        "size_mb": size_mb,
        "md5": md5_val,
        "duration_s": duration,
        "status": "success",
    }
    with open("metrics/branch_b2.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)
    print("[branch_b_step2] Complete.")


def run_pack_light_1(args=None):
    print("[pack_light_1] Starting stage (cpus=1, ram_gb=1, packing candidate)...")
    ensure_dirs()
    duration = get_duration()
    print(f"[pack_light_1] Sleeping for {duration}s to simulate computational work (~60s)...")
    time.sleep(duration)

    out_path = "artifacts/pack_light_1.bin"
    seed_byte = 90
    md5_val = write_heavy_file(out_path, 10, seed_byte)
    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(f"[pack_light_1] Generated packed output: {out_path} ({size_mb:.2f} MB, md5: {md5_val})")

    metrics = {
        "stage": "pack_light_1",
        "size_mb": size_mb,
        "md5": md5_val,
        "duration_s": duration,
        "status": "success",
    }
    with open("metrics/pack_light_1.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)
    print("[pack_light_1] Complete.")


def run_pack_light_2(args=None):
    print("[pack_light_2] Starting stage (cpus=1, ram_gb=1, packing candidate)...")
    ensure_dirs()
    duration = get_duration()
    print(f"[pack_light_2] Sleeping for {duration}s to simulate computational work (~60s)...")
    time.sleep(duration)

    out_path = "artifacts/pack_light_2.bin"
    seed_byte = 110
    md5_val = write_heavy_file(out_path, 10, seed_byte)
    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(f"[pack_light_2] Generated packed output: {out_path} ({size_mb:.2f} MB, md5: {md5_val})")

    metrics = {
        "stage": "pack_light_2",
        "size_mb": size_mb,
        "md5": md5_val,
        "duration_s": duration,
        "status": "success",
    }
    with open("metrics/pack_light_2.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)
    print("[pack_light_2] Complete.")


def run_join(args=None):
    print("[join] Starting stage (vram_gb=1)...")
    ensure_dirs()

    required_files = [
        "artifacts/branch_a2_1.bin",
        "artifacts/branch_a2_2.bin",
        "artifacts/branch_b2.bin",
        "artifacts/pack_light_1.bin",
        "artifacts/pack_light_2.bin",
    ]

    hashes = {}
    for fpath in required_files:
        if not os.path.exists(fpath):
            print(f"[ERROR] Missing required input artifact: {fpath}", file=sys.stderr)
            sys.exit(1)
        md5_val = compute_md5(fpath)
        hashes[fpath] = md5_val
        size_mb = os.path.getsize(fpath) / (1024 * 1024)
        print(f"[join] Verified dependency: {fpath} ({size_mb:.2f} MB, md5: {md5_val})")

    # GPU admission check
    gpu_info = {"cuda_available": False, "device_name": "CPU", "allocated_vram_mb": 0}
    try:
        import torch
        if torch.cuda.is_available():
            dev = torch.cuda.current_device()
            dev_name = torch.cuda.get_device_name(dev)
            print(f"[join] GPU detected: {dev_name} (Device {dev})")
            # Allocate 100 MB tensor on GPU
            dummy_tensor = torch.zeros((25, 1024, 1024), dtype=torch.float32, device="cuda")
            allocated = torch.cuda.memory_allocated(dev) / (1024 * 1024)
            print(f"[join] Successfully allocated {allocated:.2f} MB VRAM on {dev_name}")
            gpu_info = {
                "cuda_available": True,
                "device_name": dev_name,
                "allocated_vram_mb": allocated,
            }
            del dummy_tensor
            torch.cuda.empty_cache()
        else:
            print("[join] CUDA is not available. Falling back gracefully to CPU mode.")
    except ImportError:
        print("[join] PyTorch is not installed in current environment. Proceeding in pure CPU mode.")

    summary_path = "artifacts/join_summary.txt"
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("=== Cluster-CI v3 Test Pipeline Join Summary ===\n")
        f.write(f"GPU Info: {gpu_info}\n")
        f.write("Verified Artifacts:\n")
        for k, v in hashes.items():
            f.write(f"  {k} -> md5: {v}\n")

    metrics = {
        "stage": "join",
        "verified_hashes": hashes,
        "gpu_info": gpu_info,
        "status": "success",
    }
    with open("metrics/join.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)
    print("[join] Pipeline junction complete. artifacts/join_summary.txt and metrics/join.json generated.")


def run_all_sequentially():
    """Fallback monolithic sequential execution for classic test mode."""
    print("🚀 [simulate_research] Running monolithic sequential execution (classic mode)...")
    class DummyArgs:
        def __init__(self, item=None):
            self.item = item

    run_prep()
    run_branch_a_step1(DummyArgs(item=1))
    run_branch_a_step1(DummyArgs(item=2))
    run_branch_a_step2(DummyArgs(item=1))
    run_branch_a_step2(DummyArgs(item=2))
    run_branch_b_step1()
    run_branch_b_step2()
    run_pack_light_1()
    run_pack_light_2()
    run_join()
    print("🎉 [simulate_research] Monolithic sequential execution complete.")


def main():
    if len(sys.argv) == 1:
        run_all_sequentially()
        return

    parser = argparse.ArgumentParser(description="Cluster-CI v3 Integrated Pipeline Worker")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # prep
    p_prep = subparsers.add_parser("prep")
    p_prep.set_defaults(func=run_prep)

    # branch_a_step1
    p_a1 = subparsers.add_parser("branch_a_step1")
    p_a1.add_argument("--item", required=True, help="Foreach item identifier")
    p_a1.set_defaults(func=run_branch_a_step1)

    # branch_a_step2
    p_a2 = subparsers.add_parser("branch_a_step2")
    p_a2.add_argument("--item", required=True, help="Foreach item identifier")
    p_a2.set_defaults(func=run_branch_a_step2)

    # branch_b_step1
    p_b1 = subparsers.add_parser("branch_b_step1")
    p_b1.set_defaults(func=run_branch_b_step1)

    # branch_b_step2
    p_b2 = subparsers.add_parser("branch_b_step2")
    p_b2.set_defaults(func=run_branch_b_step2)

    # pack_light_1
    p_l1 = subparsers.add_parser("pack_light_1")
    p_l1.set_defaults(func=run_pack_light_1)

    # pack_light_2
    p_l2 = subparsers.add_parser("pack_light_2")
    p_l2.set_defaults(func=run_pack_light_2)

    # join
    p_join = subparsers.add_parser("join")
    p_join.set_defaults(func=run_join)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
