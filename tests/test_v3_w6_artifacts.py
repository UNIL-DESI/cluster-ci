"""
Unit and Integration Tests for Cluster-CI v3 W6:
Artifact Registry and Multi-Source CAS Dependency Fetcher.
Includes real DVC 3.67.1 checkout integration test on sub-path dependencies.
"""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

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
        self.conn.execute("CREATE TABLE jobs(job_id TEXT PRIMARY KEY, is_local INTEGER)")
        self.conn.executemany("INSERT INTO jobs VALUES (?, 0)", [("job-1",), ("job-2",)])

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

    def test_subpath_dependency_affinity_and_sources_resolution(self):
        ensure_schema(self.conn)

        dir_hash = "998d2f0fb26df6b65c60155b6fd84245.dir"
        sub_hash = "55b84a9d317184fe61224bfb4a060fb0"

        # Upstream stage records directory and its indexed nested subfile
        outs = [
            {"path": "data/raw", "md5": dir_hash, "size_bytes": 100000, "is_dir": True},
            {"path": "data/raw/train.csv", "md5": sub_hash, "size_bytes": 25000, "is_dir": False, "parent_dir_hash": dir_hash},
        ]
        record_node_outputs(self.conn, "job-1", "prep_stage", "worker-A", outs)

        # 1. Downstream stage needing only sub_hash must resolve affinity in bytes on worker-A
        bytes_sub = affinity_bytes(self.conn, [sub_hash], "worker-A")
        self.assertEqual(bytes_sub, 25000)

        # 2. sources_for looking for sub_hash must resolve worker-A
        online = {"worker-A": "http://worker-a:6000"}
        srcs = sources_for(self.conn, [sub_hash], online)
        self.assertEqual(srcs[sub_hash], ["http://worker-a:6000"])

        # 3. sources_for looking for dir_hash must also resolve worker-A
        srcs_dir = sources_for(self.conn, [dir_hash], online)
        self.assertEqual(srcs_dir[dir_hash], ["http://worker-a:6000"])

    def test_extract_from_dvc_lock_yaml_with_subpath_resolution(self):
        sample_lock = """
schema: '2.0'
stages:
  prep:
    cmd: python prep.py
    outs:
    - path: data/raw
      hash: md5
      md5: 1199066d07c4e403fa9e13c0c3e42748.dir
      size: 1000
  train:
    cmd: python train.py
    deps:
    - path: data/raw/train.csv
      hash: md5
      md5: 7e55db001d319a94b0b713529a756623
      size: 500
    outs:
    - path: models/model.pt
      hash: md5
      md5: 99999999999999999999999999999999
      size: 67890
"""
        # extract_node_deps_from_dvc_lock must detect that data/raw/train.csv belongs to data/raw (.dir)
        deps = extract_node_deps_from_dvc_lock(sample_lock, "train")
        # Must return the subfile dep AND the parent .dir manifest
        dep_map = {d["md5"]: d for d in deps}
        self.assertIn("7e55db001d319a94b0b713529a756623", dep_map)
        self.assertIn("1199066d07c4e403fa9e13c0c3e42748.dir", dep_map)
        self.assertEqual(dep_map["7e55db001d319a94b0b713529a756623"]["parent_dir_hash"], "1199066d07c4e403fa9e13c0c3e42748.dir")


class MockWorkerHTTPHandler(BaseHTTPRequestHandler):
    """Mock HTTP server serving CAS artifacts by MD5."""

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        artifacts = getattr(self.server, "artifacts", {})
        parts = self.path.split("/")
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
    """Integration tests for fetch_cas_dependencies.py."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.repo_dir = Path(self.temp_dir) / "repo"
        self.cache_dir = self.repo_dir / ".dvc" / "cache" / "files" / "md5"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

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
        # Ensure files are writable before cleanup
        for root, dirs, files in os.walk(self.temp_dir):
            for f in files:
                try:
                    os.chmod(os.path.join(root, f), stat.S_IWRITE | stat.S_IREAD)
                except OSError:
                    pass
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_multi_source_distributed_fetch_and_md5_verification(self):
        content_a = b"Dataset alpha content heavy payload 12345"
        hash_a = hashlib.md5(content_a).hexdigest()

        content_b = b"Model beta weights checkpoint 67890"
        hash_b = hashlib.md5(content_b).hexdigest()

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

        self.server1.artifacts[hash_val] = content_corrupt
        self.server2.artifacts[hash_val] = content_valid

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

        cached_file = self.cache_dir / hash_val[:2] / hash_val[2:]
        self.assertTrue(cached_file.is_file())
        self.assertEqual(compute_file_md5(cached_file), hash_val)

    def test_readonly_corrupted_cache_file_properly_unlinked_and_replaced(self):
        content_valid = b"Valid pristine payload from server"
        hash_val = hashlib.md5(content_valid).hexdigest()
        self.server1.artifacts[hash_val] = content_valid

        # Create a corrupted read-only cache file (simulating DVC 0o444 file under Windows)
        target = self.cache_dir / hash_val[:2] / hash_val[2:]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"CORRUPTED BYTES")
        os.chmod(target, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)

        ok, reason = download_single_object(
            hash_val, [self.url1], self.cache_dir
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "downloaded")
        self.assertEqual(compute_file_md5(target), hash_val)

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

        self.assertFalse(result.success)
        self.assertEqual(result.status, "missing_deps")
        self.assertIn("important_dataset.parquet", result.missing_deps)
        self.assertIn(missing_hash, result.missing_hashes)

    def test_dvc_dir_manifest_and_nested_files_recursively_fetched(self):
        sub1_bytes = b"Sub file 1 content"
        sub1_hash = hashlib.md5(sub1_bytes).hexdigest()
        sub2_bytes = b"Sub file 2 content"
        sub2_hash = hashlib.md5(sub2_bytes).hexdigest()

        manifest_list = [
            {"md5": sub1_hash, "relpath": "file1.txt"},
            {"md5": sub2_hash, "relpath": "file2.txt"},
        ]
        manifest_bytes = json.dumps(manifest_list).encode("utf-8")
        manifest_md5 = f"{hashlib.md5(manifest_bytes).hexdigest()}.dir"

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

        dest = self.cache_dir / h[:2] / h[2:]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)

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

    def test_empty_target_paths_does_not_call_dvc_checkout(self):
        content = b"Some data"
        h = hashlib.md5(content).hexdigest()
        self.server1.artifacts[h] = content

        sources_map = {h: [self.url1]}
        # Empty path: should fetch hash but never run global dvc checkout
        deps = [{"path": "", "md5": h}]

        with patch("subprocess.run") as mock_run:
            result = fetch_dependencies(
                dependencies=deps,
                sources_map=sources_map,
                repo_dir=self.repo_dir,
                cache_dir=self.cache_dir,
                run_checkout=True,
            )
            mock_run.assert_not_called()

        self.assertTrue(result.success)
        self.assertEqual(result.status, "success")

    def test_dvc_checkout_failure_is_not_masked(self):
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

        self.assertFalse(result.success)
        self.assertEqual(result.status, "checkout_failed")
        self.assertIn("ERROR: unable to link checkout files", result.error_message)

    def test_cli_execution_with_sources(self):
        content = b"CLI test payload"
        h = hashlib.md5(content).hexdigest()
        self.server1.artifacts[h] = content

        from src.runner.fetch_cas_dependencies import main
        import sys

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


class TestRealDvcCheckoutIntegration(unittest.TestCase):
    """
    Real integration test with real DVC 3.67.1 executing on disk:
      - Upstream stage_a produces a directory 'data/sub' (containing f1.txt and f2.txt)
      - Upstream stage_b depends on 'data/sub/f1.txt' (subpath dependency)
      - Downstream fresh repository has empty cache
      - Objects are served over local HTTP server
      - fetch_dependencies retrieves both .dir manifest and subfile
      - Real `dvc checkout` succeeds without mocking!
    """

    def setUp(self):
        self.temp_root = Path(tempfile.mkdtemp())
        self.upstream_dir = self.temp_root / "upstream"
        self.downstream_dir = self.temp_root / "downstream"
        self.upstream_dir.mkdir(parents=True)
        self.downstream_dir.mkdir(parents=True)

        from src.runner.fetch_cas_dependencies import get_dvc_command
        dvc_cmd = get_dvc_command()

        # 1. Initialize Upstream DVC Repo
        subprocess.run(["git", "init"], cwd=self.upstream_dir, check=True, capture_output=True)
        subprocess.run([*dvc_cmd, "init", "--no-scm"], cwd=self.upstream_dir, check=True, capture_output=True)

        # Create stages using dedicated script files to avoid cross-platform shell quoting issues
        script_a = self.upstream_dir / "stage_a.py"
        script_a.write_text("import pathlib; p=pathlib.Path('data/sub'); p.mkdir(parents=True, exist_ok=True); (p/'f1.txt').write_text('content_f1'); (p/'f2.txt').write_text('content_f2')", encoding="utf-8")
        subprocess.run([*dvc_cmd, "stage", "add", "-n", "stage_a", "-o", "data/sub", sys.executable, "stage_a.py"], cwd=self.upstream_dir, check=True, capture_output=True)
        subprocess.run([*dvc_cmd, "repro", "stage_a"], cwd=self.upstream_dir, check=True, capture_output=True)

        script_b = self.upstream_dir / "stage_b.py"
        script_b.write_text("open('out.txt','w').write('done')", encoding="utf-8")
        subprocess.run([*dvc_cmd, "stage", "add", "-n", "stage_b", "-d", "data/sub/f1.txt", "-o", "out.txt", sys.executable, "stage_b.py"], cwd=self.upstream_dir, check=True, capture_output=True)
        subprocess.run([*dvc_cmd, "repro", "stage_b"], cwd=self.upstream_dir, check=True, capture_output=True)

        # 2. Host all upstream cache files via Mock HTTP Server
        self.server = HTTPServer(("127.0.0.1", 0), MockWorkerHTTPHandler)
        self.port = self.server.server_port
        self.server_url = f"http://127.0.0.1:{self.port}"
        self.server.artifacts = {}

        up_cache = self.upstream_dir / ".dvc" / "cache" / "files" / "md5"
        for p in up_cache.glob("**/*"):
            if p.is_file():
                h_name = f"{p.parent.name}{p.name}"
                self.server.artifacts[h_name] = p.read_bytes()

        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

        # 3. Setup Downstream Repo (cloned lock, empty cache)
        shutil.copytree(self.upstream_dir / ".git", self.downstream_dir / ".git")
        shutil.copy(self.upstream_dir / "dvc.lock", self.downstream_dir / "dvc.lock")
        shutil.copy(self.upstream_dir / "dvc.yaml", self.downstream_dir / "dvc.yaml")
        (self.downstream_dir / ".dvc" / "cache" / "files" / "md5").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for root, dirs, files in os.walk(self.temp_root):
            for f in files:
                try:
                    os.chmod(os.path.join(root, f), stat.S_IWRITE | stat.S_IREAD)
                except OSError:
                    pass
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def test_real_dvc_checkout_on_subpath_dependency(self):
        # 1. Extract stage_b dependencies from dvc.lock (must include subpath dep AND parent .dir)
        dvc_lock_file = self.downstream_dir / "dvc.lock"
        deps = extract_node_deps_from_dvc_lock(str(dvc_lock_file), "stage_b")

        # Map all hashes to our server URL
        sources_map = {d["md5"]: [self.server_url] for d in deps}

        # 2. Execute fetch_dependencies with REAL dvc checkout
        res = fetch_dependencies(
            dependencies=deps,
            sources_map=sources_map,
            repo_dir=self.downstream_dir,
            run_checkout=True,  # REAL DVC CHECKOUT!
        )

        self.assertTrue(res.success, f"Fetch failed: {res.error_message}")
        self.assertEqual(res.status, "success")

        # 3. Verify that data/sub/f1.txt exists on disk with correct content!
        restored_f1 = self.downstream_dir / "data" / "sub" / "f1.txt"
        self.assertTrue(restored_f1.is_file(), "data/sub/f1.txt was not restored by real dvc checkout!")
        self.assertEqual(restored_f1.read_text(), "content_f1")


if __name__ == "__main__":
    unittest.main()
