import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock, patch

import pytest

from src.scheduler import persistence, scheduler_loop as scheduler, worker_agent as worker
from src.scheduler import headnode_service as head

TOKEN = 'synthetic-token'
AUTH = {'Authorization': 'Bearer ' + TOKEN}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('CLUSTER_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setenv('CLUSTER_TOKEN', TOKEN)
    monkeypatch.delenv('DATABASE_PATH', raising=False)
    monkeypatch.setattr(head, 'CLUSTER_TOKEN', TOKEN)
    monkeypatch.setattr(worker, 'CLUSTER_TOKEN', TOKEN)
    monkeypatch.setattr(worker, 'active_executors', {})
    monkeypatch.setattr(worker, 'preemption_executors', {})
    monkeypatch.setattr(worker, 'preemption_in_progress', set())
    monkeypatch.setattr(worker, 'preempted_runner_ids', set())
    persistence.init_db()
    with persistence.get_db_conn() as conn:
        conn.execute("INSERT INTO workers (worker_id,hostname,status,cpus,total_ram_gb,available_storage_gb,gpu_count,vram_per_gpu,service_url,last_seen) VALUES ('w','w','online',8,32,100,1,'[24]','http://127.0.0.1:1',CURRENT_TIMESTAMP)")
        conn.execute("INSERT INTO jobs (job_id,repo,username,status,parallel_mode,home_worker,active_workers) VALUES ('low','lab/test','alice','running',1,'w','[\"w\"]')")
        conn.execute("INSERT INTO jobs (job_id,repo,username,status,parallel_mode) VALUES ('high','lab/test2','bob','pending',1)")
        resources = json.dumps({'cpus':4, 'ram_gb':20, 'vram_gb':16, 'gpus':1, 'storage_gb':0})
        conn.execute("INSERT INTO job_nodes (job_id,node_name,status,scheduling_priority,worker_id,runner_id,resources,gpu_ids,preempt_count,started_at) VALUES ('low','train','running','low','w','r',?,'[0]',0,CURRENT_TIMESTAMP)", (resources,))
        conn.execute("INSERT INTO job_nodes (job_id,node_name,status,scheduling_priority,resources) VALUES ('high','train','ready','high',?)", (resources,))
        conn.commit()
    persistence.record_runner_heartbeat('low', 'r', 'w', 'train')
    return head.app.test_client()


@pytest.mark.parametrize('failure', [None, ConnectionError('synthetic unreachable worker')])
def test_capacity_retained_until_correct_worker_ack(env, failure):
    with patch.object(scheduler.requests, 'post', side_effect=failure,
                      return_value=Mock(status_code=202)) as post:
        scheduler.schedule_iteration()
        scheduler.schedule_iteration()  # retry an unconfirmed stop, not the training stage
    assert post.call_count >= 2
    assert all(c.kwargs['allow_redirects'] is False for c in post.call_args_list)
    assert persistence.get_job_node('low', 'train')['status'] == 'preempting'
    assert persistence.get_job_node('low', 'train')['preempt_count'] == 0
    with persistence.get_db_conn() as conn:
        allocation = scheduler.get_worker_allocated_resources(conn, 'w')
        assert allocation['used_ram_gb'] == 20
        assert allocation['allocated_vram_by_gpu'][0] == 16
    assert env.post('/update_job_status', headers=AUTH, json={
        'job_id':'low', 'status':'failed','exit_code':-15,'worker_id':'w','runner_id':'wrong',
        'failure_reason':'JobPreempted'}).status_code == 409
    assert persistence.get_job_node('low', 'train')['status'] == 'preempting'
    response = env.post('/update_job_status', headers=AUTH, json={
        'job_id':'low', 'status':'failed','exit_code':-15,'worker_id':'w','runner_id':'r',
        'failure_reason':'JobPreempted'})
    assert response.status_code == 200
    assert persistence.get_job_node('low', 'train')['status'] == 'ready'
    assert persistence.get_job_node('low', 'train')['preempt_count'] == 1
    assert env.post('/update_job_status', headers=AUTH, json={
        'job_id':'low', 'status':'failed','exit_code':-15,'worker_id':'w','runner_id':'r',
        'failure_reason':'JobPreempted'}).status_code == 200
    assert persistence.get_job_node('low', 'train')['preempt_count'] == 1
    scheduler.schedule_iteration()
    result = scheduler.handle_next_node({'job_id':'high','worker_id':'w','runner_id':'high-runner'})
    assert result['action'] in ('run','switch_image')
    assert result['gpu_ids'] == [0]


def test_stage_exit_during_preemption_does_not_release_early(env):
    with patch.object(scheduler.requests, 'post', return_value=Mock(status_code=202)):
        scheduler.schedule_iteration()
    result = scheduler.handle_next_node({'job_id':'low','worker_id':'w','runner_id':'r',
                                         'node':'train','status':'done','exit_code':0})
    assert result == {'action':'yield'}
    assert persistence.get_job_node('low', 'train')['status'] == 'preempting'
    env.post('/update_job_status', headers=AUTH, json={'job_id':'low','status':'failed',
        'exit_code':-15,'worker_id':'w','runner_id':'r','failure_reason':'JobPreempted'})
    assert persistence.get_job_node('low', 'train')['status'] == 'done'


def test_worker_ack_follows_real_process_stop(env, monkeypatch):
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    executor = {'job_id':'low','process':process,'is_local':False}
    monkeypatch.setattr(worker.subprocess, 'run', lambda *a, **k: Mock(returncode=0,stdout='',stderr=''))
    def confirm(*args, **kwargs):
        assert process.poll() is not None
        assert kwargs['failure_reason'] == 'JobPreempted'
    try:
        with patch.object(worker, 'update_job_status', side_effect=confirm) as ack:
            worker._async_runner_preempt_cleanup('r', process, 0.1, executor)
        ack.assert_called_once()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_worker_never_acknowledges_failed_preservation(env):
    executor = {'job_id':'low','process':None,'is_local':True}
    worker.preemption_executors['r'] = executor
    worker.preemption_in_progress.add('r')
    with patch.object(worker, '_stop_runner_and_preserve', side_effect=RuntimeError('synthetic save failure')), \
         patch.object(worker, 'update_job_status') as ack:
        worker._async_runner_preempt_cleanup('r', None, 0, executor)
    ack.assert_not_called()
    assert worker.preemption_executors['r'] == executor
    assert 'r' not in worker.preemption_in_progress  # a subsequent request can retry


def test_worker_repeated_stop_requests_are_idempotent(env, monkeypatch):
    worker.active_executors['r'] = {'job_id':'low','process':None,'runner_id':'r'}
    monkeypatch.setattr(worker.threading.Thread, 'start', lambda self: None)
    client = worker.app.test_client()
    assert client.post('/api/worker/preempt_runner/r').status_code == 401
    assert client.post('/api/worker/preempt_runner/r', headers=AUTH).status_code == 202
    assert client.post('/api/worker/preempt_runner/r', headers=AUTH).status_code == 202
    assert 'r' in worker.active_executors
    assert client.post('/api/worker/preempt_runner/unknown', headers=AUTH).status_code == 404


@pytest.mark.parametrize('running', [False, True])
def test_docker_failure_or_running_container_prevents_ack(env, monkeypatch, running):
    def docker(cmd, **kwargs):
        if not running:
            return Mock(returncode=1, stdout='', stderr='synthetic Docker unavailable')
        return Mock(returncode=0, stdout='true' if 'inspect' in cmd else 'synthetic-container', stderr='')
    monkeypatch.setattr(worker.subprocess, 'run', docker)
    monkeypatch.setattr(worker, 'safe_docker_rm_f', lambda *a, **k: None)
    with patch.object(worker, 'update_job_status') as ack:
        worker._async_runner_preempt_cleanup('r', None, 0, {'job_id':'low','is_local':False})
    ack.assert_not_called()


def test_classic_local_preemption_uses_only_private_result_sync(env, monkeypatch, tmp_path):
    monkeypatch.setattr(worker, 'REPOS_DIR', str(tmp_path))
    calls=[]
    def run(cmd, **kwargs):
        calls.append((cmd,kwargs))
        return Mock(returncode=0,stdout='',stderr='')
    monkeypatch.setattr(worker.subprocess,'run',run)
    executor=dict(job_id='local-classic',repo='lab/test',is_local=True,is_parallel=False)
    worker._stop_runner_and_preserve('classic-local-classic',None,0,executor)
    sync=[(cmd,kw) for cmd,kw in calls if 'sync-local-results' in cmd]
    assert len(sync)==1
    cmd,kw=sync[0]
    assert kw['cwd']==str(tmp_path/'_local/lab/test')
    assert kw['env']['IS_LOCAL']=='1' and kw['env']['JOB_ID']=='local-classic'
    assert kw['env']['HEADNODE_URL']==worker.HEADNODE_URL
    assert kw['env']['CLUSTER_TOKEN']==TOKEN
    assert not any('push' in cmd or 'sync' in cmd for cmd,kw in calls)
    filters=[cmd for cmd,kw in calls if 'name=^/cluster-job-local-classic$' in cmd]
    assert len(filters)==1 and 'name=^/cluster-viewer-local-classic$' in filters[0]


def test_classic_local_preservation_failure_retains_reservation(env, monkeypatch):
    def run(cmd, **kwargs):
        if 'sync-local-results' in cmd:
            raise subprocess.CalledProcessError(1,cmd)
        return Mock(returncode=0,stdout='',stderr='')
    monkeypatch.setattr(worker.subprocess,'run',run)
    executor=dict(job_id='low',repo='lab/test',is_local=True,is_parallel=False)
    worker.preemption_executors['r']=executor
    with patch.object(worker,'update_job_status') as ack:
        worker._async_runner_preempt_cleanup('r',None,0,executor)
    ack.assert_not_called()
    assert worker.preemption_executors['r']==executor
