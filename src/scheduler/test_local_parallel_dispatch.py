"""Synthetic scheduler-to-worker checks; no Docker, network or research data."""
import io
import os
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault('HEADNODE_URL', 'http://127.0.0.1:1')
with patch('threading.Thread.start'):
    import headnode_service as headnode
    import worker_agent as worker


@pytest.mark.parametrize('parallel', [0, 1])
@pytest.mark.parametrize('is_local', [0, 1])
def test_poll_preserves_mode_through_worker_startup(tmp_path, monkeypatch, parallel, is_local):
    job = dict(job_id='synthetic-job', repo='lab/synthetic', branch='synthetic',
               commit_hash='synthetic-commit', parallel_mode=parallel, is_local=is_local,
               status='assigned', home_worker='synthetic-worker')
    with patch.object(headnode, 'get_db_conn') as db:
        db.return_value.__enter__.return_value.cursor.return_value.fetchone.return_value = job
        response = headnode.app.test_client().get('/worker_poll/synthetic-worker')
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['is_local'] == is_local
    if not parallel:
        assert payload == job

    monkeypatch.setattr(worker, 'LOGS_DIR', str(tmp_path))
    monkeypatch.setattr(worker, 'REPOS_DIR', str(tmp_path))
    monkeypatch.setattr(worker, 'WORKER_ID', 'synthetic-worker')
    monkeypatch.setattr(worker, 'active_executors', {})
    for name in ['purge_orphan_runners_and_containers', 'update_job_status',
                 'safe_docker_rm_f', 'purge_ollama_vram_on_host', 'kill_dvc_viewer_processes']:
        monkeypatch.setattr(worker, name, Mock())
    monkeypatch.setenv('CLUSTER_CI_RUN_PATH', '/synthetic/runner')
    process = Mock(stdout=io.StringIO(''), poll=Mock(return_value=0), wait=Mock(return_value=0))
    with patch.object(worker.subprocess, 'Popen', return_value=process) as popen, \
            patch.object(worker.threading.Thread, 'start'):
        # A mock thread avoids background work; join needs to be mocked too.
        with patch.object(worker.threading.Thread, 'join'):
            worker.execute_job(payload)
    env = popen.call_args.kwargs['env']
    assert env['IS_LOCAL'] == str(is_local)
    if is_local:
        assert env['DVC_NO_ANALYTICS'] == '1'
    assert env['JOB_ID'] == job['job_id']
    if parallel:
        assert env['CLUSTER_CI_PARALLEL_MODE'] == '1'


def test_idle_worker_still_gets_no_job():
    with patch.object(headnode, 'get_db_conn') as db:
        db.return_value.__enter__.return_value.cursor.return_value.fetchone.return_value = None
        response = headnode.app.test_client().get('/worker_poll/synthetic-worker')
    assert response.get_json() == {'status': 'no_job'}
