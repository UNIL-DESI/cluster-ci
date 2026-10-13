"""Real DVC + local transfer API tests, with only Docker/scheduling hardware mocked."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import urllib.error
from unittest.mock import Mock, patch
import uuid
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src/scheduler'))
with patch('threading.Thread.start'):
    import headnode_service as head
    import persistence
from src.runner import branch_executor as branch, cluster_http, dvc_git_helper as helper
from src.runner.local_workspace import prepare_submission, restore_node_archive, save_node_archive
from src.planner.stage_plan import compute_stage_plan

TOKEN = 'synthetic-token'
AUTH = {'Authorization': 'Bearer ' + TOKEN}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('IS_LOCAL', '1')
    monkeypatch.setenv('DVC_NO_ANALYTICS', '1')
    monkeypatch.setenv('CLUSTER_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.delenv('DATABASE_PATH', raising=False)
    monkeypatch.setattr(head, 'REPOS_DIR', str(tmp_path / 'head-repos'))
    monkeypatch.setattr(head, 'CLUSTER_TOKEN', TOKEN)
    persistence.init_db()
    job = str(uuid.uuid4())
    project = tmp_path / 'project'
    project.mkdir()
    subprocess.run(['git', 'init', '-q'], cwd=project, check=True)
    (project / '.dvc').mkdir()
    (project / '.dvc/config').write_text('')
    (project / '.gitignore').write_text('private/\noutputs/\n')
    (project / 'private').mkdir()
    (project / 'private/case.txt').write_text('SYNTHETIC_PRIVATE_INPUT')
    (project / 'run.py').write_text(
        "from pathlib import Path\nimport sys,os\n"
        "Path('outputs').mkdir(exist_ok=True)\n"
        "if sys.argv[1]=='join':\n"
        " Path('outputs/join.txt').write_text(Path('outputs/first.txt').read_text()+Path('outputs/second.txt').read_text())\n"
        "elif sys.argv[1]=='checkpoint':\n"
        " p=Path('outputs/checkpoint.txt'); p.write_text(str(int(p.read_text())+1 if p.exists() else 1))\n"
        " sys.exit(17 if os.environ.get('TEST_FAIL')=='1' else 0)\n"
        "else:\n"
        " Path('outputs/'+sys.argv[1]+'.txt').write_text(Path('private/case.txt').read_text()+sys.argv[1])\n")
    (project / 'dvc.yaml').write_text(
        f"stages:\n  train:\n    foreach: [first, second]\n    do:\n      cmd: {sys.executable} run.py ${{item}}\n"
        "      deps: [run.py, private/case.txt]\n      outs:\n        - outputs/${item}.txt:\n            cache: false\n"
        f"  join:\n    cmd: {sys.executable} run.py join\n    deps: [outputs/first.txt, outputs/second.txt]\n"
        "    outs:\n      - outputs/join.txt:\n          cache: false\n"
        f"  checkpoint:\n    cmd: {sys.executable} run.py checkpoint\n    outs:\n      - outputs/checkpoint.txt:\n          persist: true\n")
    plan = compute_stage_plan(str(project))
    with persistence.get_db_conn() as conn:
        conn.execute("INSERT INTO jobs (job_id,repo,branch,is_local,parallel_mode,status) VALUES (?,'lab/test','local-draft/test',1,1,'running')", (job,))
        conn.commit()
    persistence.init_job_nodes_from_plan(job, plan)
    archive = tmp_path / 'source.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        for name in ['.dvc/config', '.gitignore', 'private/case.txt', 'run.py', 'dvc.yaml']:
            tar.add(project / name, arcname=name)
    client = head.app.test_client()
    requests = []
    def transport(req, timeout):
        assert req.full_url.startswith('http://cluster.invalid/'), 'Unexpected outbound destination'
        requests.append(req)
        relative = req.full_url.removeprefix('http://cluster.invalid')
        response = client.open(relative, method=req.get_method(), data=req.data, headers=dict(req.header_items()))
        if response.status_code >= 400:
            raise urllib.error.HTTPError(req.full_url, response.status_code, '', response.headers, io.BytesIO(response.data))
        body = io.BytesIO(response.data)
        body.status = response.status_code
        return body
    monkeypatch.setattr(cluster_http, 'cluster_urlopen', transport)
    monkeypatch.setattr(helper, 'cluster_urlopen', transport)
    return dict(tmp=tmp_path, job=job, archive=archive, client=client, requests=requests)


def run_node(env, worker, node, fail=False):
    root = env['tmp'] / worker / '_local/lab/test'
    source = prepare_submission(env['archive'], root, env['job'])
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source).decode().strip()
    executor = branch.BranchExecutor('http://cluster.invalid', env['job'], worker+'-'+node,
            worker, source, 'lab/test', 'local-draft/test', start_commit=sha,
            cluster_token=TOKEN, docker=Mock())
    command = [helper.get_dvc_command()[0], 'repro', '-s', node]
    def execute(**kwargs):
        result = subprocess.run(command, cwd=executor.repo_dir, env=dict(os.environ, TEST_FAIL='1' if fail else '0'), capture_output=True)
        return result.returncode, result.stdout.decode()
    calls = []
    outputs = {'train@first': ['outputs/first.txt'], 'train@second': ['outputs/second.txt'],
               'join': ['outputs/join.txt'], 'checkpoint': ['outputs/checkpoint.txt']}[node]
    deps = ['outputs/first.txt', 'outputs/second.txt'] if node == 'join' else []
    def next_node(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return dict(action='run', node=node, dep_paths=deps, out_paths=outputs, image='synthetic', resources={})
        return {'action': 'finish'}
    def start(*args):
        executor.current_container = 'synthetic'
    with patch.object(executor, 'call_next_node', side_effect=next_node), \
         patch.object(executor, '_start_heartbeat'), patch.object(executor, 'start_container_for_image', side_effect=start), \
         patch.object(executor, 'stop_current_container'), patch.object(executor, 'execute_node_in_container', side_effect=execute):
        executor.run()
    assert not Path(executor.repo_dir).exists()
    return calls[-1]


def test_real_dvc_foreach_handoff_across_workers_and_final_retrieval(env):
    first = run_node(env, 'worker-one', 'train@first')
    second = run_node(env, 'worker-two', 'train@second')
    joined = run_node(env, 'worker-two', 'join')
    assert [x['status'] for x in [first, second, joined]] == ['done'] * 3
    assert env['client'].get(f"/api/jobs/{env['job']}/results").status_code == 401
    response = env['client'].get(f"/api/jobs/{env['job']}/results", headers=AUTH)
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert archive.read('outputs/join.txt') == b'SYNTHETIC_PRIVATE_INPUTfirstSYNTHETIC_PRIVATE_INPUTsecond'
        assert not any(name.startswith('.nodes/') for name in archive.namelist())
        assert 'run.py' not in archive.namelist()
    assert all(req.get_header('Authorization') == AUTH['Authorization'] for req in env['requests'])


def test_unfinished_checkpoint_is_preserved_and_resumed_on_other_worker(env):
    failed = run_node(env, 'worker-one', 'checkpoint', fail=True)
    assert failed['status'] == 'failed'
    resumed = run_node(env, 'worker-two', 'checkpoint')
    assert resumed['status'] == 'done'
    response = env['client'].get(f"/api/jobs/{env['job']}/node_results/checkpoint", headers=AUTH)
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert archive.read('outputs/checkpoint.txt') == b'2'


def test_node_files_require_token_and_local_job(env):
    path = f"/api/jobs/{env['job']}/node_results/train@first"
    assert env['client'].get(path).status_code == 401
    assert env['client'].get(path, headers={'Authorization': 'Bearer wrong'}).status_code == 401
    with persistence.get_db_conn() as conn:
        conn.execute('UPDATE jobs SET is_local=0 WHERE job_id=?', (env['job'],))
        conn.commit()
    response = env['client'].post('/api/local_transfers', headers=AUTH, json={
        'purpose': 'node_results', 'job_id': env['job'], 'node_name': 'train@first',
        'total_size': 10, 'sha256': 'a' * 64})
    assert response.status_code == 400


def test_restore_rejects_unrelated_source_or_lock_entries(env):
    project = env['tmp'] / 'project'
    malicious = env['tmp'] / 'malicious.zip'
    with zipfile.ZipFile(malicious, 'w') as archive:
        archive.writestr('dvc.lock', 'schema: "2.0"\nstages: {}\n')
        archive.writestr('run.py', 'overwrite source')
    with pytest.raises(ValueError, match='Unexpected'):
        restore_node_archive(project, 'train@first', malicious)


def test_failed_transfer_keeps_checkpoint_and_worktree(env, monkeypatch):
    # A transfer failure must never cause disposal of the only saved state.
    import src.runner.local_workspace as local
    def fail(*args, **kwargs):
        raise ConnectionError('synthetic network failure')
    monkeypatch.setattr(local, 'upload_node_archive', fail)
    with pytest.raises(ConnectionError):
        run_node(env, 'worker-one', 'checkpoint', fail=True)
    source = env['tmp'] / 'worker-one/_local/lab/test/.cluster-ci-local/jobs' / env['job'] / 'source'
    archive = local.node_archive_path(source, 'checkpoint')
    assert archive.exists()
    records = list(source.parent.glob('runner-*.json'))
    assert records and Path(json.loads(records[0].read_text())['workspace']).exists()
