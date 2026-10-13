"""Real DVC foreach submission -> DB -> worker -> shell snapshot -> startup.

No live cluster, Docker, Git remote access, or research data. Infrastructure
operations are mocked; DVC planning, submission logic, SQLite and Git are real.
"""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from contextlib import ExitStack
from unittest.mock import Mock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src/scheduler'))
os.environ.setdefault('HEADNODE_URL', 'http://127.0.0.1:1')
with patch('threading.Thread.start'):
    import headnode_service as head
    import worker_agent as worker
    import persistence
from src.cluster import cluster_run as client
from src.planner.stage_plan import compute_stage_plan
from src.runner import branch_executor


def test_local_foreach_submission_starts_from_real_private_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv('DVC_NO_ANALYTICS', '1')
    monkeypatch.setenv('CLUSTER_TOKEN', 'synthetic-token')
    monkeypatch.setenv('CLUSTER_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.delenv('DATABASE_PATH', raising=False)
    monkeypatch.setattr(head, 'CLUSTER_TOKEN', 'synthetic-token')
    monkeypatch.setattr(head, 'MAINTENANCE_MODE', False)
    monkeypatch.setattr(head, 'REPOS_DIR', str(tmp_path / 'head-repos'))
    persistence.init_db()
    project = tmp_path / 'project'
    project.mkdir()
    def git(directory, *args):
        return subprocess.run(['git', *args], cwd=directory, check=True, capture_output=True)
    git(project, 'init')
    git(project, 'remote', 'add', 'origin', 'https://github.com/synthetic/audit.git')
    (project / '.dvc').mkdir()
    (project / '.dvc/config').write_text('')
    (project / '.cluster-ci').write_text('PARALLEL_STAGES=true\nALLOWED_WORKERS=synthetic-worker\nMAX_RUNTIME_HOURS=1\n')
    (project / 'dvc.yaml').write_text(
        'stages:\n  train:\n    foreach: [first, second]\n'
        '    do:\n      cmd: echo ${item}\n      always_changed: true\n')
    git(project, 'add', '-A')
    git(project, '-c', 'user.name=Audit', '-c', 'user.email=audit@example.invalid', 'commit', '-m', 'Synthetic DVC fixture')
    archive_copy = tmp_path / 'source.tar.gz'
    def upload(path, *args, **kwargs):
        shutil.copyfile(path, archive_copy)
        return {'transfer_id': 'synthetic-transfer'}
    def consume(transfer, dest):
        shutil.copyfile(archive_copy, dest)
        return True
    submitted = {}
    def submit_transport(req, **kwargs):
        assert req.full_url == 'http://127.0.0.1:1/submit_job'
        payload = json.loads(req.data)
        assert {n['name'] for n in payload['plan']['nodes']} == {'train@first', 'train@second'}
        response = head.app.test_client().post('/submit_job', json=payload,
                    headers={'Authorization': 'Bearer synthetic-token'})
        assert response.status_code == 200, response.get_data(as_text=True)
        submitted.update(response.get_json())
        cm = Mock()
        cm.__enter__ = Mock(return_value=Mock(read=Mock(return_value=response.data)))
        cm.__exit__ = Mock(return_value=False)
        return cm
    monkeypatch.chdir(project)
    with ExitStack() as stack:
        for name in ['clean_old_results', 'save_run_state', 'clear_run_state']:
            stack.enter_context(patch.object(client, name))
        stack.enter_context(patch.object(client, 'discover_headnode_url', return_value='http://127.0.0.1:1'))
        stack.enter_context(patch.object(client, 'get_repo_full_name', return_value='synthetic/audit'))
        stack.enter_context(patch.object(client, 'get_local_username', return_value='synthetic-user'))
        stack.enter_context(patch.object(client, 'run_planner_for_submission', side_effect=compute_stage_plan))
        stack.enter_context(patch.object(client, 'upload_local_file_in_chunks', side_effect=upload))
        stack.enter_context(patch.object(head, '_consume_source_transfer', side_effect=consume))
        stack.enter_context(patch.object(client, 'cluster_urlopen', side_effect=submit_transport))
        stack.enter_context(patch.object(client, 'stream_local_job_logs_and_wait', return_value=0))
        client.local_run()
    assert submitted['parallel_mode'] == 1
    job_id = submitted['job_id']
    with persistence.get_db_conn() as conn:
        conn.execute("UPDATE jobs SET status='assigned',worker_id='synthetic-worker',home_worker='synthetic-worker' WHERE job_id=?", (job_id,))
        conn.commit()
    payload = head.app.test_client().get('/worker_poll/synthetic-worker',
                    headers={'Authorization': 'Bearer synthetic-token'}).get_json()
    assert payload['is_local'] == payload['parallel_mode'] == 1
    assert payload['commit_hash'] == 'local-snapshot'
    monkeypatch.setattr(worker, 'LOGS_DIR', str(tmp_path))
    monkeypatch.setattr(worker, 'REPOS_DIR', str(tmp_path / 'worker-repos'))
    monkeypatch.setattr(worker, 'WORKER_ID', 'synthetic-worker')
    monkeypatch.setattr(worker, 'active_executors', {})
    monkeypatch.setenv('CLUSTER_CI_RUN_PATH', '/synthetic/runner')
    process = Mock(stdout=io.StringIO(''), poll=Mock(return_value=0), wait=Mock(return_value=0))
    with ExitStack() as stack:
        for name in ['purge_orphan_runners_and_containers', 'update_job_status', 'safe_docker_rm_f',
                     'purge_ollama_vram_on_host', 'kill_dvc_viewer_processes']:
            stack.enter_context(patch.object(worker, name))
        stack.enter_context(patch.object(worker.threading.Thread, 'start'))
        stack.enter_context(patch.object(worker.threading.Thread, 'join'))
        popen = stack.enter_context(patch.object(worker.subprocess, 'Popen', return_value=process))
        worker.execute_job(payload)
    runner_env = popen.call_args.kwargs['env']
    assert runner_env['CALLER_COMMIT_SHA'] == 'local-snapshot'
    assert runner_env['CLUSTER_CI_PARALLEL_MODE'] == '1'

    # Execute the actual local shell startup block with a synthetic archive.
    # Only curl is replaced with a local file copy; Git and snapshot creation run.
    fixed_base = tmp_path / 'fixed-base'
    (fixed_base / 'src/runner').mkdir(parents=True)
    shutil.copyfile(ROOT / 'src/runner/local_workspace.py', fixed_base / 'src/runner/local_workspace.py')
    shell = (ROOT / 'src/runner/run_research_pipeline.sh').read_text()
    begin = shell.index('if [ "$IS_LOCAL" = "1" ]; then\n    log_info "[LOCAL MODE] Bypassing')
    end = shell.index('\nelse\n    if [ ! -d "$REPO_BASENAME/.git" ]; then', begin)
    script = ('set -e\nlog_info() { :; }\nlog_success() { :; }\n'
              'curl() { cp "$FIXTURE_SOURCE_ARCHIVE" "${@: -1}"; }\n'
              + shell[begin:end] + '\nfi\n'
              + 'printf "AUDIT_SOURCE=%s\nAUDIT_SHA=%s\n" "$(pwd)" "$CALLER_COMMIT_SHA"\n')
    run_env = dict(runner_env, BASE_DIR=str(fixed_base), REPO_WORK_DIR='repositories/_local/synthetic/audit',
                   FIXTURE_SOURCE_ARCHIVE=str(archive_copy))
    result = subprocess.run(['bash', '-c', script], env=run_env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    captured = dict(line.split('=',1) for line in result.stdout.splitlines() if line.startswith('AUDIT_'))
    workspace = Path(captured['AUDIT_SOURCE'])
    run_env['CALLER_COMMIT_SHA'] = captured['AUDIT_SHA']
    assert run_env['CALLER_COMMIT_SHA'] != 'local-snapshot'
    assert git(workspace, 'rev-parse', 'HEAD').stdout.decode().strip() == run_env['CALLER_COMMIT_SHA']
    assert git(workspace, 'remote').stdout == b''
    argv = ['branch_executor.py', '--repo-dir', str(workspace), '--job-id', job_id,
            '--runner-id', 'synthetic-runner', '--target-repo', 'synthetic/audit']
    with patch.dict(os.environ, run_env, clear=True), patch.object(sys, 'argv', argv), \
         patch.object(branch_executor.signal, 'signal'), \
         patch.object(branch_executor.BranchExecutor, 'run', autospec=True, return_value=0) as execute:
        with pytest.raises(SystemExit) as stopped:
            branch_executor.main()
        assert stopped.value.code == 0
    executor = execute.call_args.args[0]
    try:
        assert (Path(executor.repo_dir) / 'dvc.yaml').read_text() == (workspace / 'dvc.yaml').read_text()
    finally:
        executor.cleanup_worktree()
