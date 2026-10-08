"""
Tests unitaires ciblés pour le Correctif 2 : Watchdog self-healing dans worker_agent.py (Point C).
Vérifie que :
1. Un statut headnode terminal 'completed' ne déclenche PAS de SIGKILL (parent.kill())
   ni d'interruption intempestive du runner alors qu'il finalise proprement.
2. Un statut headnode anormal ou d'annulation ('cancelled') déclenche
   l'auto-destruction immédiate et parent.kill().
"""

import io
from unittest.mock import MagicMock, patch
import src.scheduler.worker_agent as worker_agent


def _make_ticker(start=100.0, step=15.0):
    t = [start]
    def ticker():
        t[0] += step
        return t[0]
    return ticker


def test_watchdog_does_not_kill_process_on_completed_status():
    """
    Prouve qu'un job dont le statut headnode passe à 'completed' ne subit pas
    de SIGKILL intempestif via parent.kill().
    """
    job = {
        "job_id": "test-job-watchdog-completed",
        "repo": "user/test-repo",
        "branch": "main",
        "ram_limit_gb": 4.0,
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": "completed"}

    mock_proc = MagicMock()
    mock_proc.pid = 99999
    mock_proc.stdout = io.StringIO("")
    # poll() renvoie None aux 2 premiers tours, puis 0 pour terminer proprement
    poll_calls = [0]
    def fake_poll():
        poll_calls[0] += 1
        return None if poll_calls[0] <= 2 else 0

    mock_proc.poll.side_effect = fake_poll
    mock_proc.wait.return_value = 0

    mock_psutil_parent = MagicMock()
    mock_psutil_parent.children.return_value = []

    with patch("subprocess.Popen", return_value=mock_proc), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")), \
         patch("requests.get", return_value=mock_resp), \
         patch("psutil.Process", return_value=mock_psutil_parent), \
         patch("time.time", side_effect=_make_ticker()), \
         patch("time.sleep", return_value=None), \
         patch("src.scheduler.worker_agent.update_job_status"), \
         patch("src.scheduler.worker_agent.purge_orphan_runners_and_containers"), \
         patch("src.scheduler.worker_agent.safe_docker_rm_f"), \
         patch("src.scheduler.worker_agent.purge_ollama_vram_on_host"), \
         patch("src.scheduler.worker_agent.kill_dvc_viewer_processes"):

        worker_agent.execute_job(job)

        # Assertion clé : parent.kill() NE DOIT PAS être appelé pour un job 'completed'
        assert not mock_psutil_parent.kill.called, (
            "Le watchdog a déclenché parent.kill() sur un job marqué 'completed' !"
        )


def test_watchdog_triggers_kill_on_cancelled_status():
    """
    Vérifie qu'un job dont le statut headnode devient 'cancelled' déclenche
    bien la destruction physique et l'interruption locale via parent.kill().
    """
    job = {
        "job_id": "test-job-watchdog-cancelled",
        "repo": "user/test-repo",
        "branch": "main",
        "ram_limit_gb": 4.0,
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": "cancelled"}

    mock_proc = MagicMock()
    mock_proc.pid = 88888
    mock_proc.stdout = io.StringIO("")
    # Si le watchdog ne fait pas break, poll() finira par rendre la main pour éviter tout blocage
    poll_calls = [0]
    def fake_poll():
        poll_calls[0] += 1
        return None if poll_calls[0] <= 5 else -9

    mock_proc.poll.side_effect = fake_poll
    mock_proc.wait.return_value = -9

    mock_psutil_parent = MagicMock()
    mock_psutil_parent.children.return_value = []

    with patch("subprocess.Popen", return_value=mock_proc), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")), \
         patch("requests.get", return_value=mock_resp), \
         patch("psutil.Process", return_value=mock_psutil_parent), \
         patch("time.time", side_effect=_make_ticker()), \
         patch("time.sleep", return_value=None), \
         patch("src.scheduler.worker_agent.update_job_status"), \
         patch("src.scheduler.worker_agent.purge_orphan_runners_and_containers"), \
         patch("src.scheduler.worker_agent.safe_docker_rm_f"), \
         patch("src.scheduler.worker_agent.purge_ollama_vram_on_host"), \
         patch("src.scheduler.worker_agent.kill_dvc_viewer_processes"):

        worker_agent.execute_job(job)

        # Assertion clé : parent.kill() DOIT être appelé pour un job 'cancelled'
        assert mock_psutil_parent.kill.called, (
            "Le watchdog doit détruire physiquement le process pour un job 'cancelled' !"
        )
