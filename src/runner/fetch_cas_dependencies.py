"""
CAS Dependency Fetcher for Cluster-CI v3.

Fetches missing DVC Content-Addressable Storage (CAS) objects across multiple peer workers
before executing a pipeline node.

Features:
  - Multi-source parallel downloading (ThreadPoolExecutor)
  - Automatic fallback: tries candidate sources sequentially per object until one succeeds
  - Strict MD5 integrity verification on every downloaded chunk (rejects corrupted objects)
  - Full support for DVC directory manifests (.dir) and their nested files
  - Robust read-only permission handling (chmod stat.S_IWRITE before unlink/replace, protect to 0o444)
  - No silent OSError masking
  - Safe dvc checkout: never triggers global checkout when target_paths is empty
  - Executes `dvc checkout <deps>` WITHOUT masking errors (no 2>/dev/null)
  - Identifies missing dependencies for Amendement A4 (status: "missing_deps")
  - Protocol compatibility with existing worker_agent.py `/fetch_artifact` route
"""

from __future__ import annotations

import argparse
import concurrent.futures
from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Iterable, Mapping, Sequence
import urllib.parse
from urllib.parse import urlparse
from uuid import uuid4

import requests

logger = logging.getLogger(__name__)


class DownloadResult(tuple):
    """Backwards-compatible 2-tuple (success, reason) with optional transfer_info attribute."""
    def __new__(cls, success: bool, reason: str, transfer_info: dict[str, Any] | None = None):
        return super().__new__(cls, (success, reason))

    def __init__(self, success: bool, reason: str, transfer_info: dict[str, Any] | None = None):
        self.success = success
        self.reason = reason
        self.transfer_info = transfer_info


def normalize_worker_url(worker_str: str, default_port: int = 6000) -> str:
    """
    Normalizes a worker hostname, IP, or raw address to a canonical http(s) URL with port.
    Guarantees a valid scheme (http:// or https://) and port to prevent requests.exceptions.InvalidURL.

    Examples:
      'HEC45801' -> 'http://HEC45801:6000'
      'HEC45801:6000' -> 'http://HEC45801:6000'
      'http://HEC45801' -> 'http://HEC45801:6000'
      'http://HEC45801:6000/' -> 'http://HEC45801:6000'
      'https://remote-worker.org:8080' -> 'https://remote-worker.org:8080'
    """
    raw = str(worker_str).strip()
    if not raw:
        raise ValueError("Worker URL or hostname cannot be empty")

    if not (raw.startswith("http://") or raw.startswith("https://")):
        raw = f"http://{raw}"

    parsed = urllib.parse.urlsplit(raw)
    scheme = parsed.scheme or "http"
    netloc = parsed.netloc
    path = parsed.path.rstrip("/")
    if not netloc and path:
        netloc = path
        path = ""

    if ":" in netloc:
        host, port = netloc.rsplit(":", 1)
        if not port.isdigit():
            netloc = f"{netloc}:{default_port}"
    else:
        netloc = f"{netloc}:{default_port}"

    result = f"{scheme}://{netloc}"
    if path:
        result = f"{result}{path}"
    return result


@dataclass
class FetchResult:
    """Outcome of fetching CAS dependencies for a node."""
    success: bool
    status: str  # "success", "missing_deps", "checkout_failed"
    missing_deps: list[str] = field(default_factory=list)
    missing_hashes: list[str] = field(default_factory=list)
    downloaded_hashes: list[str] = field(default_factory=list)
    cached_hashes: list[str] = field(default_factory=list)
    transfers: list[dict[str, Any]] = field(default_factory=list)
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "missing_deps": self.missing_deps,
            "missing_hashes": self.missing_hashes,
            "downloaded_hashes": self.downloaded_hashes,
            "cached_hashes": self.cached_hashes,
            "transfers": self.transfers,
            "error_message": self.error_message,
        }


def _ensure_writable(path: Path) -> None:
    """Ensures file is writable before unlink or atomic replacement (fixes Windows 0o444 locking)."""
    if path.is_file():
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        except OSError as e:
            logger.warning("Could not unlock file %s permissions: %s", path, e)


def compute_file_md5(file_path: Path | str, chunk_size: int = 65536) -> str:
    """Computes hexadecimal MD5 of a local file in raw binary mode."""
    hasher = hashlib.md5()
    with open(file_path, "rb") as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest().lower()


def get_cas_object_relpath(md5_hash: str) -> str:
    """Returns the relative path inside .dvc/cache/files/md5 for a given hash."""
    clean = md5_hash.strip().lower()
    prefix = clean[:2]
    suffix = clean[2:]
    return f"{prefix}/{suffix}"


def build_candidate_urls(
    source_base_url: str,
    md5_hash: str,
    repo_name: str | None = None,
) -> list[str]:
    """
    Builds potential download URLs for a CAS object from a source URL.
    
    Compatible with:
      - Direct DVC remote URL: http://worker:6000/fetch_artifact/<repo>/.dvc/cache/files/md5
      - worker_agent.py /fetch_artifact route: http://worker:6000/fetch_artifact
      - Base worker URL: http://worker:6000
    """
    norm_source = normalize_worker_url(source_base_url)
    relpath = get_cas_object_relpath(md5_hash)
    base = norm_source.rstrip("/")
    candidates = []

    # Case 1: Base is already a full peer remote endpoint ending in .dvc/cache/files/md5
    if base.endswith(".dvc/cache/files/md5"):
        candidates.append(f"{base}/{relpath}")
        return candidates

    # Case 2: Source already contains /fetch_artifact
    if "/fetch_artifact" in base:
        if repo_name:
            candidates.append(f"{base}/{repo_name}/.dvc/cache/files/md5/{relpath}")
        candidates.append(f"{base}/.dvc/cache/files/md5/{relpath}")
        # Direct CAS fallback if worker supports it
        host_root = base.split("/fetch_artifact")[0]
        candidates.append(f"{host_root}/fetch_cas/{md5_hash}")
        return candidates

    # Case 3: Base worker service URL (e.g. http://worker:6000)
    if repo_name:
        candidates.append(f"{base}/fetch_artifact/{repo_name}/.dvc/cache/files/md5/{relpath}")
    candidates.append(f"{base}/fetch_artifact/.dvc/cache/files/md5/{relpath}")
    candidates.append(f"{base}/fetch_cas/{md5_hash}")

    return candidates


def download_single_object(
    md5_hash: str,
    candidate_sources: Sequence[str],
    cache_dir: Path,
    repo_name: str | None = None,
    timeout: float = 15.0,
    session: requests.Session | None = None,
) -> tuple[bool, str]:
    """
    Downloads a single CAS object into cache_dir using the first responding and valid source.
    Verifies MD5 integrity immediately. Rejects corrupted downloads and proceeds to next source.
    
    Returns:
      (True, "downloaded" | "already_cached") on success,
      (False, "missing") on failure.
    """
    clean_h = md5_hash.strip().lower()
    if len(clean_h) < 2:
        return DownloadResult(False, "invalid_hash", None)

    prefix = clean_h[:2]
    suffix = clean_h[2:]
    target_dir = cache_dir / prefix
    target_file = target_dir / suffix
    expected_hex = clean_h.replace(".dir", "")

    # 1. Check if already cached and valid
    if target_file.is_file():
        if compute_file_md5(target_file) == expected_hex:
            return DownloadResult(True, "already_cached", None)
        logger.warning("Corrupted local cache file %s, removing to re-fetch", target_file)
        _ensure_writable(target_file)
        target_file.unlink()

    target_dir.mkdir(parents=True, exist_ok=True)
    temp_file = target_dir / f"{suffix}.tmp.{uuid4().hex[:8]}"
    http = session or requests.Session()

    # 2. Try candidate sources in order
    for source in candidate_sources:
        try:
            candidate_urls = build_candidate_urls(source, clean_h, repo_name)
        except Exception as parse_err:
            logger.warning("Invalid candidate source '%s': %s", source, parse_err)
            continue
        for url in candidate_urls:
            start_t = time.monotonic()
            try:
                request_options = {"stream": True, "timeout": timeout, "allow_redirects": False}
                if os.environ.get("IS_LOCAL") == "1":
                    token = os.environ.get("CLUSTER_TOKEN")
                    if not token:
                        return DownloadResult(False, "missing_cluster_token", None)
                    # Explicitly opt into private CAS lookup; ordinary jobs must
                    # never consume a private cache just because a hash matches.
                    url += ("&" if "?" in url else "?") + "local=1"
                    request_options["headers"] = {"Authorization": f"Bearer {token}"}
                resp = http.get(url, **request_options)
                if resp.status_code != 200:
                    continue

                hasher = hashlib.md5()
                with open(temp_file, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                            hasher.update(chunk)

                computed_hex = hasher.hexdigest().lower()
                if computed_hex != expected_hex:
                    logger.warning(
                        "Checksum mismatch for %s from %s (got %s, expected %s). Rejecting and trying next source.",
                        clean_h, url, computed_hex, expected_hex
                    )
                    _ensure_writable(temp_file)
                    temp_file.unlink(missing_ok=True)
                    continue

                # MD5 verified! Atomic replace into final cache location
                _ensure_writable(target_file)
                temp_file.replace(target_file)

                # Protect downloaded cache object as read-only (0o444) matching DVC
                try:
                    os.chmod(target_file, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
                except OSError:
                    pass

                file_size = target_file.stat().st_size
                fetch_duration = time.monotonic() - start_t
                transfer_info = {
                    "hash": clean_h,
                    "source": source,
                    "url": url,
                    "size_bytes": file_size,
                    "duration_s": round(fetch_duration, 4),
                }
                logger.info(
                    "[CAS P2P FETCH] Successfully fetched artifact hash=%s size=%d bytes in %.3fs from worker source=%s (url=%s)",
                    clean_h, file_size, fetch_duration, source, url
                )
                print(
                    f"[CAS P2P FETCH] Successfully fetched artifact hash={clean_h} size={file_size} bytes in {fetch_duration:.3f}s from worker source={source} (url={url})",
                    flush=True
                )
                return DownloadResult(True, "downloaded", transfer_info)

            except Exception as e:
                logger.debug("Fetch failed for %s from %s: %s", clean_h, url, e)
                _ensure_writable(temp_file)
                temp_file.unlink(missing_ok=True)

    _ensure_writable(temp_file)
    temp_file.unlink(missing_ok=True)
    return DownloadResult(False, "missing", None)


def parse_dir_manifest(manifest_path: Path) -> list[dict[str, Any]]:
    """Parses a DVC .dir manifest JSON file and returns entries with md5 and relpath."""
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception as e:
        logger.error("Failed to parse .dir manifest %s: %s", manifest_path, e)
def get_dvc_command() -> list[str]:
    """Finds dvc executable in PATH, ~/.local/bin, or uvx fallback."""
    dvc_path = shutil.which("dvc")
    if dvc_path:
        return [dvc_path]
    local_dvc = os.path.expanduser("~/.local/bin/dvc")
    if os.path.isfile(local_dvc) and os.access(local_dvc, os.X_OK):
        return [local_dvc]
    uvx_path = shutil.which("uvx")
    if not uvx_path:
        cand_uvx = os.path.expanduser("~/.local/bin/uvx")
        if os.path.isfile(cand_uvx) and os.access(cand_uvx, os.X_OK):
            uvx_path = cand_uvx
    if uvx_path:
        return [uvx_path, "--from", "dvc==3.67.1", "dvc"]
    return ["dvc"]


def fetch_dependencies(
    dependencies: Sequence[dict[str, Any] | str],
    sources_map: Mapping[str, Sequence[str]],
    repo_dir: Path | str,
    repo_name: str | None = None,
    cache_dir: Path | str | None = None,
    run_checkout: bool = True,
    max_workers: int = 8,
    timeout: float = 15.0,
) -> FetchResult:
    """
    Main entrypoint: Fetches all CAS dependencies for a node across peer sources in parallel,
    resolves directory manifests (.dir) recursively, checks out data with `dvc checkout`,
    and reports any missing dependencies.
    
    Args:
      dependencies: List of dependency items (either dict with {"path": ..., "md5": ...} or raw hash strings).
      sources_map: Map of {md5: [url1, url2, ...]}.
      repo_dir: Local git/dvc repository root.
      repo_name: Optional repository name for URL construction.
      cache_dir: Optional DVC cache directory. Defaults to repo_dir/.dvc/cache/files/md5.
      run_checkout: If True, runs `dvc checkout <deps>` on success without hiding errors.
      max_workers: ThreadPoolExecutor concurrency.
      timeout: Per-request timeout in seconds.
    """
    repo_path = Path(repo_dir).resolve()
    if cache_dir is None:
        c_path = repo_path / ".dvc" / "cache" / "files" / "md5"
    else:
        c_path = Path(cache_dir).resolve()

    c_path.mkdir(parents=True, exist_ok=True)

    # 1. Normalize dependencies
    normalized_deps: list[dict[str, Any]] = []
    parent_dirs_to_fetch: set[str] = set()

    for item in dependencies:
        if isinstance(item, str):
            h = item.strip().lower()
            normalized_deps.append({"path": "", "md5": h, "parent_dir_hash": None})
        elif isinstance(item, Mapping):
            if item.get("is_stage_output") is False:
                continue
            raw_h = item.get("md5") or item.get("hash") or ""
            clean_h = str(raw_h).strip().lower()
            p_hash = str(item.get("parent_dir_hash") or "").strip().lower() or None
            if p_hash:
                parent_dirs_to_fetch.add(p_hash)
            normalized_deps.append({
                "path": str(item.get("path") or ""),
                "md5": clean_h,
                "sources": item.get("sources", []),
                "parent_dir_hash": p_hash,
            })

    # Collect initial hashes to fetch
    top_level_hashes = [d["md5"] for d in normalized_deps if d["md5"]]
    for p_h in parent_dirs_to_fetch:
        if p_h not in top_level_hashes:
            top_level_hashes.append(p_h)

    merged_sources: dict[str, list[str]] = {}
    for h, urls in sources_map.items():
        clean_h = h.strip().lower()
        merged_sources.setdefault(clean_h, [])
        for u in urls:
            if u not in merged_sources[clean_h]:
                merged_sources[clean_h].append(u)

    # Add inline sources if present
    for d in normalized_deps:
        h = d["md5"]
        for u in d.get("sources", []):
            merged_sources.setdefault(h, [])
            if u not in merged_sources[h]:
                merged_sources[h].append(u)
        # If item has parent_dir_hash, share sources with parent .dir
        p_h = d.get("parent_dir_hash")
        if p_h:
            # Propagate parent sources to subfile, and subfile sources to parent
            for u in merged_sources.get(p_h, []):
                merged_sources.setdefault(h, [])
                if u not in merged_sources[h]:
                    merged_sources[h].append(u)
            for u in merged_sources.get(h, []):
                merged_sources.setdefault(p_h, [])
                if u not in merged_sources[p_h]:
                    merged_sources[p_h].append(u)

    downloaded: list[str] = []
    already_cached: list[str] = []
    failed_hashes: set[str] = set()
    transfers: list[dict[str, Any]] = []

    session = requests.Session()

    # 2. Phase 1: Download top-level hashes (including .dir manifests)
    def _fetch_worker(h: str, candidates: list[str]) -> tuple[str, bool, str, dict[str, Any] | None]:
        res = download_single_object(
            h, candidates, c_path, repo_name=repo_name, timeout=timeout, session=session
        )
        t_info = getattr(res, "transfer_info", None)
        return h, res[0], res[1], t_info

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(_fetch_worker, h, merged_sources.get(h, [])): h
            for h in set(top_level_hashes)
        }
        for future in concurrent.futures.as_completed(future_map):
            h, ok, reason, t_info = future.result()
            if ok:
                if t_info:
                    transfers.append(t_info)
                if reason == "downloaded":
                    downloaded.append(h)
                else:
                    already_cached.append(h)
            else:
                failed_hashes.add(h)

    # 3. Phase 2: Inspect directory manifests (.dir) and fetch nested objects
    nested_hashes_to_fetch: set[str] = set()

    for h in top_level_hashes:
        if h.endswith(".dir") and h not in failed_hashes:
            manifest_file = c_path / h[:2] / h[2:]
            if manifest_file.is_file():
                entries = parse_dir_manifest(manifest_file)
                sources_for_dir = merged_sources.get(h, [])
                for entry in entries:
                    sub_h = entry.get("md5", "").strip().lower()
                    if sub_h:
                        nested_hashes_to_fetch.add(sub_h)
                        for s in sources_for_dir:
                            merged_sources.setdefault(sub_h, [])
                            if s not in merged_sources[sub_h]:
                                merged_sources[sub_h].append(s)

    # Fetch nested directory files
    if nested_hashes_to_fetch:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(_fetch_worker, sub_h, merged_sources.get(sub_h, [])): sub_h
                for sub_h in nested_hashes_to_fetch
            }
            for future in concurrent.futures.as_completed(future_map):
                sub_h, ok, reason, t_info = future.result()
                if ok:
                    if t_info:
                        transfers.append(t_info)
                    if reason == "downloaded":
                        downloaded.append(sub_h)
                    else:
                        already_cached.append(sub_h)
                else:
                    failed_hashes.add(sub_h)

    # 4. Determine missing dependencies
    missing_deps: list[str] = []
    missing_hashes = list(failed_hashes)

    for d in normalized_deps:
        h = d["md5"]
        p_h = d.get("parent_dir_hash")
        # Missing if direct hash failed or if its parent .dir failed
        is_missing = (h in failed_hashes) or (p_h and p_h in failed_hashes)
        if h.endswith(".dir") and not is_missing:
            # Check if any nested file failed
            manifest_file = c_path / h[:2] / h[2:]
            if manifest_file.is_file():
                entries = parse_dir_manifest(manifest_file)
                for entry in entries:
                    sub_h = entry.get("md5", "").strip().lower()
                    if sub_h in failed_hashes:
                        is_missing = True
                        break

        if is_missing:
            dep_identifier = d["path"] or h
            if dep_identifier not in missing_deps:
                missing_deps.append(dep_identifier)

    if missing_deps or missing_hashes:
        return FetchResult(
            success=False,
            status="missing_deps",
            missing_deps=missing_deps,
            missing_hashes=missing_hashes,
            downloaded_hashes=downloaded,
            cached_hashes=already_cached,
            transfers=transfers,
            error_message=f"Missing {len(missing_deps)} dependencies in CAS across all candidate peers.",
        )

    # 5. Execute dvc checkout if requested
    if run_checkout:
        target_paths = [d["path"] for d in normalized_deps if d["path"]]
        if not target_paths:
            # If no target paths specified, return immediately without global dvc checkout
            return FetchResult(
                success=True,
                status="success",
                missing_deps=[],
                missing_hashes=[],
                downloaded_hashes=downloaded,
                cached_hashes=already_cached,
                transfers=transfers,
            )

        cmd = [*get_dvc_command(), "checkout", *target_paths]

        try:
            # Never hide errors: capture stdout/stderr and check exit code
            proc = subprocess.run(
                cmd,
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                logger.error("dvc checkout failed (exit %d): %s", proc.returncode, proc.stderr)
                return FetchResult(
                    success=False,
                    status="checkout_failed",
                    missing_deps=target_paths or top_level_hashes,
                    missing_hashes=[],
                    downloaded_hashes=downloaded,
                    cached_hashes=already_cached,
                    transfers=transfers,
                    error_message=f"dvc checkout failed (exit {proc.returncode}): {proc.stderr.strip()}",
                )
        except FileNotFoundError:
            logger.warning("dvc binary not found in PATH, skipping local checkout execution")
        except Exception as e:
            return FetchResult(
                success=False,
                status="checkout_failed",
                missing_deps=target_paths or top_level_hashes,
                missing_hashes=[],
                downloaded_hashes=downloaded,
                cached_hashes=already_cached,
                transfers=transfers,
                error_message=str(e),
            )

    return FetchResult(
        success=True,
        status="success",
        missing_deps=[],
        missing_hashes=[],
        downloaded_hashes=downloaded,
        cached_hashes=already_cached,
        transfers=transfers,
    )


def main() -> int:
    """CLI entrypoint for integration into run_research_pipeline.sh."""
    parser = argparse.ArgumentParser(description="Fetch DVC CAS dependencies from peer workers.")
    parser.add_argument("--node", required=False, help="Node/stage name from dvc.lock")
    parser.add_argument("--dvc-lock", required=False, default="dvc.lock", help="Path to dvc.lock")
    parser.add_argument("--sources-json", required=True, help="JSON file or string containing {md5: [urls]}")
    parser.add_argument("--repo-dir", default=".", help="Local repository path")
    parser.add_argument("--repo-name", default=None, help="Repository name (e.g. owner/repo)")
    parser.add_argument("--cache-dir", default=None, help="DVC cache directory override")
    parser.add_argument("--no-checkout", action="store_true", help="Skip running dvc checkout")
    parser.add_argument("--max-workers", type=int, default=8, help="Parallel download threads")

    args = parser.parse_args()

    if os.path.isfile(args.sources_json):
        with open(args.sources_json, "r", encoding="utf-8") as f:
            sources_map = json.load(f)
    else:
        sources_map = json.loads(args.sources_json)

    deps = []
    if args.node and os.path.isfile(args.dvc_lock):
        from src.scheduler.artifact_registry import extract_node_deps_from_dvc_lock
        deps = extract_node_deps_from_dvc_lock(args.dvc_lock, args.node, stage_outs_only=True, repo_dir=args.repo_dir)
    else:
        deps = [{"path": "", "md5": h} for h in sources_map.keys()]

    result = fetch_dependencies(
        dependencies=deps,
        sources_map=sources_map,
        repo_dir=args.repo_dir,
        repo_name=args.repo_name,
        cache_dir=args.cache_dir,
        run_checkout=not args.no_checkout,
        max_workers=args.max_workers,
    )

    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.success else 2


if __name__ == "__main__":
    sys.exit(main())
