"""Prevent automatic reuse of local artifacts by publishable jobs."""
import json
import os
from pathlib import Path
import sqlite3
import sys
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault('HEADNODE_URL', 'http://127.0.0.1:1')
with patch('threading.Thread.start'):
    import headnode_service as headnode
    import persistence
from src.scheduler import artifact_registry as registry
from src.runner import branch_executor as branch

HASH = '1' * 32
DIR_HASH = '2' * 32 + '.dir'
TOKEN = 'synthetic-token'


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv('CLUSTER_DB_PATH', str(tmp_path / 'scheduler.sqlite'))
    monkeypatch.setattr(headnode, 'CLUSTER_TOKEN', TOKEN)
    persistence.init_db()
    with persistence.get_db_conn() as conn:
        conn.execute("INSERT INTO workers(worker_id,hostname,service_url,total_ram_gb,cpus,total_storage_gb,available_storage_gb) VALUES ('w','w','http://worker.invalid:6000',64,8,1000,1000)")
        conn.commit()


def add_job(conn, job_id, local):
    conn.execute("INSERT INTO jobs(job_id,repo,branch,is_local,parallel_mode,status,home_worker,active_workers) VALUES (?,?,'main',?,1,'running','w','[\"w\"]')",
                 (job_id, 'lab/' + job_id, int(local)))


@pytest.mark.parametrize('consumer_local', [False, True])
@pytest.mark.parametrize('producer_local', [False, True])
def test_dispatch_filters_cross_job_paths(db, producer_local, consumer_local):
    with persistence.get_db_conn() as conn:
        add_job(conn, 'producer', producer_local)
        conn.execute("UPDATE jobs SET status='completed' WHERE job_id='producer'")
        add_job(conn, 'consumer', consumer_local)
        conn.execute("INSERT INTO job_nodes(job_id,node_name,status,resources,deps,dep_paths) VALUES ('consumer','train','ready',?,'[]',?)",
                     (json.dumps({'cpus': 1, 'ram_gb': 1, 'gpus': 0, 'vram_gb': 0}), json.dumps(['data/features.bin'])))
        registry.record_node_outputs(conn, 'producer', 'prepare', 'w', [
            {'md5': HASH, 'path': 'data/features.bin', 'size': 100},
        ])
        conn.commit()
    response = headnode.app.test_client().post('/api/jobs/consumer/next_node',
        json={'worker': 'w', 'runner_id': 'r'}, headers={'Authorization': 'Bearer ' + TOKEN})
    assert response.status_code == 200
    result = response.get_json()
    assert result['action'] in ('run', 'switch_image'), result
    assert (HASH in result['dep_sources']) == (consumer_local or not producer_local)


def test_directory_sources_and_affinity_require_public_provenance(db):
    with persistence.get_db_conn() as conn:
        add_job(conn, 'private', True)
        add_job(conn, 'public', False)
        registry.record_node_outputs(conn, 'private', 'prepare', 'w', [
            {'md5': DIR_HASH, 'path': 'data', 'size': 10},
            {'md5': HASH, 'path': 'data/file', 'size': 100, 'parent_dir_hash': DIR_HASH},
        ])
        peers = {'w': 'http://worker.invalid:6000'}
        assert registry.sources_for(conn, [HASH, DIR_HASH], peers) == {HASH: [], DIR_HASH: []}
        assert registry.affinity_bytes(conn, [HASH], 'w') == 0
        assert registry.affinity_bytes(conn, [HASH], 'w', is_local=True) == 100
        assert registry.sources_for(conn, [HASH], peers, is_local=True)[HASH]
        # A public parent alone must not make a private-only child discoverable.
        registry.record_node_outputs(conn, 'public', 'prepare', 'w', [{'md5': DIR_HASH, 'size': 10}])
        assert registry.sources_for(conn, [HASH], peers)[HASH] == []


def test_orphan_artifact_is_not_treated_as_public(db):
    with persistence.get_db_conn() as conn:
        registry.record_node_outputs(conn, 'unknown-job', 'prepare', 'w', [{'md5': HASH, 'path': 'data/file', 'size': 10}])
        assert registry.hashes_for_paths(conn, ['data/file']) == []
        assert registry.sources_for(conn, [HASH], {'w': 'http://worker.invalid'})[HASH] == []
        assert registry.affinity_bytes(conn, [HASH], 'w') == 0


@pytest.mark.parametrize('local', [False, True])
def test_missing_dependency_recovery_does_not_reuse_private_producer(db, local):
    with persistence.get_db_conn() as conn:
        add_job(conn, 'private', True)
        add_job(conn, 'consumer', local)
        registry.record_node_outputs(conn, 'private', 'produce', 'w', [{'md5': HASH, 'path': 'data/file'}])
        conn.execute("INSERT INTO job_nodes(job_id,node_name,status,out_paths) VALUES ('consumer','produce','done',?)",
                     (json.dumps([{'path': 'data/file', 'md5': HASH}]),))
        conn.execute("INSERT INTO job_nodes(job_id,node_name,status,deps,dep_paths) VALUES ('consumer','train','running',?,?)",
                     (json.dumps(['produce']), json.dumps(['data/file'])))
        conn.commit()
    result = persistence.handle_missing_deps('consumer', 'train', ['data/file'])
    assert (result.get('action') == 'retry_fetch') is local, result
    if not local:
        with persistence.get_db_conn() as conn:
            row = conn.execute("SELECT status,stale_reason FROM job_nodes WHERE node_name='produce'").fetchone()
            assert tuple(row) == ('ready', 'outputs_missing_no_peer_cache')


def test_parallel_persistent_volumes_are_separated_by_mode(tmp_path, monkeypatch):
    volumes = []
    for local in ['0', '1']:
        monkeypatch.setenv('IS_LOCAL', local)
        docker = Mock(spec=branch.DockerRunner)
        docker.run_container.return_value = 0
        docker.exec_in_container.return_value = (0, '')
        obj = branch.BranchExecutor(headnode_url='http://headnode.invalid', job_id='synthetic',
            runner_id='r', worker_id='w', repo_dir=str(tmp_path), target_repo='lab/test', target_branch='main', docker=docker)
        obj.start_container_for_image('synthetic/image:1')
        created = {call.args[0] for call in docker.create_volume.call_args_list}
        assert docker.run_container.call_args.kwargs['home_volume'] in created
        volumes.append(created)
        with patch('src.runner.host_guard.docker_resource_args', return_value=[]), patch.object(branch.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            branch.DockerRunner().run_container('image', 'container', 'home', str(tmp_path), str(tmp_path), env={'IS_LOCAL': local})
        mounted = ' '.join(run.call_args.args[0])
        for name in created:
            if name.endswith(('-uv-cache', '-pip-cache')):
                assert name + ':' in mounted
    assert volumes[0].isdisjoint(volumes[1])
