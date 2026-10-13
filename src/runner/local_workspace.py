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
import urllib.error
import urllib.parse
import urllib.request
import zipfile


def node_archive_path(source, node):
    return Path(source).parent / 'nodes' / (hashlib.sha256(node.encode()).hexdigest() + '.zip')


def declared_node_outputs(repo_dir, node):
    from src.scheduler.artifact_registry import get_dag_stage_outputs
    exact, directories, _ = get_dag_stage_outputs(repo_dir=str(repo_dir))
    return sorted({p for p, info in {**exact, **directories}.items() if info.get('stage') == node})


def save_node_archive(repo_dir, node, destination):
    """Atomically retain declared outputs, including unfinished checkpoints."""
    from src.runner.dvc_git_helper import _safe_workspace_path
    import yaml
    root, destination = Path(repo_dir), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    outputs = declared_node_outputs(root, node)
    fd, temporary = tempfile.mkstemp(prefix='node-', suffix='.zip', dir=destination.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            lock = yaml.safe_load((root / 'dvc.lock').read_text()) if (root / 'dvc.lock').exists() else {}
            lock = lock or {}
            stage = (lock.get('stages') or {}).get(node)
            archive.writestr('dvc.lock', yaml.safe_dump({'schema': lock.get('schema', '2.0'),
                                                        'stages': {node: stage} if stage else {}}))
            for relative in outputs:
                source = _safe_workspace_path(root, relative)
                if source is None or Path(relative).parts[0] in ('.dvc', '.cluster-ci-local'):
                    raise ValueError('Unsafe local stage output')
                files = [source] if source.is_file() else sorted(source.rglob('*')) if source.is_dir() else []
                for file in files:
                    if file.is_symlink():
                        raise ValueError('Symlinked local stage outputs are not supported')
                    if file.is_file():
                        archive.write(file, file.relative_to(root.resolve()).as_posix())
        with open(temporary, 'rb') as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


def restore_node_archive(repo_dir, node, path):
    """Restore only this node's declared outputs and merge only its lock entry."""
    from src.runner.dvc_git_helper import _safe_workspace_path
    import yaml
    root = Path(repo_dir)
    outputs = declared_node_outputs(root, node)
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            name = member.filename
            if name == 'dvc.lock':
                continue
            if (not any(name == p or name.startswith(p.rstrip('/') + '/') for p in outputs)
                    or _safe_workspace_path(root, name) is None
                    or (member.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError('Unexpected local node archive member')
        update = yaml.safe_load(archive.read('dvc.lock')) or {}
        if set(update.get('stages') or {}) - {node}:
            raise ValueError('Node archive contains unrelated lock entries')
        for member in archive.infolist():
            if member.filename == 'dvc.lock' or member.is_dir():
                continue
            destination = _safe_workspace_path(root, member.filename)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, destination.open('wb') as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
        lock_path = root / 'dvc.lock'
        lock = yaml.safe_load(lock_path.read_text()) if lock_path.exists() else {}
        lock = lock or {'schema': '2.0', 'stages': {}}
        lock.setdefault('stages', {}).update(update.get('stages') or {})
        lock_path.write_text(yaml.safe_dump(lock))


def upload_node_archive(path, job_id, node, headnode_url, token):
    from src.runner.dvc_git_helper import _upload_file_in_chunks
    if not headnode_url or not token:
        raise RuntimeError('Authenticated cluster destination is required for local node results')
    return _upload_file_in_chunks(path, headnode_url, 'node_results', token, job_id, node_name=node)


def restore_remote_node(repo_dir, source_dir, job_id, node, headnode_url, token):
    from src.runner.cluster_http import cluster_urlopen
    path = node_archive_path(source_dir, node)
    if path.exists() and path.with_suffix('.pending').exists():
        # A failed upload must not let an older headnode copy replace a newer
        # checkpoint that is still retained on this worker.
        restore_node_archive(repo_dir, node, path)
        return
    if not headnode_url or not token:
        raise RuntimeError('Authenticated cluster destination is required for local node results')
    url = headnode_url.rstrip('/') + '/api/jobs/' + urllib.parse.quote(job_id, safe='')
    url += '/node_results/' + urllib.parse.quote(node, safe='')
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token})
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with cluster_urlopen(request, timeout=120) as response:
            fd, temporary = tempfile.mkstemp(dir=path.parent)
            try:
                with os.fdopen(fd, 'wb') as target:
                    shutil.copyfileobj(response, target, 1024 * 1024)
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        # Same-worker recovery may retain an archive that could not be uploaded.
    if path.exists():
        restore_node_archive(repo_dir, node, path)


def runner_record_path(source, runner_id):
    return Path(source).parent / ('runner-' + hashlib.sha256(runner_id.encode()).hexdigest() + '.json')


def preserve_runner_record(record_path, headnode_url, token):
    """Called after physical stop, including when the executor was SIGKILLed."""
    record_path = Path(record_path)
    if not record_path.exists():
        return
    with file_lock(str(record_path) + '.lock'):
        data = json.loads(record_path.read_text())
        archive = node_archive_path(data['source'], data['node'])
        if Path(data['workspace']).exists():
            save_node_archive(data['workspace'], data['node'], archive)
            archive.with_suffix('.pending').write_text('Upload pending\n')
        if not archive.exists():
            raise RuntimeError('Interrupted local node state could not be preserved')
        upload_node_archive(archive, data['job_id'], data['node'], headnode_url, token)
        archive.with_suffix('.pending').unlink(missing_ok=True)
        record_path.unlink()


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
