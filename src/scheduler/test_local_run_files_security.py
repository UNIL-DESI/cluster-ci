"""Tests de non-régression pour la sécurité des fichiers de jobs locaux et CAS."""
import hashlib
import os
from pathlib import Path
import sys
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault('HEADNODE_URL', 'http://127.0.0.1:1')
with patch('threading.Thread.start'):
    import headnode_service as headnode
    import worker_agent as worker
    import persistence
from src.scheduler import artifact_registry as registry

TOKEN = 'synthetic-security-token'
AUTH = {'Authorization': 'Bearer ' + TOKEN}


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, 'REPOS_DIR', str(tmp_path))
    monkeypatch.setattr(headnode, 'REPOS_DIR', str(tmp_path))
    monkeypatch.setattr(worker, 'CLUSTER_TOKEN', TOKEN)
    monkeypatch.setattr(headnode, 'CLUSTER_TOKEN', TOKEN)
    monkeypatch.setattr(headnode.app, 'secret_key', 'test-session-secret')
    monkeypatch.setenv('CLUSTER_DB_PATH', str(tmp_path / 'scheduler.sqlite'))
    persistence.init_db()

    payload = b'PRIVATE CARITAS DATA'
    md5 = hashlib.md5(payload).hexdigest()
    local_cas_path = tmp_path / '_local/lab/test/.dvc/cache/files/md5' / md5[:2] / md5[2:]
    local_cas_path.parent.mkdir(parents=True)
    local_cas_path.write_bytes(payload)

    with persistence.get_db_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, repo, branch, commit_hash, is_local, status) "
            "VALUES ('local-job-1', 'lab/test', 'main', 'c1', 1, 'completed')"
        )
        conn.execute(
            "INSERT INTO jobs (job_id, repo, branch, commit_hash, is_local, status) "
            "VALUES ('public-job-1', 'lab/public', 'main', 'c2', 0, 'completed')"
        )
        conn.commit()

    return {
        'headnode_client': headnode.app.test_client(),
        'worker_client': worker.app.test_client(),
        'md5': md5,
        'payload': payload,
        'tmp_path': tmp_path,
    }


def test_api_run_files_local_job_requires_token(test_env):
    """Vérifie que /api/runs/<job_id>/files pour un job local exige le jeton (401 sans jeton, 200 avec)."""
    client = test_env['headnode_client']

    mock_resp = headnode.app.response_class('[{"path": "secret.csv"}]', mimetype='application/json')
    with patch.object(headnode, 'local_worker_get', return_value=mock_resp):
        # Sans jeton -> 401
        res = client.get('/api/runs/local-job-1/files')
        assert res.status_code == 401
        assert b"Cluster token required for local files" in res.data

        # Avec mauvais jeton -> 401
        res = client.get('/api/runs/local-job-1/files', headers={'Authorization': 'Bearer wrong-token'})
        assert res.status_code == 401

        # Avec jeton valide -> 200
        res = client.get('/api/runs/local-job-1/files', headers=AUTH)
        assert res.status_code == 200
        assert b"secret.csv" in res.data


def test_api_run_files_local_job_allows_unlocked_browser_session(test_env):
    """Vérifie qu'une session navigateur déverrouillée via /local-access accède à /api/runs/<job_id>/files."""
    client = test_env['headnode_client']

    with client.session_transaction() as sess:
        sess['user'] = {'login': 'researcher'}

    # Déverrouillage valide
    unlock = client.post('/local-access', data={'cluster_token': TOKEN})
    assert unlock.status_code == 302

    mock_resp = headnode.app.response_class('[{"path": "unlocked.csv"}]', mimetype='application/json')
    with patch.object(headnode, 'local_worker_get', return_value=mock_resp):
        res = client.get('/api/runs/local-job-1/files')
        assert res.status_code == 200
        assert b"unlocked.csv" in res.data


def test_api_run_files_public_job_unchanged_without_token(test_env):
    """Vérifie qu'un job public ne déclenche pas le contrôle de jeton local."""
    client = test_env['headnode_client']

    with patch.object(headnode, 'find_local_repo', return_value=None), \
         patch.object(headnode, 'subprocess') as subp:
        subp.run.return_value = Mock(returncode=0, stdout='[{"path": "public.csv"}]')
        res = client.get('/api/runs/public-job-1/files')
        # Ne doit pas renvoyer 401 Unauthorized
        assert res.status_code != 401


def test_fetch_cas_without_local_param_never_serves_private_cache(test_env):
    """Prouve que /fetch_cas/<md5> sans ?local=1 renvoie 404 pour un objet du cache local."""
    worker_client = test_env['worker_client']
    md5 = test_env['md5']

    # Sans ?local=1, même avec jeton, l'objet sous _local n'est jamais servi
    res = worker_client.get(f'/fetch_cas/{md5}')
    assert res.status_code == 404

    res = worker_client.get(f'/fetch_cas/{md5}', headers=AUTH)
    assert res.status_code == 404

    # Avec ?local=1 sans jeton -> 401
    res = worker_client.get(f'/fetch_cas/{md5}?local=1')
    assert res.status_code == 401

    # Avec ?local=1 et jeton -> 200
    res = worker_client.get(f'/fetch_cas/{md5}?local=1', headers=AUTH)
    assert res.status_code == 200
    assert res.data == test_env['payload']


def test_cross_job_local_artifact_invisible_to_public_job(test_env):
    """Prouve qu'un artefact d'un job local n'est jamais proposé à un job non-local."""
    with persistence.get_db_conn() as conn:
        registry.record_node_outputs(
            conn, 'local-job-1', 'node-private', 'w1',
            [{'md5': test_env['md5'], 'path': 'data/features.parquet', 'size': 1000}]
        )
        # Consommateur public : ne doit voir aucun hash
        public_hashes = registry.hashes_for_paths(conn, ['data/features.parquet'], is_local=False)
        assert public_hashes == []

        # Consommateur local : voit le hash
        local_hashes = registry.hashes_for_paths(conn, ['data/features.parquet'], is_local=True)
        assert local_hashes == [test_env['md5']]
