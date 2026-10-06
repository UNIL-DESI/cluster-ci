"""Confidentiality checks with synthetic files, Flask clients and no cluster calls."""
import hashlib
import os
from pathlib import Path
import sys
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault('HEADNODE_URL', 'http://127.0.0.1:1')
with patch('threading.Thread.start'):
    import headnode_service as headnode
    import worker_agent as worker
from src.runner.fetch_cas_dependencies import download_single_object

TOKEN = 'synthetic-test-token'
AUTH = {'Authorization': f'Bearer {TOKEN}'}


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, 'REPOS_DIR', str(tmp_path))
    monkeypatch.setattr(worker, 'CLUSTER_TOKEN', TOKEN)
    payload = b'SYNTHETIC PRIVATE CONTENT'
    md5 = hashlib.md5(payload).hexdigest()
    root = tmp_path / '_local/lab/example'
    path = root / '.dvc/cache/files/md5' / md5[:2] / md5[2:]
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    return worker.app.test_client(), md5, payload, root


@pytest.mark.parametrize('headers', [{}, {'Authorization': 'Bearer wrong'}, AUTH])
def test_public_cas_never_searches_private_storage(cache, headers):
    client, md5, _, _ = cache
    assert client.get('/fetch_cas/' + md5, headers=headers).status_code == 404


@pytest.mark.parametrize('headers', [{}, {'Authorization': 'Bearer wrong'}])
def test_private_cas_requires_token(cache, headers):
    client, md5, _, _ = cache
    assert client.get('/fetch_cas/' + md5 + '?local=1', headers=headers).status_code == 401


def test_authorized_private_cas_and_directory_manifest(cache):
    client, md5, payload, root = cache
    for suffix in ['', '.dir']:
        path = root / '.dvc/cache/files/md5' / md5[:2] / (md5[2:] + suffix)
        path.write_bytes(payload)
        response = client.get('/fetch_cas/' + md5 + suffix + '?local=1', headers=AUTH)
        assert response.status_code == 200
        assert response.data == payload


def test_private_cas_fails_closed_without_configured_token(cache, monkeypatch):
    client, md5, _, _ = cache
    monkeypatch.setattr(worker, 'CLUSTER_TOKEN', None)
    assert client.get('/fetch_cas/' + md5 + '?local=1', headers=AUTH).status_code == 401


def test_public_cas_still_serves_public_copies(cache, tmp_path):
    client, md5, payload, _ = cache
    path = tmp_path / 'lab/public/.dvc/cache/files/md5' / md5[:2] / md5[2:]
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    assert client.get('/fetch_cas/' + md5).data == payload


def test_public_alias_cannot_expose_private_cache(cache, tmp_path):
    client, md5, _, private = cache
    (tmp_path / 'lab').mkdir()
    (tmp_path / 'lab/alias').symlink_to(private, target_is_directory=True)
    assert client.get('/fetch_cas/' + md5).status_code == 404
    assert client.get('/fetch_artifact/lab/alias/.dvc/cache/files/md5/' + md5[:2] + '/' + md5[2:]).status_code == 401


@pytest.mark.parametrize('local', [False, True])
def test_peer_download_respects_consumer_mode(cache, tmp_path, monkeypatch, local):
    client, md5, payload, _ = cache
    monkeypatch.setenv('IS_LOCAL', '1' if local else '0')
    monkeypatch.setenv('CLUSTER_TOKEN', TOKEN)
    session = Mock()

    def get(url, **kwargs):
        assert kwargs['allow_redirects'] is False
        assert bool(kwargs.get('headers')) == local
        parsed = urlsplit(url)
        response = client.get(parsed.path + ('?' + parsed.query if parsed.query else ''), headers=kwargs.get('headers', {}))
        return Mock(status_code=response.status_code, iter_content=lambda **_: iter([response.data]))

    session.get.side_effect = get
    destination = tmp_path / 'consumer-cache'
    result = download_single_object(md5, ['http://worker.invalid:6000'], destination, session=session)
    assert result[0] is local
    path = destination / md5[:2] / md5[2:]
    if local:
        assert path.read_bytes() == payload
    else:
        assert not path.exists()


@pytest.mark.parametrize('endpoint,handler', [
    ('next_node', 'handle_next_node'), ('runner_heartbeat', 'record_runner_heartbeat'),
])
@pytest.mark.parametrize('configured,supplied', [(TOKEN, None), (TOKEN, 'wrong'), (None, TOKEN)])
def test_runner_control_fails_closed(endpoint, handler, configured, supplied, monkeypatch):
    monkeypatch.setattr(headnode, 'CLUSTER_TOKEN', configured)
    headers = {'Authorization': f'Bearer {supplied}'} if supplied else {}
    with patch.object(headnode, handler) as call:
        result = headnode.app.test_client().post('/api/jobs/synthetic/' + endpoint,
            json={'worker': 'w', 'runner_id': 'r'}, headers=headers)
    assert result.status_code == 401
    call.assert_not_called()


@pytest.mark.parametrize('endpoint,handler', [
    ('next_node', 'handle_next_node'), ('runner_heartbeat', 'record_runner_heartbeat'),
])
def test_authenticated_runner_control_keeps_working(endpoint, handler, monkeypatch):
    monkeypatch.setattr(headnode, 'CLUSTER_TOKEN', TOKEN)
    with patch.object(headnode, handler, return_value={'action': 'finish'}) as call:
        result = headnode.app.test_client().post('/api/jobs/synthetic/' + endpoint,
            json={'worker': 'w', 'runner_id': 'r'}, headers=AUTH)
    assert result.status_code == 200
    call.assert_called_once()
