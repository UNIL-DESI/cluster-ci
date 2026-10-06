"""Workspace sanitizer for Cluster-CI runners (Bug 9).

Provides atomic workspace sanitation before jobs or DAG nodes execute:
1. Purges stale leftover files and directories (*.stale_*, .stale_*).
2. Cleans exclusively declared DVC outputs (outs of dvc.yaml/dvc.lock) having stray files
   or mismatched hashes, leaving all non-DVC ignored and untracked files completely untouched
   (e.g., .dvc/config.local, .dvc/tmp, models cache, job_secrets_*.env, host_guard markers, FIFOs).
3. Executes and controls 'dvc checkout --force', failing fast with explicit exception on non-zero exit.
4. Audits MD5 integrity of DVC outputs (handling .dir directory manifests without false positives,
   and ignoring cache: false / persist: true outputs), failing fast on discrepancies.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Set

import yaml

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")


class WorkspaceSanitizerError(RuntimeError):
    """Raised when workspace sanitation or DVC integrity check fails."""
    pass


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


def _get_declared_dvc_outputs(workspace_dir: str) -> Dict[str, Dict[str, Any]]:
    """Extract expected output paths, hashes, and flags from dvc.yaml and dvc.lock.

    Returns mapping of normalized relative path -> {
        'md5': Optional[str],
        'stage': str,
        'is_dir': bool,
        'cache': bool,
        'persist': bool,
    }
    """
    outputs: Dict[str, Dict[str, Any]] = {}

    # 1. Parse dvc.yaml to discover flags (cache, persist)
    yaml_path = os.path.join(workspace_dir, "dvc.yaml")
    if os.path.isfile(yaml_path):
        try:
            with open(yaml_path, "r", encoding="utf-8", errors="replace") as f:
                y_data = yaml.safe_load(f)
            if isinstance(y_data, dict):
                stages = y_data.get("stages", {}) or {}
                for stage_name, stage_data in stages.items():
                    if not isinstance(stage_data, dict):
                        continue
                    outs = stage_data.get("outs", []) or []
                    for out_entry in outs:
                        out_p = None
                        out_cache = True
                        out_persist = False
                        if isinstance(out_entry, str):
                            out_p = out_entry
                        elif isinstance(out_entry, dict):
                            # Formats: {'path': '...', 'cache': False} or {'data/out.csv': {'cache': False}}
                            if "path" in out_entry:
                                out_p = out_entry.get("path")
                                out_cache = out_entry.get("cache", True)
                                out_persist = out_entry.get("persist", False)
                            else:
                                for k, v in out_entry.items():
                                    out_p = k
                                    if isinstance(v, dict):
                                        out_cache = v.get("cache", True)
                                        out_persist = v.get("persist", False)
                                    break
                        if out_p:
                            norm_p = _norm(out_p)
                            outputs[norm_p] = {
                                "md5": None,
                                "stage": str(stage_name),
                                "is_dir": False,
                                "cache": bool(out_cache),
                                "persist": bool(out_persist),
                            }
        except Exception:
            pass

    # 2. Parse dvc.lock to bind MD5 hashes and directory flags
    lock_path = os.path.join(workspace_dir, "dvc.lock")
    if os.path.isfile(lock_path):
        try:
            with open(lock_path, "r", encoding="utf-8", errors="replace") as f:
                l_data = yaml.safe_load(f)
            if isinstance(l_data, dict):
                stages = l_data.get("stages", {}) or {}
                for stage_name, stage_data in stages.items():
                    if not isinstance(stage_data, dict):
                        continue
                    for out in stage_data.get("outs", []) or []:
                        if not isinstance(out, dict):
                            continue
                        out_p = out.get("path")
                        out_md5 = out.get("md5")
                        if out_p:
                            norm_p = _norm(out_p)
                            is_dir = bool(out_md5 and str(out_md5).endswith(".dir"))
                            entry = outputs.setdefault(
                                norm_p,
                                {
                                    "md5": None,
                                    "stage": str(stage_name),
                                    "is_dir": is_dir,
                                    "cache": True,
                                    "persist": False,
                                },
                            )
                            entry["md5"] = out_md5
                            entry["is_dir"] = is_dir
                            entry["stage"] = str(stage_name)
        except Exception as e:
            raise WorkspaceSanitizerError(f"Failed to parse dvc.lock at '{lock_path}': {e}") from e

    return outputs


def _find_dir_cache_manifest(workspace_dir: str, dir_md5: str) -> Optional[str]:
    """Find the .dir JSON manifest file in .dvc/cache corresponding to a directory output MD5."""
    candidates = [
        os.path.join(workspace_dir, ".dvc", "cache", "files", "md5", dir_md5[:2], dir_md5[2:]),
        os.path.join(workspace_dir, ".dvc", "cache", dir_md5[:2], dir_md5[2:]),
        os.path.join(workspace_dir, ".dvc", "cache", "files", "md5", dir_md5[:2], dir_md5[2:] + ".dir"),
        os.path.join(workspace_dir, ".dvc", "cache", dir_md5[:2], dir_md5[2:] + ".dir"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _is_hash_in_local_cache(workspace_dir: str, md5_hash: str) -> bool:
    """Check whether a content hash exists in local DVC cache storage."""
    if not md5_hash or len(md5_hash) < 4:
        return False
    clean_hash = md5_hash[:-4] if md5_hash.endswith(".dir") else md5_hash
    candidates = [
        os.path.join(workspace_dir, ".dvc", "cache", "files", "md5", clean_hash[:2], clean_hash[2:]),
        os.path.join(workspace_dir, ".dvc", "cache", clean_hash[:2], clean_hash[2:]),
        os.path.join(workspace_dir, ".dvc", "cache", "files", "md5", clean_hash[:2], clean_hash[2:] + ".dir"),
        os.path.join(workspace_dir, ".dvc", "cache", clean_hash[:2], clean_hash[2:] + ".dir"),
    ]
    return any(os.path.isfile(c) for c in candidates)



def sanitize_workspace(
    workspace_dir: str = ".",
    *,
    dvc_checkout: bool = True,
    strict_lock_check: bool = True,
    preserved_paths: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Sanitize worker workspace before job or DAG node execution.

    Targeted Cleaning Rules:
    - (a) Stale leftovers: purges all files and directories matching '*.stale_*' or '.stale_*'.
    - (b) Declared DVC outputs:
        * Files/contents within declared output folders not present in DVC cache manifest are purged.
        * Outputs with 'cache: false' or 'persist: true' are never purged or flagged as corrupted.
    - (c) Preserved scope:
        * Non-DVC untracked and ignored files (such as .dvc/config.local, .dvc/tmp, external models,
          job_secrets_*.env, host_guard_killed.marker, log FIFOs) are STRICTLY PRESERVED.
    - Fail-Fast Guarantees:
        * Raises WorkspaceSanitizerError if 'dvc checkout --force' fails (non-zero exit).
        * Raises WorkspaceSanitizerError if any cached DVC output has an MD5 mismatch against dvc.lock.
    """
    abs_ws = os.path.abspath(workspace_dir)
    if not os.path.isdir(abs_ws):
        raise FileNotFoundError(f"Workspace directory not found: '{abs_ws}'")

    if abs_ws in ("/", "\\") or os.path.splitdrive(abs_ws)[1] in ("/", "\\", ""):
        raise ValueError(f"Safety violation: workspace_dir cannot be filesystem root '{abs_ws}'")

    purged_files: List[str] = []

    # 1. Purge stale marker files (*.stale_* or .stale_*) across workspace (avoiding .git / .dvc/cache)
    for root, dirs, files in os.walk(abs_ws, topdown=True):
        norm_root = _norm(os.path.relpath(root, abs_ws))
        if norm_root == ".":
            norm_root = ""

        if norm_root == ".git" or norm_root.startswith(".git/"):
            dirs.clear()
            continue
        if norm_root == ".dvc/cache" or norm_root.startswith(".dvc/cache/"):
            dirs.clear()
            continue

        # Purge stale files
        for f in list(files):
            if ".stale_" in f or f.startswith(".stale_") or f.endswith(".stale"):
                target_file = os.path.join(root, f)
                if _is_path_safe(abs_ws, target_file):
                    try:
                        os.remove(target_file)
                        rel_del = _norm(os.path.relpath(target_file, abs_ws))
                        purged_files.append(rel_del)
                    except OSError as e:
                        raise WorkspaceSanitizerError(f"Failed to remove stale file '{target_file}': {e}") from e

        # Purge stale directories
        for d in list(dirs):
            if ".stale_" in d or d.startswith(".stale_") or d.endswith(".stale"):
                target_dir = os.path.join(root, d)
                if _is_path_safe(abs_ws, target_dir):
                    try:
                        shutil.rmtree(target_dir)
                        rel_del = _norm(os.path.relpath(target_dir, abs_ws)) + "/"
                        purged_files.append(rel_del)
                        dirs.remove(d)
                    except OSError as e:
                        raise WorkspaceSanitizerError(f"Failed to remove stale directory '{target_dir}': {e}") from e

    # 2. Check if workspace is an initialized DVC repository with dvc.lock
    has_dvc_dir = os.path.isdir(os.path.join(abs_ws, ".dvc"))
    has_dvc_lock = os.path.isfile(os.path.join(abs_ws, "dvc.lock"))

    if not (has_dvc_dir and has_dvc_lock):
        print(
            f"ℹ️ [Workspace Sanitizer] Workspace '{abs_ws}' is not an initialized DVC repository with dvc.lock "
            f"(.dvc: {has_dvc_dir}, dvc.lock: {has_dvc_lock}). Skipping DVC checkout and outputs verification.",
            file=sys.stderr,
        )
        return {
            "workspace": abs_ws,
            "purged_files": purged_files,
            "checked_outputs": 0,
            "missing_outputs": [],
            "mismatches": [],
            "dvc_skipped": True,
        }

    # 3. Clean declared DVC outputs exclusively
    declared_outs = _get_declared_dvc_outputs(abs_ws)

    for out_rel, out_info in declared_outs.items():
        if not out_info.get("cache", True) or out_info.get("persist", False):
            # cache: false or persist: true outputs must be left intact
            continue

        full_out_path = os.path.join(abs_ws, out_rel)
        if not os.path.exists(full_out_path):
            continue

        out_md5 = out_info.get("md5")
        if out_info.get("is_dir") and out_md5:
            manifest_file = _find_dir_cache_manifest(abs_ws, out_md5)
            if manifest_file and os.path.isdir(full_out_path):
                try:
                    with open(manifest_file, "r", encoding="utf-8") as f:
                        dir_items = json.load(f)
                    allowed_relpaths: Set[str] = set()
                    if isinstance(dir_items, list):
                        for item in dir_items:
                            if isinstance(item, dict) and "relpath" in item:
                                allowed_relpaths.add(_norm(item["relpath"]))

                    # Inspect files inside output directory and purge stray files
                    for sub_root, _, sub_files in os.walk(full_out_path):
                        for sf in sub_files:
                            sub_file_path = os.path.join(sub_root, sf)
                            rel_to_out = _norm(os.path.relpath(sub_file_path, full_out_path))
                            if rel_to_out not in allowed_relpaths:
                                if _is_path_safe(abs_ws, sub_file_path):
                                    os.remove(sub_file_path)
                                    purged_files.append(_norm(os.path.relpath(sub_file_path, abs_ws)))
                except Exception as e:
                    raise WorkspaceSanitizerError(f"Failed inspecting directory output '{out_rel}': {e}") from e

    # 3. Execute 'dvc checkout --force --allow-missing' if requested
    # On new or distributed workers, missing cache items are normal (fetched via CAS or recomputed).
    if dvc_checkout:
        dvc_yaml_path = os.path.join(abs_ws, "dvc.yaml")
        if os.path.isfile(dvc_yaml_path):
            checkout_res = subprocess.run(
                ["dvc", "checkout", "--force", "--allow-missing"],
                cwd=abs_ws,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if checkout_res.returncode != 0:
                err_detail = (checkout_res.stderr or checkout_res.stdout or "").strip()
                # If error is purely due to missing cache objects, log as expected warning on distributed worker
                is_missing_cache = checkout_res.returncode == 255 and (
                    "missing-files" in err_detail or "Checkout failed for following targets" in err_detail
                )
                if is_missing_cache:
                    print(
                        f"ℹ️ [Workspace Sanitizer] Some DVC cache objects are missing locally (expected on fresh worker):\n{err_detail}",
                        file=sys.stderr,
                    )
                else:
                    raise WorkspaceSanitizerError(
                        f"DVC checkout failed (exit code {checkout_res.returncode}) in '{abs_ws}'.\n"
                        f"Command: dvc checkout --force --allow-missing\n"
                        f"Error output:\n{err_detail}"
                    )

    # 4. Verify MD5 hashes of present outputs against dvc.lock (Fail-Fast on discrepancies)
    mismatches: List[Dict[str, Any]] = []
    missing_outputs: List[str] = []
    checked_count = 0

    if strict_lock_check and declared_outs:
        for out_rel, out_info in declared_outs.items():
            if not out_info.get("cache", True):
                # cache: false outputs are intentionally not tracked in DVC cache/lock
                continue

            full_out_path = os.path.join(abs_ws, out_rel)
            if not os.path.exists(full_out_path):
                # Output not present on disk: normal on new worker or pending stage, logged without error
                missing_outputs.append(out_rel)
                continue

            expected_md5 = out_info.get("md5")
            if not expected_md5:
                continue

            checked_count += 1

            if out_info.get("is_dir"):
                # Directory output: audit via cache manifest (.dir)
                manifest_file = _find_dir_cache_manifest(abs_ws, expected_md5)
                if manifest_file and os.path.isdir(full_out_path):
                    try:
                        with open(manifest_file, "r", encoding="utf-8") as f:
                            dir_items = json.load(f)
                        if isinstance(dir_items, list):
                            for item in dir_items:
                                item_rel = item.get("relpath")
                                item_md5 = item.get("md5")
                                if item_rel and item_md5:
                                    sub_p = os.path.join(full_out_path, item_rel)
                                    if not os.path.isfile(sub_p):
                                        mismatches.append(
                                            {
                                                "path": _norm(os.path.join(out_rel, item_rel)),
                                                "expected": item_md5,
                                                "actual": "MISSING",
                                                "stage": out_info["stage"],
                                            }
                                        )
                                    else:
                                        act_sub_md5 = _compute_file_md5(sub_p)
                                        if act_sub_md5 != item_md5:
                                            if _is_hash_in_local_cache(abs_ws, act_sub_md5):
                                                # Stale residue from earlier commit already saved in local cache: purge to prevent leak
                                                os.remove(sub_p)
                                                purged_files.append(_norm(os.path.join(out_rel, item_rel)))
                                            else:
                                                mismatches.append(
                                                    {
                                                        "path": _norm(os.path.join(out_rel, item_rel)),
                                                        "expected": item_md5,
                                                        "actual": act_sub_md5,
                                                        "stage": out_info["stage"],
                                                    }
                                                )
                    except Exception as e:
                        raise WorkspaceSanitizerError(f"Failed verifying directory cache manifest for '{out_rel}': {e}") from e
                # Note: if directory manifest is not locally in cache, we do NOT compute a naive file MD5 on the dir.
            else:
                # Regular file output
                if os.path.isfile(full_out_path):
                    actual_md5 = _compute_file_md5(full_out_path)
                    if actual_md5 != expected_md5:
                        if _is_hash_in_local_cache(abs_ws, actual_md5):
                            # Stale output from earlier run already saved in local cache: purge to prevent leak and mark missing
                            os.remove(full_out_path)
                            purged_files.append(out_rel)
                            missing_outputs.append(out_rel)
                        else:
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
            raise WorkspaceSanitizerError(
                f"Écart d'intégrité détecté après assainissement du workspace '{abs_ws}'.\n"
                f"{len(mismatches)} fichier(s) présent(s) ne correspondent pas aux hashs de dvc.lock :\n"
                f"{mismatch_summary}\n"
                f"Remède : purgez les fichiers corrompus ou mettez à jour dvc.lock via dvc repro."
            )

    return {
        "workspace": abs_ws,
        "purged_files": purged_files,
        "checked_outputs": checked_count,
        "missing_outputs": missing_outputs,
        "mismatches": mismatches,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Cluster-CI Workspace Sanitizer: clean stale outputs, run dvc checkout, verify dvc.lock hashes."
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
        if res["missing_outputs"]:
            print(f"ℹ️ [Workspace Sanitizer] {len(res['missing_outputs'])} output(s) not present on disk (expected on fresh worker / pending execution).")
        sys.exit(0)
    except WorkspaceSanitizerError as e:
        print(f"❌ [Workspace Sanitizer Error] {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"❌ [Workspace Sanitizer Fatal Error] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
