import os
import sys
import unittest
from unittest.mock import patch, MagicMock
import tempfile
import shutil

from src.scheduler.worker_agent import (
    parse_nvidia_smi_output,
    parse_docker_size,
    get_docker_images,
    get_cpu_info,
    get_arch_info,
    get_storage_info,
    get_worker_capabilities,
    build_registration_payload,
    execute_job,
    cleanup_active_jobs_and_containers,
    app,
    WORKER_ID
)
import src.scheduler.worker_agent as worker_agent

class TestWorkerCapacities(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        worker_agent.REPOS_DIR = self.test_dir
        self.app = app.test_client()
        self.app.testing = True

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)
        with worker_agent.job_lock:
            worker_agent.active_executors.clear()
            worker_agent.current_job_id = None
            worker_agent.current_process = None

    def test_detection_gb10_unified_memory(self):
        """Test Case 1: NVIDIA GB10 Grace-Blackwell with [N/A] reported by nvidia-smi.
        Should detect unified memory, report system RAM as VRAM, and set unified_memory=1.
        """
        # Recorded output of nvidia-smi for Grace-Blackwell GB10
        gb10_output = "0, NVIDIA GB10, [N/A], [N/A], [N/A]"
        total_ram = 120.0
        avail_ram = 112.5

        data = parse_nvidia_smi_output(
            gb10_output,
            total_ram_gb=total_ram,
            available_ram_gb=avail_ram
        )

        self.assertEqual(data["gpu_count"], 1)
        self.assertEqual(data["gpu_name"], "NVIDIA GB10")
        self.assertEqual(data["unified_memory"], 1)
        self.assertEqual(data["total_vram_gb"], 120.0)
        self.assertEqual(data["available_vram_gb"], 112.5)
        self.assertEqual(data["vram_per_gpu"], [120.0])
        self.assertEqual(len(data["gpu_details"]), 1)
        self.assertEqual(data["gpu_details"][0]["unified_memory"], 1)

    def test_detection_gb10_variant_not_supported(self):
        """Test GB10 with [Not Supported] text variation."""
        gb10_output = "NVIDIA Grace Blackwell GB10, [Not Supported], [Not Supported]"
        total_ram = 128.0
        avail_ram = 100.0

        data = parse_nvidia_smi_output(
            gb10_output,
            total_ram_gb=total_ram,
            available_ram_gb=avail_ram
        )

        self.assertEqual(data["gpu_count"], 1)
        self.assertEqual(data["unified_memory"], 1)
        self.assertEqual(data["total_vram_gb"], 128.0)
        self.assertEqual(data["vram_per_gpu"], [128.0])

    def test_detection_dual_rtx3090_discrete(self):
        """Test Case 2: 2x NVIDIA GeForce RTX 3090 (discrete GPUs).
        Should detect 2 GPUs, discrete memory (unified_memory=0), per-GPU VRAM of 24 GB,
        vram_per_gpu=[24.0, 24.0], and available VRAM as minimum free.
        """
        # Recorded output of nvidia-smi for 2x RTX 3090 (isipol09)
        rtx3090_output = (
            "0, NVIDIA GeForce RTX 3090, 24576, 1024, 23552\n"
            "1, NVIDIA GeForce RTX 3090, 24576, 512, 24064"
        )

        data = parse_nvidia_smi_output(rtx3090_output)

        self.assertEqual(data["gpu_count"], 2)
        self.assertEqual(data["gpu_name"], "2x NVIDIA GeForce RTX 3090")
        self.assertEqual(data["unified_memory"], 0)
        self.assertEqual(data["total_vram_gb"], 24.0)
        self.assertEqual(data["available_vram_gb"], 23.0)  # min free: 23552 / 1024 = 23.0
        self.assertEqual(data["vram_per_gpu"], [24.0, 24.0])
        self.assertEqual(len(data["gpu_details"]), 2)
        self.assertEqual(data["gpu_details"][0]["index"], 0)
        self.assertEqual(data["gpu_details"][1]["index"], 1)
        self.assertEqual(data["gpu_details"][0]["unified_memory"], 0)

    def test_detection_no_gpu(self):
        """Test Case 3: Host with no GPU (CPU-only / nvidia-smi missing).
        Should return 0 GPUs, N/A, unified_memory=0, and empty vram_per_gpu.
        """
        data = parse_nvidia_smi_output("")

        self.assertEqual(data["gpu_count"], 0)
        self.assertEqual(data["gpu_name"], "N/A")
        self.assertEqual(data["unified_memory"], 0)
        self.assertEqual(data["total_vram_gb"], 0.0)
        self.assertEqual(data["available_vram_gb"], 0.0)
        self.assertEqual(data["vram_per_gpu"], [])
        self.assertEqual(data["gpu_details"], [])

    def test_unified_memory_env_override(self):
        """Test CLUSTER_CI_UNIFIED_MEMORY env var override."""
        # Force unified on discrete GPU
        with patch.dict(os.environ, {"CLUSTER_CI_UNIFIED_MEMORY": "1"}):
            rtx_output = "0, NVIDIA GeForce RTX 3090, 24576, 1024, 23552"
            data = parse_nvidia_smi_output(rtx_output, total_ram_gb=64.0, available_ram_gb=60.0)
            self.assertEqual(data["unified_memory"], 1)
            self.assertEqual(data["total_vram_gb"], 64.0)

        # Force discrete on GB10
        with patch.dict(os.environ, {"CLUSTER_CI_UNIFIED_MEMORY": "0"}):
            gb10_output = "0, NVIDIA GB10, 122880, 2048, 120832"
            data = parse_nvidia_smi_output(gb10_output)
            self.assertEqual(data["unified_memory"], 0)
            self.assertEqual(data["total_vram_gb"], 120.0)

    def test_cpu_detection_cgroups_v2(self):
        """Test cgroups v2 quota/period detection."""
        fake_content = "400000 100000\n"  # 4 CPUs
        with patch("os.path.exists", side_effect=lambda p: p == "/sys/fs/cgroup/cpu.max"):
            with patch("builtins.open", unittest.mock.mock_open(read_data=fake_content)):
                with patch("os.cpu_count", return_value=16):
                    cpus = get_cpu_info()
                    self.assertEqual(cpus, 4)

    def test_cpu_detection_cgroups_v1(self):
        """Test cgroups v1 quota/period detection."""
        def fake_exists(p):
            return p in ("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "/sys/fs/cgroup/cpu/cpu.cfs_period_us")

        def fake_open(p, *args, **kwargs):
            if "quota" in p:
                return unittest.mock.mock_open(read_data="200000\n")()
            elif "period" in p:
                return unittest.mock.mock_open(read_data="100000\n")()
            raise FileNotFoundError(p)

        with patch("os.path.exists", side_effect=fake_exists):
            with patch("builtins.open", side_effect=fake_open):
                with patch("os.cpu_count", return_value=16):
                    cpus = get_cpu_info()
                    self.assertEqual(cpus, 2)

    def test_cpu_detection_env_override(self):
        """Test CLUSTER_CI_CPUS environment variable override."""
        with patch.dict(os.environ, {"CLUSTER_CI_CPUS": "8"}):
            cpus = get_cpu_info()
            self.assertEqual(cpus, 8)

    def test_arch_detection(self):
        """Test architecture detection and override."""
        arch = get_arch_info()
        self.assertIsInstance(arch, str)
        self.assertGreater(len(arch), 0)

        with patch.dict(os.environ, {"CLUSTER_CI_ARCH": "aarch64"}):
            self.assertEqual(get_arch_info(), "aarch64")

    def test_registration_payload_retrocompatible_and_v3_fields(self):
        """Verify registration/heartbeat payload contains ALL existing fields
        plus ALL required v3 fields (cpus, ram_gb, vram_per_gpu, unified_memory, arch, disk_free_gb).
        """
        payload = build_registration_payload(is_startup=True)

        # 1. Existing backward-compatible fields
        existing_keys = [
            "worker_id", "hostname", "service_url", "total_ram_gb",
            "available_ram_gb", "total_storage_gb", "available_storage_gb",
            "total_vram_gb", "gpu_count", "gpu_name", "available_vram_gb",
            "is_startup"
        ]
        for key in existing_keys:
            self.assertIn(key, payload, f"Missing existing field {key}")

        self.assertTrue(payload["is_startup"])

        # 2. New v3 capacity fields
        v3_keys = [
            "cpus", "ram_gb", "vram_per_gpu", "unified_memory",
            "arch", "disk_free_gb"
        ]
        for key in v3_keys:
            self.assertIn(key, payload, f"Missing v3 capacity field {key}")

        # Check types
        self.assertIsInstance(payload["cpus"], int)
        self.assertIsInstance(payload["ram_gb"], float)
        self.assertIsInstance(payload["vram_per_gpu"], list)
        self.assertIn(payload["unified_memory"], (0, 1))
        self.assertIsInstance(payload["arch"], str)
        self.assertIsInstance(payload["disk_free_gb"], float)

    @patch("subprocess.Popen")
    @patch("src.scheduler.worker_agent.safe_docker_rm_f")
    @patch("src.scheduler.worker_agent.update_job_status")
    @patch("src.scheduler.worker_agent.purge_orphan_runners_and_containers")
    def test_parallel_mode_execution_env_vars(self, mock_purge, mock_status, mock_docker_rm, mock_popen):
        """Verify execution in parallel_mode sets CLUSTER_CI_PARALLEL_MODE=1,
        CLUSTER_CI_RUNNER_ID, CLUSTER_CI_JOB_ID, and HEADNODE_URL.
        """
        # Configure mock process
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [0]
        mock_proc.wait.return_value = 0
        mock_proc.stdout = []
        mock_popen.return_value = mock_proc

        job = {
            "job_id": "test-parallel-job-123",
            "repo": "user/repo",
            "branch": "feat/parallel",
            "ram_required_gb": 16.0,
            "parallel_mode": 1,
            "role": "executor",
            "runner_id": "runner-node-alpha"
        }

        execute_job(job)

        self.assertTrue(mock_popen.called)
        _, kwargs = mock_popen.call_args_list[0]
        env = kwargs.get("env", {})

        self.assertEqual(env.get("CLUSTER_CI_PARALLEL_MODE"), "1")
        self.assertEqual(env.get("CLUSTER_CI_RUNNER_ID"), "runner-node-alpha")
        self.assertEqual(env.get("CLUSTER_CI_JOB_ID"), "test-parallel-job-123")
        self.assertIn("HEADNODE_URL", env)
        self.assertIn("CLUSTER_CI_HEADNODE_URL", env)
        self.assertEqual(env.get("CLUSTER_CI_WORKER_ID"), WORKER_ID)
        self.assertEqual(env.get("CLUSTER_CI_ROLE"), "executor")

    @patch("subprocess.Popen")
    @patch("src.scheduler.worker_agent.safe_docker_rm_f")
    @patch("src.scheduler.worker_agent.update_job_status")
    @patch("src.scheduler.worker_agent.purge_orphan_runners_and_containers")
    def test_classic_mode_execution_unmodified(self, mock_purge, mock_status, mock_docker_rm, mock_popen):
        """Verify execution in classic mode does NOT set CLUSTER_CI_PARALLEL_MODE."""
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [0]
        mock_proc.wait.return_value = 0
        mock_proc.stdout = []
        mock_popen.return_value = mock_proc

        job = {
            "job_id": "test-classic-job-456",
            "repo": "user/repo",
            "branch": "main",
            "ram_required_gb": 8.0
        }

        execute_job(job)

        self.assertTrue(mock_popen.called)
        _, kwargs = mock_popen.call_args_list[0]
        env = kwargs.get("env", {})

        self.assertNotIn("CLUSTER_CI_PARALLEL_MODE", env)
        self.assertNotIn("CLUSTER_CI_RUNNER_ID", env)
        self.assertEqual(env.get("CLUSTER_CI_MODE"), "executor")
        self.assertEqual(env.get("JOB_ID"), "test-classic-job-456")

    @patch("src.scheduler.worker_agent._async_job_cleanup")
    def test_cancel_active_job_local_cleanup(self, mock_async_cleanup):
        """Verify POST /cancel/<job_id> stops active local process cleanly."""
        job_id = "job-to-cancel-789"
        mock_proc = MagicMock()
        mock_proc.pid = 99999

        with worker_agent.job_lock:
            worker_agent.current_job_id = job_id
            worker_agent.current_process = mock_proc

        resp = self.app.post(f"/cancel/{job_id}")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "cancelled")

        # Verify active job tracker reset immediately
        self.assertIsNone(worker_agent.current_job_id)
        self.assertIsNone(worker_agent.current_process)

        # Verify cleanup thread launched
        self.assertTrue(mock_async_cleanup.called)
        args, _ = mock_async_cleanup.call_args
        self.assertEqual(args[0], job_id)
        self.assertEqual(args[2], mock_proc)

    def test_cancel_nonexistent_job_returns_404(self):
        """Verify POST /cancel/<job_id> for unknown job with no containers returns 404."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            resp = self.app.post("/cancel/nonexistent-job")
            self.assertEqual(resp.status_code, 404)

    def test_fetch_cas_standard_md5_success(self):
        """Verify GET /fetch_cas/<md5> serves standard 32-hex CAS object in DVC cache."""
        repo_dir = os.path.join(self.test_dir, "my_repo")
        cache_dir = os.path.join(repo_dir, ".dvc", "cache", "files", "md5", "3a")
        os.makedirs(cache_dir, exist_ok=True)
        cas_file = os.path.join(cache_dir, "4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d")
        payload = b"dvc-cas-payload-binary-1234"
        with open(cas_file, "wb") as f:
            f.write(payload)

        # 1. Lowercase hash
        resp = self.app.get("/fetch_cas/3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, payload)

        # 2. Uppercase hash (case-insensitivity)
        resp_upper = self.app.get("/fetch_cas/3A4B5C6D7E8F9A0B1C2D3E4F5A6B7C8D")
        self.assertEqual(resp_upper.status_code, 200)
        self.assertEqual(resp_upper.data, payload)

    def test_fetch_cas_dir_manifest_success(self):
        """Verify GET /fetch_cas/<md5>.dir serves directory manifest."""
        nested_repo = os.path.join(self.test_dir, "owner", "repo")
        cache_dir = os.path.join(nested_repo, ".dvc", "cache", "files", "md5", "1f")
        os.makedirs(cache_dir, exist_ok=True)
        dir_file = os.path.join(cache_dir, "af98845f913f29a6961f9c7b472cca.dir")
        manifest_payload = b'[{"md5": "abc1234567890abc1234567890abcdef", "relpath": "file1.txt"}]'
        with open(dir_file, "wb") as f:
            f.write(manifest_payload)

        resp = self.app.get("/fetch_cas/1faf98845f913f29a6961f9c7b472cca.dir")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, manifest_payload)

    def test_fetch_cas_invalid_format_and_traversal_rejected(self):
        """Verify GET /fetch_cas with invalid format or traversal attempts returns 400."""
        # Non-hex characters
        resp = self.app.get("/fetch_cas/not-a-valid-hex-md5-hash-1234567")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("invalid md5 format", resp.get_json()["error"].lower())

        # Too short
        resp = self.app.get("/fetch_cas/1234")
        self.assertEqual(resp.status_code, 400)

        # Invalid extension (not .dir)
        resp = self.app.get("/fetch_cas/3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d.txt")
        self.assertEqual(resp.status_code, 400)

        # Subdirectory traversal
        resp = self.app.get("/fetch_cas/subdir/secret.txt")
        self.assertEqual(resp.status_code, 400)

        # Path traversal characters
        resp = self.app.get("/fetch_cas/..%2fsecret")
        self.assertEqual(resp.status_code, 400)

    def test_fetch_cas_not_found_returns_404(self):
        """Verify GET /fetch_cas for valid hash absent from any DVC cache returns 404."""
        resp = self.app.get("/fetch_cas/00000000000000000000000000000000")
        self.assertEqual(resp.status_code, 404)
        data = resp.get_json()
        self.assertIn("not found", data["error"].lower())

    def test_fetch_cas_local_workspace_resolution(self):
        """Verify GET /fetch_cas finds objects in _local workspaces up to 3 directory levels with local=1."""
        from src.scheduler import worker_agent
        local_repo = os.path.join(self.test_dir, "_local", "org", "project")
        cache_dir = os.path.join(local_repo, ".dvc", "cache", "files", "md5", "77")
        os.makedirs(cache_dir, exist_ok=True)
        cas_file = os.path.join(cache_dir, "8899aabbccddeeff00112233445566")
        payload = b"local-workspace-cas-object"
        with open(cas_file, "wb") as f:
            f.write(payload)

        token = "test-cluster-token"
        with patch.object(worker_agent, "CLUSTER_TOKEN", token):
            # Ordinary requests must never discover private cache objects,
            # even when the requester possesses a valid cluster token.
            url = "/fetch_cas/778899aabbccddeeff00112233445566"
            self.assertEqual(self.app.get(url).status_code, 404)
            self.assertEqual(self.app.get(url, headers={
                "Authorization": f"Bearer {token}"
            }).status_code, 404)
            self.assertEqual(self.app.get(url + "?local=1").status_code, 401)
            self.assertEqual(self.app.get(url + "?local=1", headers={
                "Authorization": "Bearer wrong-token"
            }).status_code, 401)

            resp = self.app.get(
                "/fetch_cas/778899aabbccddeeff00112233445566?local=1",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.data, payload)

    def test_cpu_affinity_failure_logs_and_returns_null_when_no_fallback(self):
        """Verify error in sched_getaffinity is logged and returns None (null) if no CPU source succeeds."""
        with patch.dict(os.environ, {}, clear=True):
            # Simulate platform having sched_getaffinity (e.g. Linux) but raising an error
            with patch.object(os, "sched_getaffinity", create=True, side_effect=OSError("Operation not permitted")):
                with patch("os.cpu_count", return_value=None):
                    with patch("os.path.exists", return_value=False):
                        with self.assertLogs("src.scheduler.worker_agent", level="WARNING") as log_ctx:
                            res = get_cpu_info()
                            self.assertIsNone(res)
                            self.assertTrue(any("Error reading CPU affinity" in m for m in log_ctx.output))

    def test_storage_info_failure_returns_null_and_logs(self):
        """Verify failure in get_storage_info logs error and sets storage fields to None (null)."""
        with patch("shutil.disk_usage", side_effect=OSError("Disk failure / I/O error")):
            with self.assertLogs("src.scheduler.worker_agent", level="ERROR") as log_ctx:
                total, avail = get_storage_info()
                self.assertIsNone(total)
                self.assertIsNone(avail)
                self.assertTrue(any("Error getting storage info" in m for m in log_ctx.output))

            caps = get_worker_capabilities()
            self.assertIsNone(caps["total_storage_gb"])
            self.assertIsNone(caps["available_storage_gb"])
            self.assertIsNone(caps["disk_free_gb"])

            payload = build_registration_payload()
            self.assertIsNone(payload["total_storage_gb"])
            self.assertIsNone(payload["available_storage_gb"])
            self.assertIsNone(payload["disk_free_gb"])

    def test_discrete_gpu_vram_parse_failure_returns_null_and_logs(self):
        """Verify discrete GPU VRAM parsing failure sets tot_gb and free_gb to None (null) and logs warning."""
        raw_output = "0, NVIDIA GeForce RTX 3090, invalid_tot_mb, 500, invalid_free_mb"
        with self.assertLogs("src.scheduler.worker_agent", level="WARNING") as log_ctx:
            data = parse_nvidia_smi_output(raw_output)
            self.assertIsNone(data["total_vram_gb"])
            self.assertIsNone(data["available_vram_gb"])
            self.assertEqual(data["vram_per_gpu"], [None])
            self.assertIsNone(data["gpu_details"][0]["total_vram_gb"])
            self.assertIsNone(data["gpu_details"][0]["free_vram_gb"])
            self.assertTrue(any("Failed to parse discrete GPU total VRAM" in m for m in log_ctx.output))
            self.assertTrue(any("Failed to parse discrete GPU free VRAM" in m for m in log_ctx.output))

    def test_parse_docker_size_units(self):
        """Verify parse_docker_size parses decimal (MB, GB) and binary (MiB, GiB) units accurately."""
        self.assertEqual(parse_docker_size("512B"), 512)
        self.assertEqual(parse_docker_size("10KB"), 10000)
        self.assertEqual(parse_docker_size("10KiB"), 10 * 1024)
        self.assertEqual(parse_docker_size("77.8MB"), int(77.8 * 1000 * 1000))
        self.assertEqual(parse_docker_size("100MiB"), 100 * 1024 * 1024)
        self.assertEqual(parse_docker_size("1.5GB"), int(1.5 * 1000 * 1000 * 1000))
        self.assertEqual(parse_docker_size("2GiB"), 2 * 1024 * 1024 * 1024)
        self.assertEqual(parse_docker_size("1TB"), 1000 * 1000 * 1000 * 1000)
        self.assertEqual(parse_docker_size("1TiB"), 1024 * 1024 * 1024 * 1024)
        self.assertIsNone(parse_docker_size("unknown"))
        self.assertIsNone(parse_docker_size(""))

    def test_get_docker_images_success(self):
        """Verify get_docker_images parses docker image ls tab-separated output into a dict with byte sizes."""
        docker_output = (
            "ubuntu:22.04\t77.8MB\n"
            "python:3.11-slim\t150MiB\n"
            "<none>:<none>\t50MB\n"
            "myrepo/app:v1.0\t1.5GB\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=docker_output)
            images = get_docker_images()
            self.assertIsInstance(images, dict)
            self.assertIn("ubuntu:22.04", images)
            self.assertEqual(images["ubuntu:22.04"], int(77.8 * 1000 * 1000))
            self.assertIn("python:3.11-slim", images)
            self.assertEqual(images["python:3.11-slim"], 150 * 1024 * 1024)
            self.assertIn("myrepo/app:v1.0", images)
            self.assertEqual(images["myrepo/app:v1.0"], int(1.5 * 1000 * 1000 * 1000))
            self.assertNotIn("<none>:<none>", images)

    def test_get_docker_images_failure_returns_null_and_logs(self):
        """Verify failure in get_docker_images returns None (null) and logs ERROR (never empty {})."""
        with patch("subprocess.run", side_effect=FileNotFoundError("docker not found")):
            with self.assertLogs("src.scheduler.worker_agent", level="ERROR") as log_ctx:
                images = get_docker_images()
                self.assertIsNone(images)
                self.assertTrue(any("Error querying docker images" in m for m in log_ctx.output))

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="permission denied")
            with self.assertLogs("src.scheduler.worker_agent", level="ERROR") as log_ctx:
                images = get_docker_images()
                self.assertIsNone(images)
                self.assertTrue(any("Error querying docker images" in m for m in log_ctx.output))

    def test_capabilities_endpoint(self):
        """Verify GET /capabilities exposes complete worker hardware, capacities, docker images, and active runners."""
        with patch("src.scheduler.worker_agent.get_docker_images", return_value={"test/img:latest": 100000}):
            with worker_agent.job_lock:
                worker_agent.active_executors["runner-t1"] = {
                    "runner_id": "runner-t1",
                    "job_id": "job-t1",
                    "repo": "user/repo",
                    "branch": "main",
                    "is_parallel": True,
                    "start_time": 1000.0,
                    "process": None
                }

            resp = self.app.get("/capabilities")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()

            # Verify key capacity and amendment fields
            self.assertIn("docker_images", data)
            self.assertEqual(data["docker_images"], {"test/img:latest": 100000})
            self.assertIn("active_runners", data)
            self.assertEqual(len(data["active_runners"]), 1)
            self.assertEqual(data["active_runners"][0]["runner_id"], "runner-t1")
            self.assertEqual(data["active_runner_count"], 1)
            self.assertTrue(data["is_busy"])
            self.assertIn("role", data)
            self.assertIn("is_headnode", data)
            self.assertIn("cpus", data)
            self.assertIn("ram_gb", data)
            self.assertIn("disk_free_gb", data)
            self.assertIn("vram_per_gpu", data)

    def test_cancel_by_runner_id_and_cancel_by_job_id(self):
        """Verify targeted cancellation: POST /cancel/runner/<runner_id> cancels only targeted runner,
        while POST /cancel/<job_id> cancels all runners associated with the job.
        """
        proc1 = MagicMock(pid=111)
        proc2 = MagicMock(pid=222)
        proc3 = MagicMock(pid=333)

        with worker_agent.job_lock:
            worker_agent.active_executors["runner-a"] = {
                "runner_id": "runner-a",
                "job_id": "job-multi-1",
                "repo": "user/r1",
                "process": proc1
            }
            worker_agent.active_executors["runner-b"] = {
                "runner_id": "runner-b",
                "job_id": "job-multi-1",
                "repo": "user/r1",
                "process": proc2
            }
            worker_agent.active_executors["runner-c"] = {
                "runner_id": "runner-c",
                "job_id": "job-other-2",
                "repo": "user/r2",
                "process": proc3
            }

        with patch("src.scheduler.worker_agent._async_job_cleanup"):
            # 1. Cancel single runner via /cancel/runner/<runner_id>
            resp = self.app.post("/cancel/runner/runner-a")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data["status"], "cancelled")
            self.assertIn("runner-a", data["cancelled_runners"])

            with worker_agent.job_lock:
                self.assertNotIn("runner-a", worker_agent.active_executors)
                self.assertIn("runner-b", worker_agent.active_executors)
                self.assertIn("runner-c", worker_agent.active_executors)

            # 2. Cancel remaining runners of job-multi-1 via /cancel/<job_id>
            resp2 = self.app.post("/cancel/job-multi-1")
            self.assertEqual(resp2.status_code, 200)
            data2 = resp2.get_json()
            self.assertEqual(data2["status"], "cancelled")
            self.assertIn("runner-b", data2["cancelled_runners"])

            with worker_agent.job_lock:
                self.assertNotIn("runner-b", worker_agent.active_executors)
                # runner-c for job-other-2 is still active
                self.assertIn("runner-c", worker_agent.active_executors)

    def test_cleanup_active_jobs_and_containers_multi_executors(self):
        """Verify cleanup_active_jobs_and_containers terminates all active executor processes
        and marks jobs as failed on headnode.
        """
        proc1 = MagicMock(pid=1001)
        proc2 = MagicMock(pid=1002)

        with worker_agent.job_lock:
            worker_agent.active_executors["runner-1"] = {
                "runner_id": "runner-1",
                "job_id": "job-1",
                "repo": "user/r1",
                "process": proc1
            }
            worker_agent.active_executors["runner-2"] = {
                "runner_id": "runner-2",
                "job_id": "job-2",
                "repo": "user/r2",
                "process": proc2
            }

        with patch("src.scheduler.worker_agent.safe_docker_rm_f") as mock_rm, \
             patch("src.scheduler.worker_agent.update_job_status") as mock_status, \
             patch("psutil.Process") as mock_psutil, \
             patch("subprocess.run") as mock_subproc:
            mock_subproc.return_value = MagicMock(returncode=0, stdout="")
            mock_ps_instance = MagicMock()
            mock_ps_instance.children.return_value = []
            mock_psutil.return_value = mock_ps_instance

            cleanup_active_jobs_and_containers()

            with worker_agent.job_lock:
                self.assertEqual(len(worker_agent.active_executors), 0)
                self.assertIsNone(worker_agent.current_job_id)
                self.assertIsNone(worker_agent.current_process)

            # Verify both jobs were reported failed
            reported_jobs = [call_args[0][0] for call_args in mock_status.call_args_list]
            self.assertIn("job-1", reported_jobs)
            self.assertIn("job-2", reported_jobs)

    def test_classic_job_workspace_concurrency_warning(self):
        """Verify classic job execution logs a warning when another classic job shares the same workspace."""
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [0]
        mock_proc.wait.return_value = 0
        mock_proc.stdout = []

        with worker_agent.job_lock:
            worker_agent.active_executors["classic-active-prev"] = {
                "runner_id": "classic-active-prev",
                "job_id": "job-prev-001",
                "repo": "user/shared-repo",
                "branch": "main",
                "is_parallel": False,
                "process": None
            }

        job2 = {
            "job_id": "job-new-002",
            "repo": "user/shared-repo",
            "branch": "feat/new",
            "ram_required_gb": 4.0
        }

        with patch("subprocess.Popen", return_value=mock_proc), \
             patch("src.scheduler.worker_agent.safe_docker_rm_f"), \
             patch("src.scheduler.worker_agent.update_job_status"), \
             patch("src.scheduler.worker_agent.purge_orphan_runners_and_containers"):
            with self.assertLogs("src.scheduler.worker_agent", level="WARNING") as log_ctx:
                execute_job(job2)
                self.assertTrue(any("Workspace concurrency constraint" in m for m in log_ctx.output))
                self.assertTrue(any("shares single workspace" in m for m in log_ctx.output))

    def test_corrupt_gpu_ids_fails_fast_with_remedy(self):
        """Verify corrupt gpu_ids fails fast with English cause + remedy and updates job status to failed."""
        job = {
            "job_id": "job-corrupt-gpu",
            "repo": "user/repo",
            "branch": "main",
            "gpu_ids": "corrupt-json-{not-an-array}"
        }
        with patch("src.scheduler.worker_agent.update_job_status") as mock_update, \
             patch("src.scheduler.worker_agent.purge_orphan_runners_and_containers"):
            with self.assertRaises(ValueError) as ctx:
                execute_job(job)
            self.assertIn("corrupt or unparseable gpu_ids", str(ctx.exception))
            self.assertIn("remedy:", str(ctx.exception))
            failed_calls = [c for c in mock_update.call_args_list if len(c[0]) > 1 and c[0][1] == "failed"]
            self.assertTrue(len(failed_calls) > 0)
            self.assertIn("remedy:", failed_calls[0][1].get("error_message", ""))



