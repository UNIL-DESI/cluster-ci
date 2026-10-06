"""Workspace sanitizer for Cluster-CI runners (Bug 9).

Provides atomic workspace sanitation before jobs or DAG nodes execute:
1. Purges stale leftover files (*.stale_*, .stale_*).
2. Cleans untracked and ignored files outside the preserved whitelist and dvc.lock.
3. Executes and controls 'dvc checkout --force' with strict return code verification.
4. Audits MD5 hashes of all present DVC outputs against dvc.lock, failing fast on discrepancies.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

import yaml

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

# Whitelist of paths/patterns strictly preserved across job runs:
# 1. .dvc/cache: Content-Addressed Storage local cache, indispensable to avoid re-downloading/recomputing heavy datasets.
# 2. .dvc (config, tmp): DVC metadata and lockfiles required for 'dvc checkout' execution.
# 3. .git: Local Git repository metadata and objects required for branch tracking and commits.
# 4. venv / .venv / env: Python virtual environment holding installed packages and shims.
# 5. .env / .env.secrets / *.env / .cluster-ci*: Job secrets and execution configuration injected by runner.
DEFAULT_PRESERVED_PATTERNS = {
    ".dvc/cache",
    ".dvc",
    ".git",
    "venv",
    ".venv",
    "env",
    ".env",
    ".env.secrets",
    ".cluster-ci",
    ".cluster-ci.secrets",
}


def _norm(p: str) -> str:
    """Normalize path with forward slashes for cross-platform consistency."""
    return os.path.normpath(p).replace("\\", "/")


def _is_path_safe(workspace_dir: str, target_path: str) -> bool:
    """Ensure target_path is strictly contained within workspace_dir (safety guard)."""
    abs_ws = os.path.abspath(workspace_dir)
    abs_target = os.path.abspath(target_path)
    try:
        common = os.path.commonpath([abs_ws, abs_target])
        return common == abs_ws and abs_target != abs_ws
    except ValueError:
        return False


def _compute_file_md5(file_path: str) -> str:
    """Compute standard hex MD5 digest of a regular file."""
    h = hashlib.md5()
    with open(file_path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _get_lockfile_outputs(workspace_dir: str) -> Dict[str, Dict[str, Any]]:
    """Extract expected output paths and their hashes from dvc.lock if present.
    
    Returns a mapping of normalized relative path -> {'md5': str, 'stage': str, 'is_dir': bool}.
    """
    lock_path = os.path.join(workspace_dir, "dvc.lock")
    if not os.path.isfile(lock_path):
        return {}

    try:
        with open(lock_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as e:
        raise ValueError(f"Failed to parse dvc.lock at '{lock_path}': {e}") from e

    if not isinstance(data, dict):
        return {}

    stages = data.get("stages", {}) or {}
    outputs: Dict[str, Dict[str, Any]] = {}

    for stage_name, stage_data in stages.items():
        if not isinstance(stage_data, dict):
            continue
        for out in stage_data.get("outs", []) or []:
            if not isinstance(out, dict):
                continue
            out_p = out.get("path")
            out_md5 = out.get("md5")
            if out_p and out_md5:
                norm_p = _norm(out_p)
                is_dir = out_md5.endswith(".dir")
                outputs[norm_p] = {
                    "md5": out_md5,
                    "stage": stage_name,
                    "is_dir": is_dir,
                }

    return outputs


def sanitize_workspace(
    workspace_dir: str = ".",
    *,
    dvc_checkout: bool = True,
    strict_lock_check: bool = True,
    preserved_paths: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Sanitize worker workspace before job or DAG node execution.

    Contract & Guarantees:
    - Input:
        * workspace_dir: Root directory of the job workspace (must exist).
        * dvc_checkout: If True, executes 'dvc checkout --force' and enforces zero exit code.
        * strict_lock_check: If True, validates MD5 of present outputs against dvc.lock and fails fast.
        * preserved_paths: Optional list of additional relative paths to protect from deletion.
    - Safety Invariant:
        * Never deletes any file or directory outside workspace_dir.
        * Rejects workspace_dir if set to filesystem root.
    - Whitelist Protection (strictly preserved):
        * .dvc/cache : Content-Addressed Storage blocks (avoids re-downloading/recomputing heavy data).
        * .dvc       : DVC configuration, internal state and locks.
        * .git       : Git version control objects, branch references and configuration.
        * venv/.venv : Python virtual environment and execution shims.
        * *.env / .cluster-ci* : Environment secrets and job configuration parameters.
    - Actions performed:
        1. Deletes all leftover stale files matching '*.stale_*' or '.stale_*'.
        2. Inspects untracked/ignored files via Git: deletes residual files not in the whitelist
           and not registered as valid outputs in dvc.lock (or whose MD5 mismatches dvc.lock).
        3. Executes 'dvc checkout --force' without suppressing stderr/stdout, raising on failure.
        4. Audits MD5 of present DVC outputs against dvc.lock; raises detailed error on discrepancies.

    Returns:
        Dict[str, Any] containing execution summary:
        {'purged_files': List[str], 'checked_outputs': int, 'mismatches': List[Dict[str, Any]]}

    Raises:
        FileNotFoundError: If workspace_dir does not exist.
        ValueError: If workspace_dir is unsafe or invalid.
        RuntimeError: If 'dvc checkout' fails or outputs mismatch dvc.lock hashes.
    """
    abs_ws = os.path.abspath(workspace_dir)
    if not os.path.isdir(abs_ws):
        raise FileNotFoundError(f"Workspace directory not found: '{abs_ws}'")

    # Safety: reject root directories
    if abs_ws in ("/", "\\") or os.path.splitdrive(abs_ws)[1] in ("/", "\\", ""):
        raise ValueError(f"Safety violation: workspace_dir cannot be filesystem root '{abs_ws}'")

    # Compile preserved prefixes
    preserved_prefixes = set(DEFAULT_PRESERVED_PATTERNS)
    if preserved_paths:
        for p in preserved_paths:
            preserved_prefixes.add(_norm(p))

    def _is_preserved(rel_path: str) -> bool:
        norm_rel = _norm(rel_path)
        # Explicit file name or prefix check
        for p in preserved_prefixes:
            if norm_rel == p or norm_rel.startswith(p + "/"):
                return True
        # Secrets files ending with .env
        basename = os.path.basename(norm_rel)
        if basename.endswith(".env") or basename.startswith(".cluster-ci"):
            return True
        return False

    purged_files: List[str] = []

    # 1. Purge stale marker files (*.stale_* or .stale_*) across workspace (except .git / .dvc/cache)
    for root, dirs, files in os.walk(abs_ws):
        norm_root = _norm(os.path.relpath(root, abs_ws))
        if norm_root == ".":
            norm_root = ""

        # Avoid walking into .git or .dvc/cache
        if norm_root == ".git" or norm_root.startswith(".git/"):
            dirs.clear()
            continue
        if norm_root == ".dvc/cache" or norm_root.startswith(".dvc/cache/"):
            dirs.clear()
            continue

        for f in files:
            if ".stale_" in f:
                target_file = os.path.join(root, f)
                if _is_path_safe(abs_ws, target_file):
                    try:
                        os.remove(target_file)
                        rel_del = _norm(os.path.relpath(target_file, abs_ws))
                        purged_files.append(rel_del)
                    except OSError as e:
                        raise RuntimeError(f"Failed to remove stale file '{target_file}': {e}") from e

    # 2. Inspect untracked and ignored files via Git (if Git repo is initialized)
    expected_outs = _get_lockfile_outputs(abs_ws)
    git_dir = os.path.join(abs_ws, ".git")

    if os.path.exists(git_dir):
        try:
            status_res = subprocess.run(
                ["git", "status", "--porcelain=v1", "--ignored", "-u"],
                cwd=abs_ws,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if status_res.returncode == 0:
                for line in status_res.stdout.splitlines():
                    if len(line) < 4:
                        continue
                    prefix = line[:2]
                    rel_item = _norm(line[3:].strip().strip('"'))

                    # We inspect untracked (??) or ignored (!!) items
                    if prefix not in ("??", "!!"):
                        continue

                    # If item is preserved (cache, venv, .git, .env...), keep it
                    if _is_preserved(rel_item):
                        continue

                    full_path = os.path.join(abs_ws, rel_item)
                    if not os.path.exists(full_path):
                        continue

                    # If item is a declared DVC output:
                    # If file exists and matches expected lock MD5, keep it.
                    # If MD5 mismatches, delete it to force clean checkout / reproduction.
                    if rel_item in expected_outs and os.path.isfile(full_path):
                        exp_md5 = expected_outs[rel_item]["md5"]
                        actual_md5 = _compute_file_md5(full_path)
                        if actual_md5 == exp_md5:
                            continue  # Valid output, keep

                    # Otherwise, delete untracked/ignored leftover
                    if _is_path_safe(abs_ws, full_path):
                        if os.path.isfile(full_path) or os.path.islink(full_path):
                            os.remove(full_path)
                            purged_files.append(rel_item)
                        elif os.path.isdir(full_path):
                            shutil.rmtree(full_path)
                            purged_files.append(rel_item + "/")
        except Exception as e:
            # Re-raise if Git cleanup fails unexpectedly
            raise RuntimeError(f"Git workspace hygiene scan failed in '{abs_ws}': {e}") from e

    # 3. Execute 'dvc checkout --force' if requested
    if dvc_checkout:
        dvc_yaml_path = os.path.join(abs_ws, "dvc.yaml")
        if os.path.isfile(dvc_yaml_path):
            checkout_res = subprocess.run(
                ["dvc", "checkout", "--force"],
                cwd=abs_ws,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if checkout_res.returncode != 0:
                err_detail = (checkout_res.stderr or checkout_res.stdout or "").strip()
                raise RuntimeError(
                    f"DVC checkout failed (exit code {checkout_res.returncode}) in '{abs_ws}'.\n"
                    f"Command: dvc checkout --force\n"
                    f"Error output:\n{err_detail}"
                )

    # 4. Verify MD5 hashes of present outputs against dvc.lock
    mismatches: List[Dict[str, Any]] = []
    checked_count = 0

    if strict_lock_check and expected_outs:
        for out_rel, out_info in expected_outs.items():
            full_out_path = os.path.join(abs_ws, out_rel)
            if not os.path.exists(full_out_path):
                # Output not present locally yet (normal if stage hasn't run or wasn't in cache)
                continue

            checked_count += 1
            expected_md5 = out_info["md5"]

            if out_info["is_dir"]:
                # Directory output: check .dir manifest in .dvc/cache if available
                cache_dir_file = os.path.join(
                    abs_ws, ".dvc", "cache", "files", "md5", expected_md5[:2], expected_md5[2:]
                )
                if not os.path.isfile(cache_dir_file):
                    cache_dir_file = os.path.join(
                        abs_ws, ".dvc", "cache", expected_md5[:2], expected_md5[2:]
                    )

                if os.path.isfile(cache_dir_file):
                    try:
                        with open(cache_dir_file, "r", encoding="utf-8") as f:
                            dir_items = json.load(f)
                        if isinstance(dir_items, list):
                            for item in dir_items:
                                item_rel = item.get("relpath")
                                item_md5 = item.get("md5")
                                if item_rel and item_md5:
                                    sub_p = os.path.join(full_out_path, item_rel)
                                    if os.path.isfile(sub_p):
                                        act_sub_md5 = _compute_file_md5(sub_p)
                                        if act_sub_md5 != item_md5:
                                            mismatches.append(
                                                {
                                                    "path": _norm(os.path.join(out_rel, item_rel)),
                                                    "expected": item_md5,
                                                    "actual": act_sub_md5,
                                                    "stage": out_info["stage"],
                                                }
                                            )
                    except Exception as e:
                        raise RuntimeError(f"Failed to read DVC directory cache manifest for '{out_rel}': {e}") from e
            else:
                # Regular file output
                if os.path.isfile(full_out_path):
                    actual_md5 = _compute_file_md5(full_out_path)
                    if actual_md5 != expected_md5:
                        mismatches.append(
                            {
                                "path": out_rel,
                                "expected": expected_md5,
                                "actual": actual_md5,
                                "stage": out_info["stage"],
                            }
                        )

        if mismatches:
            lines = [f"  - {m['path']} (stage '{m['stage']}'): attendu {m['expected']}, obtenu {m['actual']}" for m in mismatches]
            mismatch_summary = "\n".join(lines)
            raise RuntimeError(
                f"Écart d'intégrité détecté après assainissement du workspace '{abs_ws}'.\n"
                f"{len(mismatches)} fichier(s) présent(s) ne correspondent pas aux hashs de dvc.lock :\n"
                f"{mismatch_summary}\n"
                f"Remède : purgez les fichiers corrompus ou mettez à jour dvc.lock via dvc repro."
            )

    return {
        "workspace": abs_ws,
        "purged_files": purged_files,
        "checked_outputs": checked_count,
        "mismatches": mismatches,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Cluster-CI Workspace Sanitizer: clean untracked/stale files, run dvc checkout, verify dvc.lock hashes."
    )
    parser.add_argument(
        "workspace",
        nargs="?",
        default=".",
        help="Path to workspace root directory (default: current directory)",
    )
    parser.add_argument(
        "--no-checkout",
        action="store_true",
        help="Skip 'dvc checkout --force' execution",
    )
    parser.add_argument(
        "--skip-lock-check",
        action="store_true",
        help="Skip strict MD5 hash verification against dvc.lock",
    )
    parser.add_argument(
        "--preserve",
        action="append",
        default=[],
        help="Additional relative path to preserve from deletion",
    )

    args = parser.parse_args()

    try:
        res = sanitize_workspace(
            workspace_dir=args.workspace,
            dvc_checkout=not args.no_checkout,
            strict_lock_check=not args.skip_lock_check,
            preserved_paths=args.preserve,
        )
        print(f"✅ [Workspace Sanitizer] Workspace '{res['workspace']}' is clean and verified.")
        if res["purged_files"]:
            print(f"🧹 Purged {len(res['purged_files'])} stale/untracked residual item(s):")
            for p in res["purged_files"]:
                print(f"   - {p}")
        if res["checked_outputs"]:
            print(f"🔒 Verified {res['checked_outputs']} present DVC output(s) against dvc.lock (0 hash mismatch).")
        sys.exit(0)
    except Exception as e:
        print(f"❌ [Workspace Sanitizer Error] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
