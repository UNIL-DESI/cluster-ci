"""
Branch Executor for Cluster-CI v3.

Orchestrates sequential and multi-image node execution for a parallel branch worker.
Communicates with headnode via:
  - POST /api/jobs/<job_id>/next_node
  - POST /api/jobs/<job_id>/runner_heartbeat
Handles:
  - Container lifecycle per Docker image (reusing container for same image).
  - Dedicated named volumes per image: cluster-ci-home-<repo>-<image_slug>.
  - Configurable container name prefix (CLUSTER_CI_CONTAINER_PREFIX, default: cluster-job-).
  - Git synchronization using W5 dvc_git_helper (sync_before_node, push_with_retries).
  - Checking and fetching missing CAS dependencies using W6 fetch_cas_dependencies (Amendment A4).
  - Setting CUDA_VISIBLE_DEVICES per node.
  - Streaming prefixed logs: [node@machine].
"""

import argparse
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Bootstrap BASE_DIR in sys.path to ensure src.* packages are discoverable
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

from src.runner.cluster_http import cluster_urlopen

try:
    from src.runner import dvc_git_helper
except (ImportError, SystemExit):
    dvc_git_helper = None

from src.runner.fetch_cas_dependencies import (
    compute_file_md5,
    fetch_dependencies,
    normalize_worker_url,
    parse_dir_manifest,
)
from src.scheduler.artifact_registry import (
    extract_node_deps_from_dvc_lock,
    get_dag_stage_outputs,
    is_dag_stage_output,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [BranchExecutor] %(message)s",
)
logger = logging.getLogger("branch_executor")


# =====================================================================
# Fonctions Git / DVC directes (W5)
# =====================================================================

def sync_before_node(repo_dir: str, branch: str, start_commit: Optional[str] = None) -> None:
    """
    Synchronisation amont ciblée avant exécution d'un nœud via W5 dvc_git_helper.
    Restaure uniquement dvc.lock et les sorties suivies par git (outs, metrics, plots)
    depuis la pointe distante sans déplacer HEAD du code.
    """
    logger.info("Synchronisation amont ciblée via W5 sync_before_node sur %s...", branch)
    helper = dvc_git_helper
    if helper is None:
        from src.runner import dvc_git_helper as helper
    helper.sync_before_node(current_branch=branch, cwd=repo_dir, start_commit=start_commit)


def commit_and_push_node(
    repo_dir: str,
    node: str,
    branch: str,
    out_paths: Optional[List[Any]] = None,
    start_commit: Optional[str] = None,
) -> bool:
    """
    Commit et push ciblé (dvc.lock + sorties non cachées du nœud) avec W5 push_with_retries.
    Construit un commit partant de la pointe distante sans toucher à HEAD ni écraser le code.
    """
    if os.environ.get("IS_LOCAL") == "1":
        logger.info("IS_LOCAL=1: keeping node %s outputs local; skipping Git staging and push.", node)
        return True

    uncached_outs: List[str] = []
    dvc_lock = os.path.join(repo_dir, "dvc.lock")
    if os.path.exists(dvc_lock):
        uncached_outs.append("dvc.lock")

    if out_paths:
        for item in out_paths:
            p = item if isinstance(item, str) else item.get("path")
            is_cache = False if isinstance(item, str) else item.get("cache", True)
            if p and not is_cache:
                uncached_outs.append(p)

    # Extraire également les sorties non-cachées (cache: false) depuis dvc.yaml / DAG pour ce nœud
    try:
        from src.scheduler.artifact_registry import get_dag_stage_outputs
        exact_outs, dir_outs, _ = get_dag_stage_outputs(repo_dir=repo_dir)
        for p, info in exact_outs.items():
            if info.get("stage") == node and info.get("cache") is False:
                if p not in uncached_outs:
                    uncached_outs.append(p)
        for p, info in dir_outs.items():
            if info.get("stage") == node and info.get("cache") is False:
                if p not in uncached_outs:
                    uncached_outs.append(p)
    except Exception as exc:
        logger.debug("Extraction des sorties non-cachées pour %s impossible: %s", node, exc)

    files_to_sync: List[str] = []
    for p in uncached_outs:
        norm = Path(p).as_posix().lstrip("./")
        if norm not in files_to_sync:
            files_to_sync.append(norm)

    commit_msg = f"chore(ci): complete node {node} [skip ci]"

    for p in uncached_outs:
        full_p = os.path.join(repo_dir, p)
        if os.path.exists(full_p):
            subprocess.run(["git", "add", "-f", p], cwd=repo_dir, check=False)

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    if status.stdout.strip():
        subprocess.run(["git", "config", "user.name", "cluster-ci-bot"], cwd=repo_dir, check=False)
        subprocess.run(["git", "config", "user.email", "bot@cluster-ci.io"], cwd=repo_dir, check=False)
        subprocess.run(["git", "commit", "-m", commit_msg], cwd=repo_dir, capture_output=True, text=True)

    logger.info("Pushing changes for node %s via W5 push_with_retries (%s)...", node, files_to_sync)
    helper = dvc_git_helper
    if helper is None:
        from src.runner import dvc_git_helper as helper

    return helper.push_with_retries(
        current_branch=branch,
        cwd=repo_dir,
        files_to_commit=files_to_sync,
        commit_msg=commit_msg,
        start_commit=start_commit,
    )


# =====================================================================
# Abstraction Docker (Permet injection et tests unitaires complets)
# =====================================================================

class DockerRunner:
    """Encapsulation des commandes Docker pour l'exécuteur."""

    def __init__(self, docker_cmd: str = "docker"):
        self.docker_cmd = docker_cmd

    def create_volume(self, volume_name: str) -> int:
        res = subprocess.run([self.docker_cmd, "volume", "create", volume_name], capture_output=True)
        return res.returncode

    def is_oom_killed(self, container_name: str) -> bool:
        """Inspecte si le conteneur a subi un OOMKilled via docker inspect."""
        if not container_name:
            return False
        try:
            res = subprocess.run(
                [self.docker_cmd, "inspect", "-f", "{{.State.OOMKilled}}", container_name],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return res.stdout.strip().lower() == "true"
        except Exception:
            return False

    def run_container(
        self,
        image: str,
        container_name: str,
        home_volume: str,
        repo_dir: str,
        base_dir: str,
        ram_limit: float = 10.0,
        vram_limit: float = 0.0,
        env: Optional[Dict[str, str]] = None,
        user_id: int = 1000,
        group_id: int = 1000,
        resources: Optional[Dict[str, Any]] = None,
    ) -> int:
        from src.runner.host_guard import docker_resource_args

        host_profile = {
            "role": os.environ.get("CLUSTER_CI_ROLE", "worker"),
            "is_headnode": os.environ.get("IS_HEADNODE") == "1" or os.environ.get("CLUSTER_CI_ROLE") == "headnode",
            "verify_cgroup": False,
        }
        node_res = dict(resources or {})
        node_res.setdefault("ram_gb", ram_limit)
        node_res.setdefault("vram_gb", vram_limit)

        guard_args = docker_resource_args(host_profile, node_res)
        cache_prefix = 'cluster-ci-local' if (env or {}).get('IS_LOCAL') == '1' else 'cluster-ci'

        cmd = [
            self.docker_cmd, "run", "-d",
            "--init",
            "--name", container_name,
        ]
        cmd.extend(guard_args)
        cmd.extend([
            "-v", f"{repo_dir}:/workspace",
            "-w", "/workspace",
            "-v", f"{home_volume}:/home/user",
            "-v", f"{cache_prefix}-uv-cache:/home/user/.cache/uv",
            "-v", f"{cache_prefix}-pip-cache:/home/user/.cache/pip",
            "-v", f"{base_dir}:/cluster-ci:ro",
            "-v", "/etc/passwd:/etc/passwd:ro",
            "-v", "/etc/group:/etc/group:ro",
            "--ulimit", "memlock=-1",
            "--ulimit", "stack=67108864",
            "--ipc=host",
            "--user", f"{user_id}:{group_id}",
            "-e", "HOME=/home/user",
            "-e", "PYTHONUSERBASE=/home/user/.local",
            "--entrypoint", "tail",
        ])
        if env:
            for k, v in env.items():
                cmd.extend(["-e", f"{k}={v}"])
        cmd.extend([image, "-f", "/dev/null"])

        res = subprocess.run(cmd, capture_output=True, text=True)
        return res.returncode

    def stop_container(self, container_name: str, timeout: int = 10) -> int:
        res = subprocess.run(
            [self.docker_cmd, "stop", "-t", str(timeout), container_name],
            capture_output=True,
        )
        return res.returncode

    def remove_container(self, container_name: str, force: bool = True) -> int:
        args = [self.docker_cmd, "rm"]
        if force:
            args.append("-f")
        args.append(container_name)
        res = subprocess.run(args, capture_output=True)
        return res.returncode

    def exec_in_container(
        self,
        container_name: str,
        command: str,
        env: Optional[Dict[str, str]] = None,
        user: Optional[str] = None,
        stream_prefix: Optional[str] = None,
    ) -> Tuple[int, str]:
        cmd = [self.docker_cmd, "exec"]
        if user:
            cmd.extend(["--user", user])
        if env:
            for k, v in env.items():
                cmd.extend(["-e", f"{k}={v}"])
        cmd.extend([container_name, "bash", "-c", command])

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        captured = []
        if proc.stdout:
            for line in iter(proc.stdout.readline, ""):
                if not line:
                    break
                captured.append(line)
                if stream_prefix:
                    print(f"{stream_prefix} {line}", end="", flush=True)
                else:
                    print(line, end="", flush=True)
            proc.stdout.close()
        exit_code = proc.wait()
        return exit_code, "".join(captured)


# =====================================================================
# Exécuteur de Branche (BranchExecutor)
# =====================================================================

class BranchExecutor:
    """
    Exécuteur de branche côté worker pour Cluster-CI v3.
    Gère la boucle d'actions retournées par le headnode (next_node).
    """

    def __init__(
        self,
        headnode_url: str,
        job_id: str,
        runner_id: str,
        worker_id: str,
        repo_dir: str,
        target_repo: str,
        target_branch: str,
        cluster_token: Optional[str] = None,
        docker: Optional[DockerRunner] = None,
        base_dir: Optional[str] = None,
        ram_limit: float = 10.0,
        vram_limit: float = 0.0,
        poll_interval: float = 5.0,
        heartbeat_interval: float = 15.0,
        current_container: Optional[str] = None,
        current_image: Optional[str] = None,
        container_prefix: Optional[str] = None,
        start_commit: Optional[str] = None,
    ):
        self.headnode_url = headnode_url.rstrip("/") if headnode_url else ""
        self.job_id = job_id
        self.safe_job_id = re.sub(r"[^a-zA-Z0-9_-]", "-", job_id)
        self.runner_id = runner_id
        self.worker_id = worker_id
        self.repo_dir = os.path.abspath(repo_dir)
        self.target_repo = target_repo
        self.target_branch = target_branch
        self.cluster_token = cluster_token
        self.docker = docker or DockerRunner()
        self.ram_limit = ram_limit
        self.vram_limit = vram_limit
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.container_prefix = container_prefix or os.environ.get("CLUSTER_CI_CONTAINER_PREFIX", "cluster-job-")

        # Résolution du commit de départ figé (Henri - gel du code de départ)
        if start_commit:
            self.start_commit = start_commit
        else:
            helper = dvc_git_helper
            if helper is None:
                from src.runner import dvc_git_helper as helper
            self.start_commit = helper._get_start_commit(self.repo_dir)

        if self.start_commit and self.start_commit != "HEAD":
            git_dir = os.path.join(self.repo_dir, ".git")
            target_git_file = None
            if os.path.isdir(git_dir):
                target_git_file = os.path.join(git_dir, "cluster-ci-start-commit")
            elif os.path.isfile(git_dir):
                try:
                    with open(git_dir, "r", encoding="utf-8") as f:
                        content = f.read().strip()
                    if content.startswith("gitdir:"):
                        actual_git_dir = content.split(":", 1)[1].strip()
                        if not os.path.isabs(actual_git_dir):
                            actual_git_dir = os.path.normpath(os.path.join(self.repo_dir, actual_git_dir))
                        target_git_file = os.path.join(actual_git_dir, "cluster-ci-start-commit")
                except Exception:
                    pass
            if target_git_file:
                try:
                    with open(target_git_file, "w", encoding="utf-8") as f:
                        f.write(self.start_commit + "\n")
                except Exception:
                    pass



        # Résolution du dossier racine de cluster-ci
        if base_dir:
            self.base_dir = os.path.abspath(base_dir)
        elif "BASE_DIR" in os.environ:
            self.base_dir = os.path.abspath(os.environ["BASE_DIR"])
        else:
            self.base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

        # Détection sécurisée des identifiants utilisateur (fallback sur 1000 sous Windows)
        try:
            self.user_id = os.getuid()  # type: ignore[attr-defined]
            self.group_id = os.getgid()  # type: ignore[attr-defined]
        except AttributeError:
            self.user_id = 1000
            self.group_id = 1000

        # État d'exécution
        self.current_container: Optional[str] = current_container
        self.current_image: Optional[str] = current_image
        self.current_resource_args: Optional[List[str]] = None
        self.current_node: Optional[str] = None
        self.current_pythonpath: str = ""
        self.is_running = False
        self.total_containers_started = 0
        self._heartbeat_thread: Optional[threading.Thread] = None

    # -----------------------------------------------------------------
    # Gestion du Heartbeat
    # -----------------------------------------------------------------

    def _start_heartbeat(self) -> None:
        if not self.headnode_url:
            return
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_worker,
            name="BranchExecutor-Heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _stop_heartbeat(self) -> None:
        pass

    def _heartbeat_worker(self) -> None:
        while self.is_running:
            try:
                url = f"{self.headnode_url}/api/jobs/{self.job_id}/runner_heartbeat"
                payload = {
                    "runner_id": self.runner_id,
                    "worker": self.worker_id,
                    "current_node": self.current_node,
                }
                data = json.dumps(payload).encode("utf-8")
                headers = {"Content-Type": "application/json"}
                if self.cluster_token:
                    headers["Authorization"] = f"Bearer {self.cluster_token}"
                req = urllib.request.Request(url, data=data, headers=headers, method="POST")
                with cluster_urlopen(req, timeout=5) as resp:
                    pass
            except Exception as exc:
                logger.debug("Erreur heartbeat runner (non fatale): %s", exc)
            time.sleep(self.heartbeat_interval)

    # -----------------------------------------------------------------
    # Communication Headnode (next_node)
    # -----------------------------------------------------------------

    def call_next_node(
        self,
        node: Optional[str] = None,
        status: Optional[str] = None,
        duration_s: float = 0.0,
        exit_code: Optional[int] = None,
        error_message: Optional[str] = None,
        failure_reason: Optional[str] = None,
        cas_transfers: Optional[List[Dict[str, Any]]] = None,
        missing_deps: Optional[List[str]] = None,
        outputs: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.headnode_url}/api/jobs/{self.job_id}/next_node"
        payload = {
            "job_id": self.job_id,
            "runner_id": self.runner_id,
            "worker": self.worker_id,
            "node": node,
            "status": status,
            "duration_s": duration_s,
            "exit_code": exit_code,
            "error_message": error_message,
            "failure_reason": failure_reason,
            "cas_transfers": cas_transfers,
            "missing_deps": missing_deps,
            "missing_paths": missing_deps,
            "outputs": outputs,
            "current_image": self.current_image,
        }
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.cluster_token:
            headers["Authorization"] = f"Bearer {self.cluster_token}"
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with cluster_urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content)

    # -----------------------------------------------------------------
    # Gestion des Dépendances Lourdes (W6 CAS Fetcher - Amendement A4)
    # -----------------------------------------------------------------

    def _get_workers_from_headnode(self) -> List[Dict[str, Any]]:
        for endpoint in ("/list_workers", "/workers"):
            try:
                url = f"{self.headnode_url}{endpoint}"
                headers = {}
                if self.cluster_token:
                    headers["Authorization"] = f"Bearer {self.cluster_token}"
                req = urllib.request.Request(url, headers=headers)
                with cluster_urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        workers = data if isinstance(data, list) else data.get("workers", [])
                        if workers:
                            return workers
            except Exception as exc:
                logger.debug("Erreur %s: %s", endpoint, exc)
        return []

    def fetch_missing_deps(
        self,
        node: str,
        dep_paths: List[str],
        dep_sources: Optional[Dict[str, Any]] = None,
        resources: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """
        Vérifie la présence locale des dépendances requises et rapatrie les objets CAS DVC
        depuis les workers pairs via fetch_cas_dependencies (W6), multi-sources avec vérification MD5 stricte,
        SANS repli /fetch_artifact.
        Distingue rigoureusement :
          - Dépendance qui est une SORTIE (outs) d'un stage du DAG : artefact CAS.
            Si présente localement avec le bon MD5 -> rien à faire.
            Sinon -> récupération CAS depuis les pairs. Si introuvable -> échec explicite.
          - Dépendance non-sortie (fichier suivi par git, script, config, dossier source) :
            Vient du checkout git. Doit exister localement sur disque, sinon échec explicite.
            Jamais de récupération CAS pour elle ; pas de vérification de MD5 contre dvc.lock.
        """
        dvc_lock_path = os.path.join(self.repo_dir, "dvc.lock")
        exact_outs, dir_outs, pat_outs = get_dag_stage_outputs(repo_dir=self.repo_dir)

        # 1. Collecter toutes les dépendances requises pour ce nœud
        raw_paths = list(dep_paths or [])
        node_lock_deps: List[Dict[str, Any]] = []
        if os.path.isfile(dvc_lock_path):
            try:
                node_lock_deps = extract_node_deps_from_dvc_lock(dvc_lock_path, node)
            except Exception as exc:
                logger.debug("Extraction dvc.lock impossible pour %s: %s", node, exc)

        for d in node_lock_deps:
            p = d.get("path")
            if p and p not in raw_paths:
                raw_paths.append(p)

        lock_hash_by_path: Dict[str, str] = {}
        for d in node_lock_deps:
            p = d.get("path")
            m = d.get("md5")
            if p and m:
                lock_hash_by_path[p.replace("\\", "/").rstrip("/")] = m

        missing_paths: List[str] = []
        cas_deps_to_fetch: List[Dict[str, Any]] = []

        # 2. Classifier et inspecter chaque dépendance
        for dep_path in raw_paths:
            norm_p = os.path.normpath(dep_path).replace("\\", "/").rstrip("/")
            if not norm_p or norm_p == ".":
                continue

            local_path = os.path.join(self.repo_dir, norm_p)
            is_stage_out, out_info = is_dag_stage_output(norm_p, exact_outs, dir_outs, pat_outs)

            if not is_stage_out:
                # -------------------------------------------------------------
                # Branche A : Dépendance suivie par git / fichier source
                # -------------------------------------------------------------
                if not os.path.exists(local_path):
                    cause = f"Git-tracked dependency '{dep_path}' was not found in the local repository checkout."
                    remedy = f"Ensure '{dep_path}' is committed and pushed to git on branch '{self.target_branch}'."
                    logger.error(
                        "❌ Dependency '%s' for node %s is a git file that does not exist locally. Cause: %s Remedy: %s",
                        dep_path,
                        node,
                        cause,
                        remedy,
                    )
                    missing_paths.append(dep_path)
                else:
                    logger.info("Git-tracked dependency present locally: %s", dep_path)
                continue

            # -----------------------------------------------------------------
            # Branche B : Dépendance qui est une SORTIE (outs) d'un stage du DAG
            # -----------------------------------------------------------------
            out_info = out_info or {}
            is_cached = out_info.get("cache", True)

            if not is_cached:
                # -------------------------------------------------------------
                # Branche B.1 : Sortie de stage non-cachée (cache: false)
                # DVC ne stocke JAMAIS ces fichiers dans le CAS.
                # Elles sont suivies par Git et synchronisées par W5.
                # Présence locale requise, JAMAIS de CAS.
                # -------------------------------------------------------------
                if not os.path.exists(local_path):
                    prod_stage = out_info.get("stage", "upstream")
                    cause = (
                        f"Uncached stage output '{dep_path}' (cache: false) produced by stage '{prod_stage}' "
                        f"was not found in the local repository checkout."
                    )
                    remedy = (
                        f"Ensure stage '{prod_stage}' executed and committed its outputs to git "
                        f"on branch '{self.target_branch}'."
                    )
                    logger.error(
                        "❌ Dependency '%s' for node %s is an uncached stage output (cache: false) missing locally. Cause: %s Remedy: %s",
                        dep_path,
                        node,
                        cause,
                        remedy,
                    )
                    missing_paths.append(dep_path)
                else:
                    logger.info(
                        "Uncached stage output (cache: false) from stage '%s' present locally: %s",
                        out_info.get("stage"),
                        dep_path,
                    )
                continue

            # -----------------------------------------------------------------
            # Branche B.2 : Sortie de stage CACHÉE (cache: true / CAS DVC)
            # -----------------------------------------------------------------
            # Priorité absolue au hash réellement produit par le stage producteur
            # (out_info["md5"] extrait des outs du producteur dans dvc.lock après synchro W5),
            # avant le hash résiduel dans deps du consommateur (lock_hash_by_path).
            expected_md5 = out_info.get("md5")
            parent_dir_hash = out_info.get("parent_dir_hash")

            if not expected_md5 and parent_dir_hash:
                manifest_file = Path(self.repo_dir) / ".dvc" / "cache" / "files" / "md5" / parent_dir_hash[:2] / parent_dir_hash[2:]
                if manifest_file.is_file():
                    entries = parse_dir_manifest(manifest_file)
                    parent_dir = out_info.get("parent_dir", "")
                    rel_sub = norm_p[len(parent_dir):].lstrip("/")
                    for ent in entries:
                        if ent.get("relpath") == rel_sub:
                            expected_md5 = ent.get("md5")
                            break

            if not expected_md5:
                expected_md5 = lock_hash_by_path.get(norm_p)

            already_valid = False
            if expected_md5:
                clean_exp = expected_md5.lower().strip()
                if clean_exp.endswith(".dir"):
                    if os.path.isdir(local_path):
                        manifest_file = Path(self.repo_dir) / ".dvc" / "cache" / "files" / "md5" / clean_exp[:2] / clean_exp[2:]
                        if manifest_file.is_file():
                            entries = parse_dir_manifest(manifest_file)
                            if entries and all(
                                os.path.isfile(os.path.join(local_path, e.get("relpath", "")))
                                and compute_file_md5(os.path.join(local_path, e.get("relpath", ""))) == e.get("md5", "").strip().lower()
                                for e in entries
                            ):
                                already_valid = True
                else:
                    if os.path.isfile(local_path) and compute_file_md5(local_path) == clean_exp:
                        already_valid = True

            if already_valid:
                logger.info(
                    "Stage output dependency '%s' already present locally with matching md5 (%s)",
                    dep_path,
                    expected_md5,
                )
                continue

            cas_deps_to_fetch.append({
                "path": dep_path,
                "md5": expected_md5 or "",
                "parent_dir_hash": parent_dir_hash,
                "stage": out_info.get("stage"),
            })

        # 3. Récupération CAS pour les sorties de stages manquantes ou invalides
        if cas_deps_to_fetch:
            sources_map: Dict[str, List[str]] = {}
            dep_sources_dict = dep_sources or {}
            for k, urls in dep_sources_dict.items():
                url_list = urls if isinstance(urls, list) else [urls]
                for u in url_list:
                    if u:
                        try:
                            norm_u = normalize_worker_url(u)
                            sources_map.setdefault(k.strip().lower(), []).append(norm_u)
                        except Exception:
                            sources_map.setdefault(k.strip().lower(), []).append(str(u))

            workers_hint = (resources or {}).get("workers") or []
            all_workers = []
            for w in workers_hint:
                if w:
                    try:
                        all_workers.append(normalize_worker_url(w))
                    except Exception:
                        all_workers.append(str(w))

            if not all_workers and self.headnode_url:
                hw_list = self._get_workers_from_headnode()
                for w in hw_list:
                    s_url = w.get("service_url") or w.get("worker_id")
                    if s_url:
                        try:
                            norm_s = normalize_worker_url(s_url)
                        except Exception:
                            norm_s = str(s_url)
                        if norm_s not in all_workers:
                            all_workers.append(norm_s)
                        if "isipol09" in norm_s:
                            alt_url = norm_s.replace("isipol09", "130.223.73.209")
                            if alt_url not in all_workers:
                                all_workers.append(alt_url)

            for d in cas_deps_to_fetch:
                md5 = d.get("md5", "")
                if md5:
                    for w in all_workers:
                        if w not in sources_map.get(md5, []):
                            sources_map.setdefault(md5, []).append(w)
                p_hash = d.get("parent_dir_hash")
                if p_hash:
                    for w in all_workers:
                        if w not in sources_map.get(p_hash, []):
                            sources_map.setdefault(p_hash, []).append(w)

            fetch_items = list(cas_deps_to_fetch)
            existing_hashes = {d["md5"] for d in fetch_items if d.get("md5")}
            for d in cas_deps_to_fetch:
                p_hash = d.get("parent_dir_hash")
                if p_hash and p_hash not in existing_hashes:
                    fetch_items.append({
                        "path": "",
                        "md5": p_hash,
                        "is_dir": True,
                        "parent_dir_hash": None,
                    })
                    existing_hashes.add(p_hash)

            result = fetch_dependencies(
                dependencies=fetch_items,
                sources_map=sources_map,
                repo_dir=self.repo_dir,
                repo_name=self.target_repo,
                run_checkout=True,
            )

            self.last_cas_transfers = getattr(result, "transfers", [])

            if not result.success:
                missing = result.missing_deps or result.missing_hashes
                cause = result.error_message or f"status {result.status}: CAS objects not found on candidate sources or invalid md5 integrity"
                remedy = "Ensure upstream stages completed successfully on peer workers and published CAS artifacts."
                logger.error(
                    "❌ Failed to fetch CAS dependencies (W6) for node %s: %s (cause: %s, remedy: %s)",
                    node,
                    missing,
                    cause,
                    remedy,
                )
                missing_paths.extend(missing)

        return list(dict.fromkeys(missing_paths))

    # -----------------------------------------------------------------
    # Cycle de Vie des Conteneurs
    # -----------------------------------------------------------------

    def compute_docker_resource_args(self, resources: Optional[Dict[str, Any]] = None) -> List[str]:
        from src.runner.host_guard import docker_resource_args

        host_profile = {
            "role": os.environ.get("CLUSTER_CI_ROLE", "worker"),
            "is_headnode": os.environ.get("IS_HEADNODE") == "1" or os.environ.get("CLUSTER_CI_ROLE") == "headnode",
            "verify_cgroup": False,
        }
        node_res = dict(resources or {})
        node_res.setdefault("ram_gb", self.ram_limit)
        node_res.setdefault("vram_gb", self.vram_limit)
        return docker_resource_args(host_profile, node_res)

    def start_container_for_image(
        self,
        image: str,
        resources: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Démarre un conteneur dédié sur l'image spécifiée, avec named volume
        dédié cluster-ci-home-<repo>-<image_slug> pour /home/user.
        Exécute l'initialisation nécessaire avec set -euo pipefail et dvc --version.
        """
        resources = resources or {}
        ram_limit = resources.get("ram_gb", self.ram_limit)
        vram_limit = resources.get("vram_gb", self.vram_limit)

        image_slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", image).strip("-")
        repo_slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", self.target_repo).strip("-")
        local = os.environ.get('IS_LOCAL') == '1'
        if local:
            repo_slug = '_local-' + repo_slug
        home_volume = f"cluster-ci-home-{repo_slug}-{image_slug}"
        container_name = f"{self.container_prefix}{self.safe_job_id}-{image_slug}"

        logger.info(
            "Starting container %s on image %s (volume: %s)",
            container_name,
            image,
            home_volume,
        )
        self.docker.create_volume(home_volume)
        cache_prefix = 'cluster-ci-local' if local else 'cluster-ci'
        self.docker.create_volume(f"{cache_prefix}-uv-cache")
        self.docker.create_volume(f"{cache_prefix}-pip-cache")

        env = {
            "HOME": "/home/user",
            "PYTHONUSERBASE": "/home/user/.local",
            "UV_CACHE_DIR": "/home/user/.cache/uv",
            "HEADNODE_URL": self.headnode_url,
            "JOB_ID": self.job_id,
            "CLUSTER_TOKEN": self.cluster_token or "",
            "CLUSTER_CI_MODE": "executor",
            "IS_LOCAL": os.environ.get("IS_LOCAL", "0"),
        }
        if local:
            env['DVC_NO_ANALYTICS'] = '1'

        ret = self.docker.run_container(
            image=image,
            container_name=container_name,
            home_volume=home_volume,
            repo_dir=self.repo_dir,
            base_dir=self.base_dir,
            ram_limit=ram_limit,
            vram_limit=vram_limit,
            env=env,
            user_id=self.user_id,
            group_id=self.group_id,
            resources=resources,
        )
        if ret != 0:
            raise RuntimeError(f"docker run failed for {container_name} (code {ret})")

        self.current_container = container_name
        self.current_image = image
        self.current_resource_args = self.compute_docker_resource_args(resources)
        self.total_containers_started += 1

        # 1. Initialisation root : permissions, purge de l'ancien .pth, migration ~/.local/local vers user-site
        init_cmd = (
            f"chown -R {self.user_id}:{self.group_id} /home/user && "
            f'HOMEDIR=$(getent passwd {self.user_id} 2>/dev/null | cut -d: -f6) && '
            f'if [ -n "$HOMEDIR" ] && [ "$HOMEDIR" != "/home/user" ] && [ ! -e "$HOMEDIR" ]; then ln -s /home/user "$HOMEDIR"; fi && '
            "if [ -d /opt/Automodel ]; then chmod -R a+rX /opt/Automodel; fi && "
            "if [ -d /opt/uv_cache ]; then chmod -R a+rwX /opt/uv_cache; fi && "
            'SITE=$(python3 -c "import site; print(site.getsitepackages()[0])" 2>/dev/null) && '
            'if [ -n "$SITE" ] && [ -f "$SITE/cluster-ci-prefix.pth" ]; then rm -f "$SITE/cluster-ci-prefix.pth"; fi && '
            'if [ -d /home/user/.local/local ]; then '
            'for d in /home/user/.local/local/lib/python3.*/dist-packages; do '
            'if [ -d "$d" ]; then '
            'pyver=$(basename $(dirname "$d")); '
            'target="/home/user/.local/lib/$pyver/site-packages"; '
            'mkdir -p "$target"; '
            'if [ -n "$(ls -A "$d" 2>/dev/null)" ]; then cp -a "$d"/. "$target/" || { echo "Root migration failed: could not copy packages from $d to $target" >&2; exit 1; }; fi; '
            'fi; '
            'done; '
            'if [ -d /home/user/.local/local/bin ]; then '
            'mkdir -p /home/user/.local/bin; '
            'if [ -n "$(ls -A /home/user/.local/local/bin 2>/dev/null)" ]; then cp -a /home/user/.local/local/bin/. /home/user/.local/bin/ || { echo "Root migration failed: could not copy binaries from /home/user/.local/local/bin to /home/user/.local/bin" >&2; exit 1; }; fi; '
            'fi; '
            'rm -rf /home/user/.local/local; '
            f'chown -R {self.user_id}:{self.group_id} /home/user; '
            'fi'
        )
        init_code, init_out = self.docker.exec_in_container(self.current_container, init_cmd, user="root")
        if init_code != 0:
            raise RuntimeError(f"Root initialization failed for {container_name} (code {init_code}):\n{init_out}")

        # 2. Outils de base (uv, dvc via uv tool, dvc-viewer) avec set -euo pipefail
        bootstrap_cmd = (
            "set -euo pipefail\n"
            "export PATH=$PATH:/home/user/.local/bin\n"
            "if ! command -v uv >/dev/null 2>&1; then\n"
            "    python3 -m pip install uv --user --break-system-packages >/dev/null 2>&1 || (curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1)\n"
            "fi\n"
            "uv tool install --force dvc --with dvc-http\n"
            "uv tool upgrade dvc-viewer || uv tool install git+https://github.com/UNIL-DESI/dvc-viewer.git || true\n"
            "dvc --version\n"
        )
        boot_code, boot_out = self.docker.exec_in_container(self.current_container, bootstrap_cmd)
        if boot_code != 0:
            raise RuntimeError(
                f"Failed to initialize dvc/uv in container {self.current_container} (code {boot_code}):\n{boot_out}"
            )

        # 3. smart_install avec clé de cache par image (dans son volume dédié)
        if os.path.exists(os.path.join(self.repo_dir, "pyproject.toml")):
            smart_cmd = "export PATH=$PATH:/home/user/.local/bin && bash /cluster-ci/src/runner/smart_install.sh"
            smart_code, smart_out = self.docker.exec_in_container(
                self.current_container,
                smart_cmd,
                stream_prefix=f"[{image_slug}@{self.worker_id}]",
            )
            if smart_code != 0:
                raise RuntimeError(
                    f"smart_install.sh failed in container {self.current_container} (code {smart_code}):\n{smart_out}"
                )

        # 4. Vérification fail-fast des paquets installés par le projet (conditions réelles de stage)
        verify_cmd = "python3 /cluster-ci/src/runner/verify_packages.py"
        vcode, vout = self.docker.exec_in_container(
            self.current_container,
            verify_cmd,
            stream_prefix=f"[{image_slug}@{self.worker_id}]",
        )
        if vcode != 0:
            raise RuntimeError(
                f"Fail-fast package verification failed for container {self.current_container} (code {vcode}):\n{vout}"
            )

    def discover_project_pythonpath(self) -> str:
        """
        Découvre les dossiers site-packages et dist-packages installés sous
        /home/user/.local (en excluant les outils uv isolés) et les retourne
        sous forme de chaîne PYTHONPATH.
        """
        if not self.current_container:
            return ""
        find_cmd = (
            'if [ -d /home/user/.local ]; then '
            'find /home/user/.local -path "*/share/uv*" -prune -o \\( -path "*/lib/python3.*/site-packages" -o -path "*/lib/python3.*/dist-packages" \\) -print | sort -u; '
            'fi'
        )
        code, out = self.docker.exec_in_container(self.current_container, find_cmd)
        if code != 0 or not out.strip():
            return ""
        paths = [p.strip() for p in out.strip().splitlines() if p.strip()]
        return ":".join(paths)

    def stop_current_container(self) -> None:
        """Arrêt et suppression propre du conteneur en cours."""
        if self.current_container:
            logger.info("Stopping and removing container %s", self.current_container)
            self.docker.stop_container(self.current_container)
            self.docker.remove_container(self.current_container)
            self.current_container = None
            self.current_image = None
            self.current_resource_args = None
            self.current_pythonpath = ""

    def execute_node_in_container(
        self,
        node: str,
        gpu_ids_str: str = "",
        attempt: int = 1,
        env_vars: Optional[Dict[str, Any]] = None,
        resources: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, str]:
        """Exécute dvc repro -s <node> dans le conteneur avec logs préfixés."""
        if not self.current_container:
            raise RuntimeError("No active container to execute node.")

        if not self.current_pythonpath:
            self.current_pythonpath = self.discover_project_pythonpath()

        stream_prefix = f"[{node}@{self.worker_id}]"
        cmd = "export PATH=/home/user/shims:$PATH:/home/user/.local/bin"
        if self.current_pythonpath:
            cmd += f" && export PYTHONPATH=\"{self.current_pythonpath}:$PYTHONPATH\""
        cmd += f" && dvc repro -s {node}"
        env = {}
        if self.current_pythonpath:
            env["PYTHONPATH"] = self.current_pythonpath
        if gpu_ids_str != "":
            env["CUDA_VISIBLE_DEVICES"] = gpu_ids_str
        elif gpu_ids_str == "":
            env["CUDA_VISIBLE_DEVICES"] = ""

        # Contrat CLUSTER_CI_NODE_ATTEMPT et propagation des variables d'environnement
        env["CLUSTER_CI_NODE_ATTEMPT"] = str(attempt)
        if env_vars:
            for k, v in env_vars.items():
                env[str(k)] = str(v)

        # Si un fichier de secrets existe sur l'hôte, charger ses variables
        secrets_file = os.environ.get("CLUSTER_CI_SECRETS_FILE")
        if secrets_file and os.path.isfile(secrets_file):
            try:
                with open(secrets_file, "r", encoding="utf-8") as sf:
                    for line in sf:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            sk, sv = line.split("=", 1)
                            env.setdefault(sk.strip(), sv.strip())
            except Exception as e:
                logger.debug("Erreur lecture CLUSTER_CI_SECRETS_FILE: %s", e)

        # Job metadata wins over custom environment variables and secret files.
        # Otherwise a local stage could accidentally select publishing helpers
        # or send results to a different HEADNODE_URL.
        env.update({
            'IS_LOCAL': os.environ.get('IS_LOCAL', '0'),
            'CLUSTER_CI_MODE': 'executor',
            'JOB_ID': self.job_id,
            'HEADNODE_URL': self.headnode_url,
            'CLUSTER_TOKEN': self.cluster_token or '',
            'CLUSTER_CI_NODE_ATTEMPT': str(attempt),
        })
        if env['IS_LOCAL'] == '1':
            env['DVC_NO_ANALYTICS'] = '1'

        # Démarrage du watchdog mémoire hôte si supporté (Grace-Blackwell GB10 Guard)
        watchdog_proc = None
        watchdog_script = Path(__file__).parent / "gpu_watchdog.sh"
        vram_limit_gb = (resources or {}).get("vram_gb") or 0.0
        marker_file = os.environ.get("HOST_GUARD_MARKER_FILE", "host_guard_killed.marker")
        from src.runner.host_guard import purge_host_guard_marker
        purge_host_guard_marker(workspace_dir=self.repo_dir, marker_file=marker_file)

        if watchdog_script.exists() and sys.platform != "win32":
            try:
                w_env = dict(os.environ)
                w_env["HOST_GUARD_MARKER_FILE"] = marker_file
                watchdog_proc = subprocess.Popen(
                    ["bash", str(watchdog_script), self.current_container, str(int(vram_limit_gb))],
                    env=w_env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                logger.debug("Could not start gpu_watchdog.sh: %s", e)

        try:
            return self.docker.exec_in_container(
                container_name=self.current_container,
                command=cmd,
                env=env,
                stream_prefix=stream_prefix,
            )
        finally:
            if watchdog_proc and watchdog_proc.poll() is None:
                try:
                    watchdog_proc.terminate()
                    watchdog_proc.wait(timeout=2)
                except Exception:
                    try:
                        watchdog_proc.kill()
                    except Exception:
                        pass

    # -----------------------------------------------------------------
    # Boucle Principale de l'Exécuteur
    # -----------------------------------------------------------------

    def run(self) -> int:
        """Boucle principale d'ordonnancement de l'exécuteur."""
        self.is_running = True
        self._start_heartbeat()
        from src.runner.host_guard import purge_host_guard_marker
        purge_host_guard_marker(workspace_dir=self.repo_dir)

        node_for_req: Optional[str] = None
        status_for_req: Optional[str] = None
        duration_for_req: float = 0.0
        exit_code_for_req: Optional[int] = None
        error_message_for_req: Optional[str] = None
        failure_reason_for_req: Optional[str] = None
        cas_transfers_for_req: Optional[List[Dict[str, Any]]] = None
        missing_deps_for_req: Optional[List[str]] = None
        outputs_for_req: Optional[List[Dict[str, Any]]] = None

        try:
            while self.is_running:
                resp = self.call_next_node(
                    node=node_for_req,
                    status=status_for_req,
                    duration_s=duration_for_req,
                    exit_code=exit_code_for_req,
                    error_message=error_message_for_req,
                    failure_reason=failure_reason_for_req,
                    cas_transfers=cas_transfers_for_req,
                    missing_deps=missing_deps_for_req,
                    outputs=outputs_for_req,
                )
                outputs_for_req = None
                failure_reason_for_req = None
                cas_transfers_for_req = None

                action = resp.get("action")
                target_node = resp.get("node")
                image = resp.get("image")
                resources = resp.get("resources") or {}
                gpu_ids = resp.get("gpu_ids")
                if gpu_ids is not None:
                    resources["gpu_ids"] = gpu_ids
                dep_paths = resp.get("dep_paths") or resources.get("dep_paths") or []
                dep_sources = resp.get("dep_sources")
                out_paths = resp.get("out_paths") or resources.get("out_paths") or []

                logger.info(
                    "Headnode returned action='%s' (node='%s', image='%s')",
                    action,
                    target_node,
                    image,
                )

                if action == "wait":
                    node_for_req = None
                    status_for_req = None
                    duration_for_req = 0.0
                    exit_code_for_req = None
                    error_message_for_req = None
                    missing_deps_for_req = None
                    time.sleep(self.poll_interval)
                    continue

                elif action == "yield":
                    logger.info("Machine yielded (yield). Clean shutdown.")
                    self.stop_current_container()
                    return 0

                elif action == "finish":
                    logger.info("Job finished (finish). Clean shutdown.")
                    self.stop_current_container()
                    return 0

                elif action == "switch_image":
                    self.stop_current_container()
                    if image:
                        self.start_container_for_image(image, resources)
                    if not target_node:
                        node_for_req = None
                        status_for_req = None
                        duration_for_req = 0.0
                        exit_code_for_req = None
                        error_message_for_req = None
                        missing_deps_for_req = None
                        continue

                elif action == "run":
                    # Ne réutiliser le conteneur que si l'image ET TOUS les arguments de ressources
                    # (memory, cpus, gpu_ids, shm, cgroup-parent) sont strictement identiques
                    target_resource_args = self.compute_docker_resource_args(resources)
                    resource_args_changed = (
                        self.current_resource_args is None
                        or self.current_resource_args != target_resource_args
                    )
                    image_changed = bool(image and self.current_image != image)

                    if not self.current_container or image_changed or resource_args_changed:
                        if self.current_container:
                            logger.info(
                                "Recréation du conteneur pour le nœud %s : image_changed=%s, resource_args_changed=%s",
                                target_node, image_changed, resource_args_changed
                            )
                            self.stop_current_container()
                        try:
                            self.start_container_for_image(image or self.current_image, resources)
                        except Exception as err:
                            logger.error("Échec du démarrage ou de la vérification du conteneur pour le nœud %s: %s", target_node, err)
                            err_str = str(err)
                            if "package verification" in err_str.lower() or "fail-fast" in err_str.lower():
                                fail_reason = "PackageVerificationFailed"
                            elif "smart_install" in err_str.lower():
                                fail_reason = "SmartInstallFailed"
                            else:
                                fail_reason = "ContainerStartFailed"
                            node_for_req = target_node
                            status_for_req = "failed"
                            duration_for_req = 0.0
                            exit_code_for_req = 1
                            error_message_for_req = err_str
                            failure_reason_for_req = fail_reason
                            self.current_node = None
                            continue
                    if not target_node:
                        node_for_req = None
                        status_for_req = None
                        duration_for_req = 0.0
                        exit_code_for_req = None
                        error_message_for_req = None
                        missing_deps_for_req = None
                        continue

                else:
                    logger.warning("Unknown action from headnode: %s", action)
                    time.sleep(self.poll_interval)
                    continue

                # --- Exécution du nœud DVC ---
                self.current_node = target_node
                node_start_time = time.time()

                # 1. Sync amont avant exécution via W5
                sync_before_node(repo_dir=self.repo_dir, branch=self.target_branch, start_commit=self.start_commit)

                # 1.bis Assainissement du workspace (Bug 9)
                from src.runner.workspace_sanitizer import sanitize_workspace, WorkspaceSanitizerError
                try:
                    sanitize_workspace(self.repo_dir)
                except WorkspaceSanitizerError as err:
                    logger.error("Workspace sanitation failed before node %s: %s", target_node, err)
                    node_for_req = target_node
                    status_for_req = "failed"
                    duration_for_req = time.time() - node_start_time
                    exit_code_for_req = 1
                    error_message_for_req = str(err)
                    failure_reason_for_req = "WorkspaceSanitizerError"
                    self.current_node = None
                    continue

                # 2. Vérification et rapatriement des dep_paths via W6
                missing = self.fetch_missing_deps(
                    node=target_node,
                    dep_paths=dep_paths,
                    dep_sources=dep_sources,
                    resources=resources,
                )
                if missing:
                    logger.warning(
                        "Dependencies not found for %s: %s (Amendment A4)",
                        target_node,
                        missing,
                    )
                    node_for_req = target_node
                    status_for_req = "missing_deps"
                    duration_for_req = time.time() - node_start_time
                    exit_code_for_req = 1
                    error_message_for_req = f"Missing dependencies: {missing}"
                    missing_deps_for_req = missing
                    self.current_node = None
                    continue

                # 3. Exécution dvc repro -s <node>
                gpu_ids_str = (
                    ",".join(map(str, gpu_ids)) if gpu_ids is not None else ""
                )
                attempt = resp.get("attempt") or 1
                node_env_vars = resp.get("env_vars") or {}

                node_exit_code, _ = self.execute_node_in_container(
                    node=target_node,
                    gpu_ids_str=gpu_ids_str,
                    attempt=attempt,
                    env_vars=node_env_vars,
                    resources=resources,
                )
                node_duration = time.time() - node_start_time

                if node_exit_code == 0:
                    # 4. Commit + push de dvc.lock et sorties non cachées via W5
                    commit_and_push_node(
                        repo_dir=self.repo_dir,
                        node=target_node,
                        branch=self.target_branch,
                        out_paths=out_paths,
                        start_commit=self.start_commit,
                    )
                    node_for_req = target_node
                    status_for_req = "done"
                    duration_for_req = node_duration
                    exit_code_for_req = 0
                    error_message_for_req = None
                    failure_reason_for_req = None
                    cas_transfers_for_req = getattr(self, "last_cas_transfers", None)
                    self.last_cas_transfers = []
                    missing_deps_for_req = None
                    outputs_for_req = []
                    dvc_lock_path = os.path.join(self.repo_dir, "dvc.lock")
                    if os.path.isfile(dvc_lock_path):
                        try:
                            from src.scheduler.artifact_registry import extract_node_outputs_from_dvc_lock
                            outputs_for_req = extract_node_outputs_from_dvc_lock(dvc_lock_path, target_node, repo_dir=self.repo_dir)
                        except Exception as e:
                            logger.debug("Extraction outputs dvc.lock impossible: %s", e)
                else:
                    logger.error("Node %s failed (code %d)", target_node, node_exit_code)
                    failure_reason_for_req = None
                    marker_file = "host_guard_killed.marker"
                    marker_in_repo = os.path.join(self.repo_dir, "host_guard_killed.marker")
                    found_marker = None
                    if os.path.exists(marker_file):
                        found_marker = marker_file
                    elif os.path.exists(marker_in_repo):
                        found_marker = marker_in_repo

                    if node_exit_code == 137 and found_marker:
                        try:
                            with open(found_marker, "r", encoding="utf-8") as mf:
                                mdata = json.load(mf)
                            err_msg = f"HostMemoryPressureExceeded: {mdata.get('reason')} (used: {mdata.get('used_gb')}GB, available: {mdata.get('available_gb')}GB)"
                        except Exception:
                            err_msg = "HostMemoryPressureExceeded: Container killed by host memory guard."
                        try:
                            os.remove(found_marker)
                        except Exception:
                            pass
                        logger.error("❌ %s", err_msg)
                        error_message_for_req = err_msg
                        failure_reason_for_req = "HostMemoryPressureExceeded"
                    else:
                        oom_killed = self.docker.is_oom_killed(self.current_container)
                        if oom_killed:
                            node_ram = (resources or {}).get("ram_gb", self.ram_limit)
                            err_msg = (
                                f"node {target_node} killed due to out-of-memory (ram_gb={node_ram} GB ceiling); "
                                f"remedy: increase meta.cluster.ram_gb of stage {target_node} in dvc.yaml"
                            )
                            logger.error("❌ %s", err_msg)
                            error_message_for_req = err_msg
                        else:
                            error_message_for_req = (
                                f"Node {target_node} failed with exit code {node_exit_code}"
                            )
                    node_for_req = target_node
                    status_for_req = "failed"
                    duration_for_req = node_duration
                    exit_code_for_req = node_exit_code
                    cas_transfers_for_req = getattr(self, "last_cas_transfers", None)
                    self.last_cas_transfers = []
                    missing_deps_for_req = None

                self.current_node = None

            return 0

        finally:
            self.is_running = False
            self._stop_heartbeat()
            self.stop_current_container()


# =====================================================================
# CLI Entrypoint
# =====================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Cluster-CI v3 Branch Executor")
    parser.add_argument("--headnode-url", default=os.environ.get("HEADNODE_URL", "http://localhost:5000"))
    parser.add_argument("--job-id", default=os.environ.get("CLUSTER_CI_JOB_ID", os.environ.get("JOB_ID", "default-job")))
    parser.add_argument("--runner-id", default=os.environ.get("CLUSTER_CI_RUNNER_ID", os.environ.get("RUNNER_ID", f"runner-{socket.gethostname()}-{os.getpid()}")))
    parser.add_argument("--worker-id", default=os.environ.get("WORKER_ID", socket.gethostname()))
    parser.add_argument("--repo-dir", default=os.getcwd())
    parser.add_argument("--target-repo", default=os.environ.get("TARGET_REPO", "repo"))
    parser.add_argument("--target-branch", default=os.environ.get("TARGET_BRANCH", "main"))
    parser.add_argument("--cluster-token", default=os.environ.get("CLUSTER_TOKEN", None))
    parser.add_argument("--ram-limit", type=float, default=float(os.environ.get("RAM_LIMIT", 10.0)))
    parser.add_argument("--vram-limit", type=float, default=float(os.environ.get("VRAM_LIMIT", 0.0)))
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--heartbeat-interval", type=float, default=15.0)
    parser.add_argument("--current-container", default=None)
    parser.add_argument("--current-image", default=None)
    parser.add_argument("--container-prefix", default=None, help="Prefix for container names (default: CLUSTER_CI_CONTAINER_PREFIX or cluster-job-)")
    parser.add_argument("--start-commit", default=os.environ.get("CALLER_COMMIT_SHA", os.environ.get("JOB_START_COMMIT", None)), help="Frozen starting commit hash of the job")

    args = parser.parse_args()

    executor = BranchExecutor(
        headnode_url=args.headnode_url,
        job_id=args.job_id,
        runner_id=args.runner_id,
        worker_id=args.worker_id,
        repo_dir=args.repo_dir,
        target_repo=args.target_repo,
        target_branch=args.target_branch,
        cluster_token=args.cluster_token,
        ram_limit=args.ram_limit,
        vram_limit=args.vram_limit,
        poll_interval=args.poll_interval,
        heartbeat_interval=args.heartbeat_interval,
        current_container=args.current_container,
        current_image=args.current_image,
        container_prefix=args.container_prefix,
        start_commit=args.start_commit,
    )

    exit_code = executor.run()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
