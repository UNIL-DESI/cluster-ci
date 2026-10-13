import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
from unittest.mock import Mock

import pytest

from src.runner.local_workspace import prepare_submission
from src.runner.branch_executor import BranchExecutor


def archive(tmp_path, version='new', ignored='input', unsafe=None):
    path = tmp_path / ('source-' + version + '.tar.gz')
    entries = {'.gitignore': 'private-data/\n', 'script.py': version,
               'private-data/case.txt': ignored, '.dvc/config': ''}
    if unsafe:
        entries[unsafe] = 'bad'
    with tarfile.open(path, 'w:gz') as tar:
        for name, text in entries.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def test_each_submission_freezes_exact_inputs_without_git_remote(tmp_path, monkeypatch):
    monkeypatch.setenv('IS_LOCAL', '1')
    repo = tmp_path / '_local/lab/test'
    first = prepare_submission(archive(tmp_path, 'first'), repo, 'job-first')
    second_archive = archive(tmp_path, 'second', 'updated private input')
    second = prepare_submission(second_archive, repo, 'job-second')
    assert prepare_submission(second_archive, repo, 'job-second') == second
    for source, version, data in [(first, 'first', 'input'), (second, 'second', 'updated private input')]:
        sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source).decode().strip()
        assert subprocess.check_output(['git', 'remote'], cwd=source) == b''
        assert b'private-data/case.txt' not in subprocess.check_output(['git', 'ls-files'], cwd=source)
        executor = BranchExecutor('', 'job-' + version, 'runner', 'worker', source,
                                  'lab/test', 'local-draft/test', start_commit=sha, docker=Mock())
        try:
            assert (Path(executor.repo_dir) / 'script.py').read_text() == version
            assert (Path(executor.repo_dir) / 'private-data/case.txt').read_text() == data
            assert Path(executor.repo_dir, '.dvc/cache').resolve() == repo / '.dvc/cache'
        finally:
            executor.cleanup_worktree()
    with pytest.raises(RuntimeError, match='changed'):
        prepare_submission(second_archive, repo, 'job-first')


@pytest.mark.parametrize('name', ['../outside', '/absolute', '.git/config', '.cluster-ci-local/payload'])
def test_snapshot_rejects_unsafe_archive(tmp_path, name):
    with pytest.raises(ValueError, match='Unsafe'):
        prepare_submission(archive(tmp_path, unsafe=name), tmp_path / '_local/lab/test', 'job')


def test_private_snapshots_never_execute_host_git_hooks(tmp_path, monkeypatch):
    monkeypatch.setenv('IS_LOCAL', '1')
    template = tmp_path / 'git-template'
    (template / 'hooks').mkdir(parents=True)
    marker = tmp_path / 'hook-ran'
    for name in ['post-commit', 'post-checkout']:
        hook = template / 'hooks' / name
        hook.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\n')
        hook.chmod(0o755)
    config = tmp_path / 'global.gitconfig'
    config.write_text('[init]\n\ttemplateDir = ' + str(template) + '\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(config))
    source = prepare_submission(archive(tmp_path), tmp_path / '_local/lab/test', 'job')
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source).decode().strip()
    executor = BranchExecutor('', 'job-hooks', 'runner', 'worker', source,
                              'lab/test', 'local-draft/test', start_commit=sha, docker=Mock())
    try:
        assert not marker.exists()
        assert subprocess.check_output(['git', 'config', 'core.hooksPath'], cwd=source).strip() == b'/dev/null'
    finally:
        executor.cleanup_worktree()
