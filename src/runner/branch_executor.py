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
from typing import Any, Dict, List, Optional, Tuple

try:
    from src.runner import dvc_git_helper
except (ImportError, SystemExit):
    dvc_git_helper = None

from src.runner.fetch_cas_dependencies import fetch_dependencies
from src.scheduler.artifact_registry import extract_node_deps_from_dvc_lock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [BranchExecutor] %(message)s",
)
logger = logging.getLogger("branch_executor")


# =====================================================================
# Fonctions Git / DVC directes (W5)
# =====================================================================

def sync_before_node(repo_dir: str, branch: str) -> None:
    """
    Synchronisation amont avant exécution d'un nœud via W5 dvc_git_helper.
    Installe le pilote de fusion dvc.lock et effectue git pull --rebase origin <branch>.
    """
    logger.info("Synchronisation amont via W5 sync_before_node sur %s...", branch)
    if dvc_git_helper is not None:
        dvc_git_helper.sync_before_node(current_branch=branch, cwd=repo_dir)
    else:
        from src.runner import dvc_git_helper as dgh
        dgh.sync_before_node(current_branch=branch, cwd=repo_dir)


def commit_and_push_node(
    repo_dir: str,
    node: str,
    branch: str,
    out_paths: Optional[List[Any]] = None,
) -> bool:
    """
    Commit et push (dvc.lock + sorties non cachées du nœud) avec W5 push_with_retries.
    """
    dvc_lock = os.path.join(repo_dir, "dvc.lock")
    if os.path.exists(dvc_lock):
        subprocess.run(["git", "add", "dvc.lock"], cwd=repo_dir, check=False)

    if out_paths:
        for item in out_paths:
            p = item if isinstance(item, str) else item.get("path")
            is_cache = False if isinstance(item, str) else item.get("cache", True)
            if p and not is_cache:
                full_p = os.path.join(repo_dir, p)
                if os.path.exists(full_p):
                    subprocess.run(["git", "add", "-f", p], cwd=repo_dir, check=False)

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    if not status.stdout.strip():
        logger.info("Aucun fichier modifié à commiter pour le nœud %s.", node)
        return True

    subprocess.run(["git", "config", "user.name", "cluster-ci-bot"], cwd=repo_dir, check=False)
    subprocess.run(["git", "config", "user.email", "bot@cluster-ci.io"], cwd=repo_dir, check=False)
    commit_msg = f"chore(ci): complete node {node} [skip ci]"
    commit_res = subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    if commit_res.returncode != 0:
        logger.warning("git commit a échoué: %s", commit_res.stderr)
        return False

    logger.info("Push des modifications pour le nœud %s via W5 push_with_retries...", node)
    if dvc_git_helper is not None:
        dvc_git_helper.push_with_retries(current_branch=branch, cwd=repo_dir)
    else:
        from src.runner import dvc_git_helper as dgh
        dgh.push_with_retries(current_branch=branch, cwd=repo_dir)
    return True


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
    ) -> int:
        cmd = [
            self.docker_cmd, "run", "-d",
            "--init",
            "--name", container_name,
            f"--memory={int(ram_limit)}g",
            f"--memory-swap={int(ram_limit)}g",
            "--gpus", "all",
            "-v", f"{repo_dir}:/workspace",
            "-w", "/workspace",
            "-v", f"{home_volume}:/home/user",
            "-v", f"{base_dir}:/cluster-ci:ro",
            "-v", "/etc/passwd:/etc/passwd:ro",
            "-v", "/etc/group:/etc/group:ro",
            "--ulimit", "memlock=-1",
            "--ulimit", "stack=67108864",
            "--ipc=host",
            "--user", f"{user_id}:{group_id}",
            "--entrypoint", "tail",
        ]
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
        self.current_node: Optional[str] = None
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
                with urllib.request.urlopen(req, timeout=5) as resp:
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
        missing_deps: Optional[List[str]] = None,
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
            "missing_deps": missing_deps,
            "current_image": self.current_image,
        }
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.cluster_token:
            headers["Authorization"] = f"Bearer {self.cluster_token}"
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content)

    # -----------------------------------------------------------------
    # Gestion des Dépendances Lourdes (W6 CAS Fetcher - Amendement A4)
    # -----------------------------------------------------------------

    def _get_workers_from_headnode(self) -> List[Dict[str, Any]]:
        try:
            url = f"{self.headnode_url}/list_workers"
            headers = {}
            if self.cluster_token:
                headers["Authorization"] = f"Bearer {self.cluster_token}"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.debug("Erreur list_workers: %s", exc)
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
        En cas d'échec, consigne une erreur explicite avec la cause et retourne les dépendances manquantes.
        """
        dvc_lock_path = os.path.join(self.repo_dir, "dvc.lock")
        deps: List[Dict[str, Any]] = []
        if os.path.isfile(dvc_lock_path):
            try:
                deps = extract_node_deps_from_dvc_lock(dvc_lock_path, node)
            except Exception as exc:
                logger.debug("Extraction dvc.lock impossible pour %s: %s", node, exc)

        cas_deps = [d for d in deps if d.get("md5")]
        if cas_deps:
            # Sources map: {md5: [urls]}
            sources_map: Dict[str, List[str]] = {}
            dep_sources_dict = dep_sources or {}
            for k, urls in dep_sources_dict.items():
                url_list = urls if isinstance(urls, list) else [urls]
                sources_map.setdefault(k.strip().lower(), []).extend(url_list)

            workers_hint = (resources or {}).get("workers") or []
            all_workers = list(workers_hint)
            if not all_workers and self.headnode_url:
                hw_list = self._get_workers_from_headnode()
                for w in hw_list:
                    s_url = w.get("service_url")
                    if s_url and s_url not in all_workers:
                        all_workers.append(s_url)

            for d in cas_deps:
                md5 = d.get("md5", "")
                if md5:
                    for w in all_workers:
                        if w not in sources_map.get(md5, []):
                            sources_map.setdefault(md5, []).append(w)

            result = fetch_dependencies(
                dependencies=cas_deps,
                sources_map=sources_map,
                repo_dir=self.repo_dir,
                repo_name=self.target_repo,
                run_checkout=True,
            )

            if not result.success:
                missing = result.missing_deps or result.missing_hashes
                cause = result.error_message or f"statut {result.status}: objets CAS introuvables sur les sources candidates ou intégrité md5 invalide"
                logger.error(
                    "❌ Échec récupération des dépendances CAS (W6) pour le nœud %s: %s (cause: %s)",
                    node,
                    missing,
                    cause,
                )
                return missing

        # Vérification des chemins locaux sans repli /fetch_artifact (W6)
        all_paths = list(dep_paths or [])
        for d in deps:
            p = d.get("path")
            if p and p not in all_paths:
                all_paths.append(p)

        if not all_paths and not cas_deps:
            return []

        missing_paths: List[str] = []
        for path in all_paths:
            local_path = os.path.join(self.repo_dir, path)
            if os.path.exists(local_path) and (
                os.path.isdir(local_path) or os.path.getsize(local_path) > 0
            ):
                logger.info("Dépendance déjà présente localement : %s", path)
                continue

            logger.error(
                "❌ Dépendance %s introuvable localement pour le nœud %s (repli /fetch_artifact interdit sous W6 CAS)",
                path,
                node,
            )
            missing_paths.append(path)

        return missing_paths

    # -----------------------------------------------------------------
    # Cycle de Vie des Conteneurs
    # -----------------------------------------------------------------

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
        home_volume = f"cluster-ci-home-{repo_slug}-{image_slug}"
        container_name = f"{self.container_prefix}{self.safe_job_id}-{image_slug}"

        logger.info(
            "Démarrage du conteneur %s sur l'image %s (volume: %s)",
            container_name,
            image,
            home_volume,
        )
        self.docker.create_volume(home_volume)

        env = {
            "HOME": "/home/user",
            "UV_CACHE_DIR": "/home/user/.cache/uv",
            "HEADNODE_URL": self.headnode_url,
            "JOB_ID": self.job_id,
            "CLUSTER_TOKEN": self.cluster_token or "",
            "CLUSTER_CI_MODE": "executor",
        }

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
        )
        if ret != 0:
            raise RuntimeError(f"Échec docker run pour {container_name} (code {ret})")

        self.current_container = container_name
        self.current_image = image
        self.total_containers_started += 1

        # 1. Initialisation root
        init_cmd = (
            f"chown -R {self.user_id}:{self.group_id} /home/user && "
            f"chown -R {self.user_id}:{self.group_id} /workspace && "
            'SITE=$(python3 -c "import site; print(site.getsitepackages()[0])" 2>/dev/null) && '
            '[ -n "$SITE" ] && find /home/user -path "*/lib/python3.*/site-packages" -o -path "*/lib/python3.*/dist-packages" 2>/dev/null > "$SITE/cluster-ci-prefix.pth" || true'
        )
        self.docker.exec_in_container(self.current_container, init_cmd, user="root")

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
                f"Échec initialisation dvc/uv dans le conteneur {self.current_container} (code {boot_code}):\n{boot_out}"
            )

        # 3. smart_install avec clé de cache par image (dans son volume dédié)
        if os.path.exists(os.path.join(self.repo_dir, "pyproject.toml")):
            smart_cmd = "export PATH=$PATH:/home/user/.local/bin && bash /cluster-ci/src/runner/smart_install.sh"
            self.docker.exec_in_container(
                self.current_container,
                smart_cmd,
                stream_prefix=f"[{image_slug}@{self.worker_id}]",
            )

    def stop_current_container(self) -> None:
        """Arrêt et suppression propre du conteneur en cours."""
        if self.current_container:
            logger.info("Arrêt et suppression du conteneur %s", self.current_container)
            self.docker.stop_container(self.current_container)
            self.docker.remove_container(self.current_container)
            self.current_container = None
            self.current_image = None

    def execute_node_in_container(
        self,
        node: str,
        gpu_ids_str: str = "",
    ) -> Tuple[int, str]:
        """Exécute dvc repro -s <node> dans le conteneur avec logs préfixés."""
        if not self.current_container:
            raise RuntimeError("Aucun conteneur actif pour exécuter le nœud.")

        stream_prefix = f"[{node}@{self.worker_id}]"
        cmd = f"export PATH=/home/user/shims:$PATH:/home/user/.local/bin && dvc repro -s {node}"
        env = {}
        if gpu_ids_str != "":
            env["CUDA_VISIBLE_DEVICES"] = gpu_ids_str
        elif gpu_ids_str == "":
            env["CUDA_VISIBLE_DEVICES"] = ""

        return self.docker.exec_in_container(
            container_name=self.current_container,
            command=cmd,
            env=env,
            stream_prefix=stream_prefix,
        )

    # -----------------------------------------------------------------
    # Boucle Principale de l'Exécuteur
    # -----------------------------------------------------------------

    def run(self) -> int:
        """Boucle principale d'ordonnancement de l'exécuteur."""
        self.is_running = True
        self._start_heartbeat()

        node_for_req: Optional[str] = None
        status_for_req: Optional[str] = None
        duration_for_req: float = 0.0
        exit_code_for_req: Optional[int] = None
        error_message_for_req: Optional[str] = None
        missing_deps_for_req: Optional[List[str]] = None

        try:
            while self.is_running:
                resp = self.call_next_node(
                    node=node_for_req,
                    status=status_for_req,
                    duration_s=duration_for_req,
                    exit_code=exit_code_for_req,
                    error_message=error_message_for_req,
                    missing_deps=missing_deps_for_req,
                )

                action = resp.get("action")
                target_node = resp.get("node")
                image = resp.get("image")
                resources = resp.get("resources") or {}
                gpu_ids = resp.get("gpu_ids")
                dep_paths = resp.get("dep_paths") or resources.get("dep_paths") or []
                dep_sources = resp.get("dep_sources")
                out_paths = resp.get("out_paths") or resources.get("out_paths") or []

                logger.info(
                    "Headnode a répondu action='%s' (nœud='%s', image='%s')",
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
                    logger.info("Machine cédée (yield). Arrêt propre.")
                    self.stop_current_container()
                    return 0

                elif action == "finish":
                    logger.info("Job terminé (finish). Arrêt propre.")
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
                    # Si aucun conteneur n'est actif, ou si l'image courante diffère
                    if not self.current_container or (image and self.current_image != image):
                        if self.current_container:
                            self.stop_current_container()
                        self.start_container_for_image(image or self.current_image, resources)
                    if not target_node:
                        node_for_req = None
                        status_for_req = None
                        duration_for_req = 0.0
                        exit_code_for_req = None
                        error_message_for_req = None
                        missing_deps_for_req = None
                        continue

                else:
                    logger.warning("Action inconnue du headnode: %s", action)
                    time.sleep(self.poll_interval)
                    continue

                # --- Exécution du nœud DVC ---
                self.current_node = target_node
                node_start_time = time.time()

                # 1. Sync amont avant exécution via W5
                sync_before_node(repo_dir=self.repo_dir, branch=self.target_branch)

                # 2. Vérification et rapatriement des dep_paths via W6
                missing = self.fetch_missing_deps(
                    node=target_node,
                    dep_paths=dep_paths,
                    dep_sources=dep_sources,
                    resources=resources,
                )
                if missing:
                    logger.warning(
                        "Dépendances introuvables pour %s: %s (Amendement A4)",
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
                node_exit_code, _ = self.execute_node_in_container(
                    node=target_node,
                    gpu_ids_str=gpu_ids_str,
                )
                node_duration = time.time() - node_start_time

                if node_exit_code == 0:
                    # 4. Commit + push de dvc.lock et sorties non cachées via W5
                    commit_and_push_node(
                        repo_dir=self.repo_dir,
                        node=target_node,
                        branch=self.target_branch,
                        out_paths=out_paths,
                    )
                    node_for_req = target_node
                    status_for_req = "done"
                    duration_for_req = node_duration
                    exit_code_for_req = 0
                    error_message_for_req = None
                    missing_deps_for_req = None
                else:
                    logger.error("Le nœud %s a échoué (code %d)", target_node, node_exit_code)
                    node_for_req = target_node
                    status_for_req = "failed"
                    duration_for_req = node_duration
                    exit_code_for_req = node_exit_code
                    error_message_for_req = (
                        f"Node {target_node} failed with exit code {node_exit_code}"
                    )
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
    )

    exit_code = executor.run()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
