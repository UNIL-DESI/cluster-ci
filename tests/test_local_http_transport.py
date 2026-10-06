"""Real loopback HTTP only: redirects must not receive tokens or upload bytes."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import runpy
import sys
import threading
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

from src.cluster import cluster_run as client
from src.runner import branch_executor, cluster_http, dvc_git_helper
from src.scheduler import submit_job


@pytest.fixture(scope='module')
def http_pair():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_request(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            self.server.received.append((self.path, self.headers.get('Authorization'), body))
            if self.path.startswith('/redirect/') or self.path == '/submit_job':
                code = int(self.path.rsplit('/', 1)[-1]) if self.path.startswith('/redirect/') else 307
                self.send_response(code)
                self.send_header('Location', self.server.destination + '/sink')
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'ok')

        do_GET = do_POST = do_PUT = handle_request

    servers = [ThreadingHTTPServer(('127.0.0.1', 0), Handler) for _ in range(2)]
    threads = []
    for server in servers:
        server.received = []
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
        thread.start()
        threads.append(thread)
    source, sink = servers
    source.destination = f'http://127.0.0.1:{sink.server_port}'
    try:
        yield f'http://127.0.0.1:{source.server_port}', source, sink
    finally:
        for server, thread in zip(servers, threads):
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()


@pytest.fixture(scope='module')
def transports():
    # Emulate install.sh's single-file CLI, without access to the src package.
    with patch.dict(sys.modules, {'src': None, 'src.runner.cluster_http': None}):
        standalone = runpy.run_path(str(Path(client.__file__)))
    assert standalone['cluster_urlopen'] is not cluster_http.cluster_urlopen
    return [cluster_http.cluster_urlopen, client.cluster_urlopen,
            branch_executor.cluster_urlopen, dvc_git_helper.cluster_urlopen,
            standalone['cluster_urlopen']]


@pytest.mark.parametrize('code', [301, 302, 303, 307, 308])
@pytest.mark.parametrize('method', ['GET', 'POST', 'PUT'])
def test_redirects_cannot_forward_credentials_or_body(http_pair, transports, code, method):
    base, source, sink = http_pair
    for transport in transports:
        request = urllib.request.Request(base + f'/redirect/{code}', method=method,
            data=b'synthetic-private-upload' if method != 'GET' else None,
            headers={'Authorization': 'Bearer synthetic-token'})
        with pytest.raises(urllib.error.HTTPError) as error:
            transport(request, timeout=2)
        assert error.value.code == code
        error.value.close()
        assert source.received[-1][1] == 'Bearer synthetic-token'
        assert sink.received == []


def test_configured_endpoint_still_accepts_authenticated_data(http_pair, transports):
    base, source, _ = http_pair
    for transport in transports:
        request = urllib.request.Request(base + '/ok', data=b'synthetic-upload',
            headers={'Authorization': 'Bearer synthetic-token'})
        with transport(request, timeout=2) as response:
            assert response.read() == b'ok'
        assert source.received[-1] == ('/ok', 'Bearer synthetic-token', b'synthetic-upload')


def test_submission_does_not_redirect_private_job_metadata(http_pair, tmp_path, monkeypatch):
    base, source, sink = http_pair
    (tmp_path / '.cluster-ci').write_text('PARALLEL_STAGES=false\nMAX_RUNTIME_HOURS=1\n')
    monkeypatch.setenv('CLUSTER_TOKEN', 'synthetic-token')
    monkeypatch.setenv('DVC_NO_ANALYTICS', '')
    with pytest.raises(SystemExit):
        submit_job.submit_job(base, 'lab/synthetic', 'local-test', is_local=True,
            repo_dir=str(tmp_path), commit_hash='synthetic', env_vars={'PRIVATE': 'synthetic-value'})
    assert sink.received == []
    assert b'synthetic-value' in source.received[-1][2]
    assert source.received[-1][1] == 'Bearer synthetic-token'
    from dvc.analytics import is_enabled
    assert is_enabled() is False
