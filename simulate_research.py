#!/usr/bin/env python3
"""Cluster-CI integrated test pipeline script (simulate_research.py).

Implements both individual DAG stages for Cluster-CI v3 parallel execution
and monolithic sequential execution for classic non-regression test mode:
- prep: Initializes metadata and seed.
- branch_a_step1 / step2: Parameterized branch A stages (foreach 1, 2) producing 50MB cached outputs.
- branch_b_step1 / step2: Branch B stages (Image B, cpus=2, ram_gb=4) producing 50MB cached outputs.
- pack_light_1 / pack_light_2: Lightweight stages (cpus=1, ram_gb=1) for machine packing (A11).
- join: Verifies MD5 checksums of all upstream outputs and exercises GPU/VRAM admission (vram_gb=1).

Test controls (Lot G):
- TOY_DURATION_SEC: Adjustable stage sleep duration (CLI --toy-duration, env var, or params.yaml).
- Simulated failure & retry: Configurable deliberate failure (CLI --fail-stage, env FAIL_STAGE, or params.yaml)
  with reliable persistent attempt counting in artifacts/.attempts/<stage>.attempt.
"""

import argparse
import hashlib
import io
import json
import os
import sys
import time
from typing import Any, Optional

# Ensure clean UTF-8 stdout/stderr on all platforms (including Windows cp1252)
if sys.stdout and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

TARGET_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB
LIGHT_SIZE_BYTES = 10 * 1024 * 1024   # 10 MB
CHUNK_SIZE = 1024 * 1024              # 1 MB


def load_params_config() -> dict:
    """Load optional parameters from params.yaml if present."""
    if os.path.exists("params.yaml"):
        try:
            import yaml
            with open("params.yaml", "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            print(f"[simulate_research] Warning: Failed to read params.yaml: {e}", file=sys.stderr)
    return {}


def get_duration(cli_duration: Optional[float] = None) -> float:
    """Return configured stage duration in seconds. Default: 60s (~60s).

    Priority:
    1. CLI argument (--toy-duration)
    2. Environment variable (TOY_DURATION_SEC)
    3. params.yaml (toy_duration_sec)
    4. Default: 60.0s.
    """
    if cli_duration is not None:
        try:
            val = float(cli_duration)
            if val < 0:
                raise ValueError(f"CLI --toy-duration must be >= 0, got {val}")
            return val
        except ValueError as e:
            raise ValueError(f"Invalid --toy-duration: {e}") from e

    env_val = os.environ.get("TOY_DURATION_SEC")
    if env_val is not None and env_val.strip():
        try:
            val = float(env_val)
            if val < 0:
                raise ValueError(f"TOY_DURATION_SEC must be >= 0, got {val}")
            return val
        except ValueError as e:
            raise ValueError(f"Invalid TOY_DURATION_SEC environment variable: {e}") from e

    params = load_params_config()
    if "toy_duration_sec" in params and params["toy_duration_sec"] is not None:
        try:
            val = float(params["toy_duration_sec"])
            if val < 0:
                raise ValueError(f"params.yaml toy_duration_sec must be >= 0, got {val}")
            return val
        except ValueError as e:
            raise ValueError(f"Invalid toy_duration_sec in params.yaml: {e}") from e

    return 60.0


def check_simulated_failure(
    stage_name: str,
    item: Optional[Any] = None,
    args: Optional[argparse.Namespace] = None,
) -> None:
    """Vérifie si une panne simulée est configurée pour ce stage.

    Permet de tester la résilience et le retry du scheduler (Lot G / Chantier 12).
    Configuration possible via :
    1. Arguments CLI (--fail-stage, --fail-attempts, --fail-exit-code)
    2. Variables d'environnement (FAIL_STAGE, FAIL_ATTEMPTS, FAIL_EXIT_CODE)
    3. Fichier params.yaml (fail_stage, fail_attempts, fail_exit_code)

    Comptage persistant des tentatives :
    Le compteur de tentatives est persisté dans artifacts/.attempts/{stage_slug}.attempt.
    À chaque tentative, le compteur est incrémenté.
    Si current_attempt <= FAIL_ATTEMPTS, le stage logge l'échec et termine avec FAIL_EXIT_CODE.
    Sinon, le stage logge le passage au succès et continue nominalement.
    """
    params = load_params_config()

    # 1. Target stage
    fail_stage = None
    if args and getattr(args, "fail_stage", None):
        fail_stage = args.fail_stage
    elif "FAIL_STAGE" in os.environ and os.environ["FAIL_STAGE"].strip():
        fail_stage = os.environ["FAIL_STAGE"].strip()
    elif "fail_stage" in params and params["fail_stage"]:
        fail_stage = str(params["fail_stage"]).strip()

    if not fail_stage:
        return

    # Normalisation du nom de stage cible et actuel
    full_stage_name = f"{stage_name}@{item}" if item is not None else stage_name
    targets = [t.strip() for t in str(fail_stage).split(",") if t.strip()]

    # Match si la cible est 'all', ou correspond au stage_name ou full_stage_name
    matched = False
    for t in targets:
        if t in ("all", stage_name, full_stage_name):
            matched = True
            break

    if not matched:
        return

    # 2. Max attempts
    fail_attempts = 999999
    if args and getattr(args, "fail_attempts", None) is not None:
        fail_attempts = int(args.fail_attempts)
    elif "FAIL_ATTEMPTS" in os.environ and os.environ["FAIL_ATTEMPTS"].strip():
        try:
            fail_attempts = int(os.environ["FAIL_ATTEMPTS"].strip())
        except ValueError as e:
            raise ValueError(f"Invalid FAIL_ATTEMPTS environment variable: {e}") from e
    elif "fail_attempts" in params and params["fail_attempts"] is not None:
        fail_attempts = int(params["fail_attempts"])

    # 3. Exit code
    fail_exit_code = 1
    if args and getattr(args, "fail_exit_code", None) is not None:
        fail_exit_code = int(args.fail_exit_code)
    elif "FAIL_EXIT_CODE" in os.environ and os.environ["FAIL_EXIT_CODE"].strip():
        try:
            fail_exit_code = int(os.environ["FAIL_EXIT_CODE"].strip())
        except ValueError as e:
            raise ValueError(f"Invalid FAIL_EXIT_CODE environment variable: {e}") from e
    elif "fail_exit_code" in params and params["fail_exit_code"] is not None:
        fail_exit_code = int(params["fail_exit_code"])

    # 4. Lecture de la tentative courante via CLUSTER_CI_NODE_ATTEMPT (injectée par le scheduler)
    raw_attempt = None
    if args and getattr(args, "node_attempt", None) is not None:
        raw_attempt = args.node_attempt
    elif "CLUSTER_CI_NODE_ATTEMPT" in os.environ and os.environ["CLUSTER_CI_NODE_ATTEMPT"].strip():
        raw_attempt = os.environ["CLUSTER_CI_NODE_ATTEMPT"].strip()

    if raw_attempt is None:
        raise RuntimeError(
            f"Stage '{full_stage_name}' ciblé par FAIL_STAGE='{fail_stage}', mais la variable d'environnement "
            f"'CLUSTER_CI_NODE_ATTEMPT' est absente ou non renseignée.\n"
            f"Cause : Le scheduler (Lot E / Chantier 12) doit injecter CLUSTER_CI_NODE_ATTEMPT (>= 1) à chaque tentative d'exécution.\n"
            f"Remède : Pour tester localement, exportez CLUSTER_CI_NODE_ATTEMPT=1 ou passez l'option CLI --node-attempt 1."
        )

    try:
        current_attempt = int(raw_attempt)
        if current_attempt < 1:
            raise ValueError(f"CLUSTER_CI_NODE_ATTEMPT doit être >= 1, reçu {current_attempt}")
    except (ValueError, TypeError) as e:
        raise ValueError(
            f"Valeur invalide pour CLUSTER_CI_NODE_ATTEMPT : {raw_attempt!r} (doit être un entier >= 1) : {e}"
        ) from e

    if current_attempt <= fail_attempts:
        print(
            f"❌ [SIMULATED FAILURE] Stage '{full_stage_name}' failing on attempt "
            f"{current_attempt}/{fail_attempts} (CLUSTER_CI_NODE_ATTEMPT={current_attempt}, target: {fail_stage}, exit_code: {fail_exit_code}).",
            file=sys.stderr,
        )
        sys.exit(fail_exit_code)
    else:
        print(
            f"✅ [SIMULATED FAILURE] Stage '{full_stage_name}' attempt {current_attempt} "
            f"exceeds fail threshold ({fail_attempts}). Proceeding nominally."
        )


def compute_md5(filepath: str) -> str:
    """Compute MD5 hash of a file efficiently."""
    hasher = hashlib.md5()
    with open(filepath, "rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


def write_heavy_file(filepath: str, num_mb: int, seed_byte: int) -> str:
    """Write exactly num_mb MB file with verifiable pattern."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "wb") as f:
        for i in range(num_mb):
            b = bytes([(seed_byte + i) % 256]) * CHUNK_SIZE
            f.write(b)
    return compute_md5(filepath)


def ensure_dirs() -> None:
    os.makedirs("artifacts", exist_ok=True)
    os.makedirs("metrics", exist_ok=True)


def run_prep(args: Optional[argparse.Namespace] = None) -> None:
    print("[prep] Running pipeline initialization...")
    ensure_dirs()
    check_simulated_failure("prep", args=args)
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


def run_branch_a_step1(args: argparse.Namespace) -> None:
    item = str(args.item)
    print(f"[branch_a_step1@{item}] Starting stage (Image A, ram_gb=4)...")
    ensure_dirs()
    check_simulated_failure("branch_a_step1", item=item, args=args)

    duration = get_duration(getattr(args, "toy_duration", None))
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


def run_branch_a_step2(args: argparse.Namespace) -> None:
    item = str(args.item)
    print(f"[branch_a_step2@{item}] Starting stage (Image A, ram_gb=4)...")
    ensure_dirs()
    check_simulated_failure("branch_a_step2", item=item, args=args)

    in_path = f"artifacts/branch_a1_{item}.bin"
    if not os.path.exists(in_path):
        print(f"[ERROR] Input missing: {in_path}", file=sys.stderr)
        sys.exit(1)

    in_md5 = compute_md5(in_path)
    print(f"[branch_a_step2@{item}] Verified input {in_path} (md5: {in_md5})")

    duration = get_duration(getattr(args, "toy_duration", None))
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


def run_branch_b_step1(args: Optional[argparse.Namespace] = None) -> None:
    print("[branch_b_step1] Starting stage (Image B, cpus=2, ram_gb=4, HEC45801)...")
    ensure_dirs()
    check_simulated_failure("branch_b_step1", args=args)

    duration = get_duration(getattr(args, "toy_duration", None) if args else None)
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


def run_branch_b_step2(args: Optional[argparse.Namespace] = None) -> None:
    print("[branch_b_step2] Starting stage (Image B, cpus=2, ram_gb=4, HEC45803)...")
    ensure_dirs()
    check_simulated_failure("branch_b_step2", args=args)

    in_path = "artifacts/branch_b1.bin"
    if not os.path.exists(in_path):
        print(f"[ERROR] Input missing: {in_path}", file=sys.stderr)
        sys.exit(1)

    in_md5 = compute_md5(in_path)
    print(f"[branch_b_step2] Verified input {in_path} (md5: {in_md5})")

    duration = get_duration(getattr(args, "toy_duration", None) if args else None)
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


def run_pack_light_1(args: Optional[argparse.Namespace] = None) -> None:
    print("[pack_light_1] Starting stage (cpus=1, ram_gb=1, packing candidate)...")
    ensure_dirs()
    check_simulated_failure("pack_light_1", args=args)

    duration = get_duration(getattr(args, "toy_duration", None) if args else None)
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


def run_pack_light_2(args: Optional[argparse.Namespace] = None) -> None:
    print("[pack_light_2] Starting stage (cpus=1, ram_gb=1, packing candidate)...")
    ensure_dirs()
    check_simulated_failure("pack_light_2", args=args)

    duration = get_duration(getattr(args, "toy_duration", None) if args else None)
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


def run_join(args: Optional[argparse.Namespace] = None) -> None:
    print("[join] Starting stage (vram_gb=1)...")
    ensure_dirs()
    check_simulated_failure("join", args=args)

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


def run_all_sequentially(toy_duration: Optional[float] = None) -> None:
    """Fallback monolithic sequential execution for classic test mode."""
    print("🚀 [simulate_research] Running monolithic sequential execution (classic mode)...")

    class DummyArgs:
        def __init__(self, item=None, duration=None, attempt=None):
            self.item = item
            self.toy_duration = duration
            self.fail_stage = None
            self.fail_attempts = None
            self.fail_exit_code = None
            self.node_attempt = attempt

    run_prep(DummyArgs(duration=toy_duration))
    run_branch_a_step1(DummyArgs(item=1, duration=toy_duration))
    run_branch_a_step1(DummyArgs(item=2, duration=toy_duration))
    run_branch_a_step2(DummyArgs(item=1, duration=toy_duration))
    run_branch_a_step2(DummyArgs(item=2, duration=toy_duration))
    run_branch_b_step1(DummyArgs(duration=toy_duration))
    run_branch_b_step2(DummyArgs(duration=toy_duration))
    run_pack_light_1(DummyArgs(duration=toy_duration))
    run_pack_light_2(DummyArgs(duration=toy_duration))
    run_join(DummyArgs(duration=toy_duration))
    print("🎉 [simulate_research] Monolithic sequential execution complete.")


def main():
    if len(sys.argv) == 1:
        run_all_sequentially()
        return

    parser = argparse.ArgumentParser(description="Cluster-CI v3 Integrated Pipeline Worker")

    # Common options inherited by all stage subparsers
    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument(
        "--toy-duration",
        type=float,
        default=None,
        help="Stage execution sleep duration in seconds (overrides TOY_DURATION_SEC)",
    )
    parent_parser.add_argument(
        "--fail-stage",
        type=str,
        default=None,
        help="Stage name or pattern to trigger a simulated failure (overrides FAIL_STAGE)",
    )
    parent_parser.add_argument(
        "--fail-attempts",
        type=int,
        default=None,
        help="Number of initial attempts to fail before succeeding (overrides FAIL_ATTEMPTS)",
    )
    parent_parser.add_argument(
        "--fail-exit-code",
        type=int,
        default=None,
        help="Exit code returned upon simulated failure (overrides FAIL_EXIT_CODE)",
    )
    parent_parser.add_argument(
        "--node-attempt",
        type=int,
        default=None,
        help="Current execution attempt number (overrides CLUSTER_CI_NODE_ATTEMPT)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # prep
    p_prep = subparsers.add_parser("prep", parents=[parent_parser])
    p_prep.set_defaults(func=run_prep)

    # branch_a_step1
    p_a1 = subparsers.add_parser("branch_a_step1", parents=[parent_parser])
    p_a1.add_argument("--item", required=True, help="Foreach item identifier")
    p_a1.set_defaults(func=run_branch_a_step1)

    # branch_a_step2
    p_a2 = subparsers.add_parser("branch_a_step2", parents=[parent_parser])
    p_a2.add_argument("--item", required=True, help="Foreach item identifier")
    p_a2.set_defaults(func=run_branch_a_step2)

    # branch_b_step1
    p_b1 = subparsers.add_parser("branch_b_step1", parents=[parent_parser])
    p_b1.set_defaults(func=run_branch_b_step1)

    # branch_b_step2
    p_b2 = subparsers.add_parser("branch_b_step2", parents=[parent_parser])
    p_b2.set_defaults(func=run_branch_b_step2)

    # pack_light_1
    p_l1 = subparsers.add_parser("pack_light_1", parents=[parent_parser])
    p_l1.set_defaults(func=run_pack_light_1)

    # pack_light_2
    p_l2 = subparsers.add_parser("pack_light_2", parents=[parent_parser])
    p_l2.set_defaults(func=run_pack_light_2)

    # join
    p_join = subparsers.add_parser("join", parents=[parent_parser])
    p_join.set_defaults(func=run_join)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
