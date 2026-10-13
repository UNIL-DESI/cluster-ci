"""Private, immutable local submission snapshots (no remote Git operations)."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile


@contextmanager
def file_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def private_job_root(repo_dir, job_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', str(job_id)):
        raise ValueError('Invalid local job identity')
    return Path(repo_dir) / '.cluster-ci-local' / 'jobs' / job_id


def _git(repo, *args):
    return subprocess.run(['git', *args], cwd=repo, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def prepare_submission(archive_path, repo_dir, job_id):
    """Extract once per job, retaining ignored inputs outside the Git snapshot."""
    root = private_job_root(repo_dir, job_id)
    root.mkdir(parents=True, exist_ok=True)
    metadata = root / 'snapshot.json'
    source = root / 'source'
    h = hashlib.sha256()
    with open(archive_path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(block)
    digest = h.hexdigest()
    with file_lock(root / 'snapshot.lock'):
        if metadata.exists():
            saved = json.loads(metadata.read_text())
            if saved['archive_sha256'] != digest:
                raise RuntimeError('Local job source changed after its snapshot was frozen')
            _git(source, 'cat-file', '-e', saved['commit'] + '^{commit}')
            return str(source)
        staging = Path(tempfile.mkdtemp(prefix='source-', dir=root))
        try:
            with tarfile.open(archive_path, 'r:gz') as archive:
                members = archive.getmembers()
                for member in members:
                    parts = Path(member.name).parts
                    if (Path(member.name).is_absolute() or '..' in parts
                            or '.git' in parts or '.cluster-ci-local' in parts
                            or not (member.isfile() or member.isdir())):
                        raise ValueError('Unsafe local source archive member')
                archive.extractall(staging)
            _git(staging, 'init', '-q')
            # Host template/global hooks must not publish private snapshot data.
            _git(staging, 'config', 'core.hooksPath', '/dev/null')
            _git(staging, 'config', 'commit.gpgSign', 'false')
            _git(staging, 'add', '-A')
            _git(staging, '-c', 'user.name=cluster-ci-local', '-c',
                 'user.email=local@cluster.invalid', 'commit', '-q', '--allow-empty',
                 '-m', 'Local submission snapshot')
            commit = _git(staging, 'rev-parse', 'HEAD').decode().strip()
            tracked = set(_git(staging, 'ls-files', '-z').decode().split('\0'))
            extra = [m.name for m in members if m.isfile() and m.name not in tracked]
            cache = Path(repo_dir).resolve() / '.dvc/cache'
            cache.mkdir(parents=True, exist_ok=True)
            target_cache = staging / '.dvc/cache'
            if target_cache.exists():
                raise ValueError('Source archives must not include DVC caches')
            target_cache.parent.mkdir(parents=True, exist_ok=True)
            target_cache.symlink_to(cache, target_is_directory=True)
            os.replace(staging, source)
            temporary = metadata.with_suffix('.tmp')
            temporary.write_text(json.dumps({'archive_sha256': digest,
                                             'commit': commit, 'extra_inputs': extra}))
            os.replace(temporary, metadata)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return str(source)


def copy_extra_inputs(source, destination):
    """Copy explicitly uploaded Git-ignored inputs into each isolated worktree."""
    source, destination = Path(source), Path(destination)
    metadata = source.parent / 'snapshot.json'
    if not metadata.exists():
        return  # Direct callers may supply an already prepared Git-only checkout.
    for relative in json.loads(metadata.read_text())['extra_inputs']:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', required=True)
    parser.add_argument('--repo-dir', required=True)
    parser.add_argument('--job-id', required=True)
    args = parser.parse_args()
    print(prepare_submission(args.archive, args.repo_dir, args.job_id))
