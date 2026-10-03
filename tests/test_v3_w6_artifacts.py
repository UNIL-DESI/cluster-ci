"""
Unit and Integration Tests for Cluster-CI v3 W6:
Artifact Registry and Multi-Source CAS Dependency Fetcher.
"""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest

from src.scheduler.artifact_registry import (
    affinity_bytes,
    ensure_schema,
    extract_node_deps_from_dvc_lock,
    extract_node_outputs_from_dvc_lock,
    record_node_outputs,
    sources_for,
)
from src.runner.fetch_cas_dependencies import (
    compute_file_md5,
    download_single_object,
    fetch_dependencies,
    FetchResult,
)


class TestArtifactRegistry(unittest.TestCase):
    """Tests for artifact_registry.py."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_ensure_schema_idempotency(self):
        ensure_schema(self.conn)
        ensure_schema(self.conn)
        cursor = self.conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='node_artifacts'")
        self.assertIsNotNone(cursor.fetchone())

    def test_record_node_outputs_various_formats(self):
        ensure_schema(self.conn)

        # 1. List of dicts with .dir auto-detection
        outs_list = [
            {"path": "models/model.pt", "md5": "a1b2c3d4e5f60718293a4b5c6d7e8f90", "size_bytes": 1000000},
            {"path": "data/features", "md5": "1faf98845f913f29a6961f9c7b472cca.dir", "size": 50000},
        ]
        count = record_node_outputs(self.conn, "job-1", "train", "worker-A", outs_list)
        self.assertEqual(count, 2)

        # 2. Dict format {md5: size}
        outs_dict = {
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb": 2048,
            "cccccccccccccccccccccccccccccccc.dir": {"size_bytes": 4096, "is_dir": True},
        }
        count2 = record_node_outputs(self.conn, "job-1", "eval", "worker-B", outs_dict)
        self.assertEqual(count2, 2)

        # 3. Verify directory flag
        cursor = self.conn.cursor()
        cursor.execute("SELECT md5, is_dir, size_bytes FROM node_artifacts WHERE worker_id='worker-A'")
        rows = {r[0]: (r[1], r[2]) for r in cursor.fetchall()}
        self.assertEqual(rows["a1b2c3d4e5f60718293a4b5c6d7e8f90"][0], 0)
        self.assertEqual(rows["1faf98845f913f29a6961f9c7b472cca.dir"][0], 1)

    def test_affinity_bytes_calculation_including_dir(self):
        ensure_schema(self.conn)

        h1 = "11111111111111111111111111111111"
        h2 = "22222222222222222222222222222222.dir"
        h3 = "33333333333333333333333333333333"

        # Worker A has h1 (10 MB) and h2 (.dir, 50 MB)
        record_node_outputs(
            self.conn, "job-1", "node-1", "worker-A",
            [{"md5": h1, "size_bytes": 10 * 1024 * 1024}, {"md5": h2, "size_bytes": 50 * 1024 * 1024}]
        )
        # Duplicate record in job-2 on Worker A (same hash): should not double-count
        record_node_outputs(
            self.conn, "job-2", "node-1", "worker-A",
            [{"md5": h1, "size_bytes": 10 * 1024 * 1024}]
        )

        # Worker B has h3 (20 MB)
        record_node_outputs(
            self.conn, "job-1", "node-2", "worker-B",
            [{"md5": h3, "size_bytes": 20 * 1024 * 1024}]
        )

        # Affinity for Worker A needing [h1, h2, h3]
        bytes_worker_a = affinity_bytes(self.conn, [h1, h2, h3], "worker-A")
        self.assertEqual(bytes_worker_a, 60 * 1024 * 1024)

        # Affinity for Worker B needing [h1, h2, h3]
        bytes_worker_b = affinity_bytes(self.conn, [h1, h2, h3], "worker-B")
        self.assertEqual(bytes_worker_b, 20 * 1024 * 1024)

        # Affinity for Worker C (no artifacts)
        bytes_worker_c = affinity_bytes(self.conn, [h1, h2, h3], "worker-C")
        self.assertEqual(bytes_worker_c, 0)

    def test_sources_for_multiple_online_workers(self):
        ensure_schema(self.conn)

        h_shared = "aaaa1111aaaa1111aaaa1111aaaa1111"
        h_worker1 = "bbbb2222bbbb2222bbbb2222bbbb2222"
        h_worker2 = "cccc3333cccc3333cccc3333cccc3333"
        h_offline = "dddd4444dddd4444dddd4444dddd4444"

        # Worker 1 holds h_shared and h_worker1
        record_node_outputs(self.conn, "job-1", "n1", "w1", [h_shared, h_worker1])
        # Worker 2 holds h_shared and h_worker2
        record_node_outputs(self.conn, "job-1", "n2", "w2", [h_shared, h_worker2])
        # Worker 3 (offline) holds h_offline
        record_node_outputs(self.conn, "job-1", "n3", "w3_offline", [h_offline])

        online_workers = {
            "w1": "http://10.0.0.1:6000",
            "w2": "http://10.0.0.2:6000",
        }

        routes = sources_for(self.conn, [h_shared, h_worker1, h_worker2, h_offline, "unknown_hash"], online_workers)

        # Shared hash must list both online workers
        self.assertIn("http://10.0.0.1:6000", routes[h_shared])
        self.assertIn("http://10.0.0.2:6000", routes[h_shared])
        self.assertEqual(len(routes[h_shared]), 2)

        # Exclusive hashes
        self.assertEqual(routes[h_worker1], ["http://10.0.0.1:6000"])
        self.assertEqual(routes[h_worker2], ["http://10.0.0.2:6000"])

        # Offline worker must not be returned
        self.assertEqual(routes[h_offline], [])
        self.assertEqual(routes["unknown_hash"], [])

    def test_extract_from_dvc_lock_yaml(self):
        sample_lock = """
schema: '2.0'
stages:
  train:
    cmd: python train.py
    deps:
    - path: data/prep.parquet
      hash: md5
      md5: 88888888888888888888888888888888
      size: 12345
    outs:
    - path: models/model.pt
      hash: md5
      md5: 99999999999999999999999999999999
      size: 67890
    - path: models/checkpoints
      hash: md5
      md5: 77777777777777777777777777777777.dir
      size: 4000
"""
        outs = extract_node_outputs_from_dvc_lock(sample_lock, "train")
        self.assertEqual(len(outs), 2)
        self.assertEqual(outs[0]["md5"], "99999999999999999999999999999999")
        self.assertFalse(outs[0]["is_dir"])
        self.assertEqual(outs[1]["md5"], "77777777777777777777777777777777.dir")
        self.assertTrue(outs[1]["is_dir"])

        deps = extract_node_deps_from_dvc_lock(sample_lock, "train")
        self.assertEqual(len(deps), 1)
        self.assertEqual(deps[0]["md5"], "88888888888888888888888888888888")
        self.assertEqual(deps[0]["size_bytes"], 12345)


class MockWorkerHTTPHandler(BaseHTTPRequestHandler):
    """Mock HTTP server serving CAS artifacts by MD5."""

    def log_message(self, format, *args):
        # Suppress noisy HTTP request logging during tests
        pass

    def do_GET(self):
        # Inspect stored artifacts on the server instance
        artifacts = getattr(self.server, "artifacts", {})

        # URL path parsing: looks for md5 in path /xx/rest
        # Supports /fetch_artifact/<repo>/.dvc/cache/files/md5/xx/suffix
        # or /fetch_artifact/.dvc/cache/files/md5/xx/suffix
        parts = self.path.split("/")
        # Find prefix/suffix
        found_data = None

        for i in range(len(parts) - 1):
            if len(parts[i]) == 2 and len(parts[i + 1]) >= 30:
                reconstructed_hash = f"{parts[i]}{parts[i + 1]}"
                if reconstructed_hash in artifacts:
                    found_data = artifacts[reconstructed_hash]
                    break

        if found_data is not None:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(found_data)))
            self.end_headers()
            self.wfile.write(found_data)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")


class TestFetchCasDependencies(unittest.TestCase):
    """Integration tests for fetch_cas_dependencies.py with real local HTTP servers."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.repo_dir = Path(self.temp_dir) / "repo"
        self.cache_dir = self.repo_dir / ".dvc" / "cache" / "files" / "md5"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Launch two local HTTP mock workers
        self.server1 = HTTPServer(("127.0.0.1", 0), MockWorkerHTTPHandler)
        self.server2 = HTTPServer(("127.0.0.1", 0), MockWorkerHTTPHandler)
        self.port1 = self.server1.server_port
        self.port2 = self.server2.server_port
        self.url1 = f"http://127.0.0.1:{self.port1}"
        self.url2 = f"http://127.0.0.1:{self.port2}"

        self.server1.artifacts = {}
        self.server2.artifacts = {}

        self.t1 = threading.Thread(target=self.server1.serve_forever, daemon=True)
        self.t2 = threading.Thread(target=self.server2.serve_forever, daemon=True)
        self.t1.start()
        self.t2.start()

    def tearDown(self):
        self.server1.shutdown()
        self.server2.shutdown()
        self.server1.server_close()
        self.server2.server_close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_multi_source_distributed_fetch_and_md5_verification(self):
        # Create 2 valid test artifacts
        content_a = b"Dataset alpha content heavy payload 12345"
        hash_a = hashlib.md5(content_a).hexdigest()

        content_b = b"Model beta weights checkpoint 67890"
        hash_b = hashlib.md5(content_b).hexdigest()

        # Server 1 has A, Server 2 has B
        self.server1.artifacts[hash_a] = content_a
        self.server2.artifacts[hash_b] = content_b

        sources_map = {
            hash_a: [self.url1],
            hash_b: [self.url2],
        }

        deps = [
            {"path": "data/alpha.csv", "md5": hash_a},
            {"path": "models/beta.pt", "md5": hash_b},
        ]

        result = fetch_dependencies(
            dependencies=deps,
            sources_map=sources_map,
            repo_dir=self.repo_dir,
            cache_dir=self.cache_dir,
            run_checkout=False,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.status, "success")
        self.assertEqual(len(result.missing_deps), 0)
        self.assertIn(hash_a, result.downloaded_hashes)
        self.assertIn(hash_b, result.downloaded_hashes)

        # Check files on disk in CAS
        target_a = self.cache_dir / hash_a[:2] / hash_a[2:]
        target_b = self.cache_dir / hash_b[:2] / hash_b[2:]
        self.assertTrue(target_a.is_file())
        self.assertTrue(target_b.is_file())
        self.assertEqual(compute_file_md5(target_a), hash_a)
        self.assertEqual(compute_file_md5(target_b), hash_b)

    def test_corrupted_object_rejection_and_fallback_to_next_source(self):
        content_valid = b"Valid uncorrupted payload from server 2"
        hash_val = hashlib.md5(content_valid).hexdigest()
        content_corrupt = b"CORRUPTED BYTES INVALID CHECKSUM"

        # Server 1 has corrupted content, Server 2 has valid content
        self.server1.artifacts[hash_val] = content_corrupt
        self.server2.artifacts[hash_val] = content_valid

        # Provide server 1 first, then server 2
        sources_map = {
            hash_val: [self.url1, self.url2]
        }

        deps = [{"path": "data/file.bin", "md5": hash_val}]

        result = fetch_dependencies(
            dependencies=deps,
            sources_map=sources_map,
            repo_dir=self.repo_dir,
            cache_dir=self.cache_dir,
            run_checkout=False,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.status, "success")
        self.assertIn(hash_val, result.downloaded_hashes)

        # The cached file MUST be the valid one
        cached_file = self.cache_dir / hash_val[:2] / hash_val[2:]
        self.assertTrue(cached_file.is_file())
        self.assertEqual(compute_file_md5(cached_file), hash_val)

    def test_missing_object_everywhere_returns_explicit_missing_deps(self):
        missing_hash = "ffffffffffffffffffffffffffffffff"
        sources_map = {
            missing_hash: [self.url1, self.url2]
        }
        deps = [{"path": "important_dataset.parquet", "md5": missing_hash}]

        result = fetch_dependencies(
            dependencies=deps,
            sources_map=sources_map,
            repo_dir=self.repo_dir,
            cache_dir=self.cache_dir,
            run_checkout=False,
        )

        # Never false positive success
        self.assertFalse(result.success)
        self.assertEqual(result.status, "missing_deps")
        self.assertIn("important_dataset.parquet", result.missing_deps)
        self.assertIn(missing_hash, result.missing_hashes)

    def test_dvc_dir_manifest_and_nested_files_recursively_fetched(self):
        # Create 2 sub-files
        sub1_bytes = b"Sub file 1 content"
        sub1_hash = hashlib.md5(sub1_bytes).hexdigest()
        sub2_bytes = b"Sub file 2 content"
        sub2_hash = hashlib.md5(sub2_bytes).hexdigest()

        # Create .dir manifest JSON
        manifest_list = [
            {"md5": sub1_hash, "relpath": "file1.txt"},
            {"md5": sub2_hash, "relpath": "file2.txt"},
        ]
        manifest_bytes = json.dumps(manifest_list).encode("utf-8")
        manifest_md5 = f"{hashlib.md5(manifest_bytes).hexdigest()}.dir"

        # Server 2 holds the .dir manifest AND sub files
        self.server2.artifacts[manifest_md5] = manifest_bytes
        self.server2.artifacts[sub1_hash] = sub1_bytes
        self.server2.artifacts[sub2_hash] = sub2_bytes

        sources_map = {
            manifest_md5: [self.url2],
        }

        deps = [{"path": "dataset_folder", "md5": manifest_md5}]

        result = fetch_dependencies(
            dependencies=deps,
            sources_map=sources_map,
            repo_dir=self.repo_dir,
            cache_dir=self.cache_dir,
            run_checkout=False,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.status, "success")
        self.assertEqual(len(result.missing_deps), 0)

        # Check that manifest AND both sub files exist in local CAS
        man_path = self.cache_dir / manifest_md5[:2] / manifest_md5[2:]
        s1_path = self.cache_dir / sub1_hash[:2] / sub1_hash[2:]
        s2_path = self.cache_dir / sub2_hash[:2] / sub2_hash[2:]

        self.assertTrue(man_path.is_file())
        self.assertTrue(s1_path.is_file())
        self.assertTrue(s2_path.is_file())
        self.assertEqual(compute_file_md5(s1_path), sub1_hash)
        self.assertEqual(compute_file_md5(s2_path), sub2_hash)

    def test_already_cached_objects_are_not_refetched(self):
        content = b"Local cached valid data"
        h = hashlib.md5(content).hexdigest()

        # Pre-populate local cache
        dest = self.cache_dir / h[:2] / h[2:]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)

        # Server does not have it, but it should not be queried
        sources_map = {h: [self.url1]}
        deps = [{"path": "cached_file.txt", "md5": h}]

        result = fetch_dependencies(
            dependencies=deps,
            sources_map=sources_map,
            repo_dir=self.repo_dir,
            cache_dir=self.cache_dir,
            run_checkout=False,
        )

        self.assertTrue(result.success)
        self.assertIn(h, result.cached_hashes)
        self.assertEqual(len(result.downloaded_hashes), 0)

    def test_dvc_checkout_failure_is_not_masked(self):
        from unittest.mock import patch, MagicMock
        content = b"Some data"
        h = hashlib.md5(content).hexdigest()
        self.server1.artifacts[h] = content

        sources_map = {h: [self.url1]}
        deps = [{"path": "data/output.csv", "md5": h}]

        mock_proc = MagicMock()
        mock_proc.returncode = 127
        mock_proc.stderr = "ERROR: unable to link checkout files"

        with patch("subprocess.run", return_value=mock_proc):
            result = fetch_dependencies(
                dependencies=deps,
                sources_map=sources_map,
                repo_dir=self.repo_dir,
                cache_dir=self.cache_dir,
                run_checkout=True,
            )

        # Errors must NOT be masked!
        self.assertFalse(result.success)
        self.assertEqual(result.status, "checkout_failed")
        self.assertIn("ERROR: unable to link checkout files", result.error_message)

    def test_cli_execution_with_sources(self):
        content = b"CLI test payload"
        h = hashlib.md5(content).hexdigest()
        self.server1.artifacts[h] = content

        from src.runner.fetch_cas_dependencies import main
        import sys
        from unittest.mock import patch

        sources_json = json.dumps({h: [self.url1]})
        test_args = [
            "fetch_cas_dependencies",
            "--sources-json", sources_json,
            "--repo-dir", str(self.repo_dir),
            "--cache-dir", str(self.cache_dir),
            "--no-checkout",
        ]

        with patch.object(sys, "argv", test_args):
            exit_code = main()
            self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()

