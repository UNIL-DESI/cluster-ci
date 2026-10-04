import os
import re
import sys
import argparse
import hashlib
import json
import subprocess
import tempfile
import time
import random
import urllib.error
import urllib.request
import zipfile
import shutil
from pathlib import Path

if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

try:
    from ruamel.yaml import YAML
except ImportError:
    try:
        import yaml as PyYAML

        class YAML:
            def __init__(self, *args, **kwargs):
                self.preserve_quotes = kwargs.get("preserve_quotes", False)
            def load(self, stream):
                return PyYAML.safe_load(stream)
            def dump(self, data, stream):
                return PyYAML.dump(data, stream, sort_keys=False)
    except ImportError:
        YAML = None

def log_info(msg):
    print(f"ℹ️  [DVC-Git-Helper] {msg}")

def log_warn(msg):
    print(f"⚠️  [DVC-Git-Helper] {msg}")

def log_success(msg):
    print(f"✅ [DVC-Git-Helper] {msg}")

def _load_params(dvc_yaml_path, dvc_data):
    """Load parameters from params.yaml and any vars section in dvc.yaml.
    
    Ensures relative paths in vars are resolved relative to the directory containing dvc.yaml.
    """
    params = {}
    project_dir = os.path.dirname(dvc_yaml_path) or '.'
    
    # 1. Load params.yaml (DVC default parameter file)
    params_path = os.path.join(project_dir, "params.yaml")
    yaml = YAML()
    if os.path.exists(params_path):
        try:
            with open(params_path, "r") as f:
                loaded = yaml.load(f) or {}
                params.update(dict(loaded))
        except Exception as e:
            log_warn(f"Failed to load params.yaml: {e}")

    # 2. Load any vars files or inline dicts declared in dvc.yaml
    vars_section = dvc_data.get("vars", [])
    if isinstance(vars_section, list):
        for var_entry in vars_section:
            if isinstance(var_entry, str):
                var_path = os.path.join(project_dir, var_entry)
                if os.path.exists(var_path):
                    try:
                        with open(var_path, "r") as f:
                            loaded = yaml.load(f) or {}
                            params.update(dict(loaded))
                    except Exception as e:
                        log_warn(f"Failed to load vars file '{var_entry}': {e}")
                else:
                    log_warn(f"Vars file '{var_entry}' does not exist (resolved as '{var_path}').")
            elif isinstance(var_entry, dict):
                params.update(var_entry)
    return params

def _resolve_interpolation(value, params):
    """Resolve a ${var.path} reference using the params dict."""
    if not isinstance(value, str):
        return value
    m = re.fullmatch(r"\$\{(.+)\}", value.strip())
    if not m:
        return value
    keys = m.group(1).split(".")
    result = params
    for k in keys:
        if isinstance(result, dict) and k in result:
            result = result[k]
        else:
            return value  # unresolvable → keep raw string
    return result

def _resolve_foreach_var(text, item_val):
    """Replace ${item} (and ${item.attr} variants) with the concrete foreach value.
    
    Handles two cases:
    - ${item}: replaced with str(item_val) (works for strings and simple values)
    - ${item.attr}: when item_val is a dict, replaced with item_val[attr]
    """
    if not isinstance(text, str):
        return text
    
    def _replace_match(match):
        attr = match.group(1)  # e.g. ".safe_name" or None
        if attr and isinstance(item_val, dict):
            # ${item.safe_name} -> item_val["safe_name"]
            attr_name = attr.lstrip(".")
            return str(item_val.get(attr_name, match.group(0)))
        return str(item_val)
    
    return re.sub(r'\$\{item(\.[^}]+)?\}', _replace_match, text)

def _resolve_entries(entries, item_val):
    """Deep-clone entries list/dict, replacing ${item} with the concrete value."""
    if isinstance(entries, list):
        resolved = []
        for entry in entries:
            if isinstance(entry, str):
                resolved.append(_resolve_foreach_var(entry, item_val))
            elif isinstance(entry, dict):
                resolved.append({
                    _resolve_foreach_var(k, item_val): v
                    for k, v in entry.items()
                })
            else:
                resolved.append(entry)
        return resolved
    elif isinstance(entries, dict):
        return {
            _resolve_foreach_var(k, item_val): v
            for k, v in entries.items()
        }
    return entries

def inject_cache_false(dvc_yaml_path):
    if not os.path.exists(dvc_yaml_path):
        log_info(f"{dvc_yaml_path} not found, skipping injection.")
        return

    yaml = YAML()
    yaml.preserve_quotes = True
    with open(dvc_yaml_path, 'r') as f:
        data = yaml.load(f)

    if not data:
        log_info("Empty dvc.yaml.")
        return

    modified = False

    def process_entries(entries, container, key_in_container):
        nonlocal modified
        if isinstance(entries, list):
            for i, entry in enumerate(entries):
                if isinstance(entry, str):
                    container[key_in_container][i] = {entry: {'cache': False}}
                    modified = True
                elif isinstance(entry, dict):
                    for filename, config in entry.items():
                        if isinstance(config, dict):
                            if config.get('cache') is not False:
                                config['cache'] = False
                                modified = True
                        else:
                            entry[filename] = {'cache': False}
                            modified = True
        elif isinstance(entries, dict):
            for filename, config in entries.items():
                if isinstance(config, dict):
                    if config.get('cache') is not False:
                        config['cache'] = False
                        modified = True
                else:
                    entries[filename] = {'cache': False}
                    modified = True

    # Process stages
    if 'stages' in data:
        for stage_name, stage in data['stages'].items():
            # Direct stage metrics/plots
            for key in ['metrics', 'plots']:
                if key in stage:
                    process_entries(stage[key], stage, key)
            # Foreach/do stage metrics/plots
            do_block = stage.get('do', {})
            if isinstance(do_block, dict):
                for key in ['metrics', 'plots']:
                    if key in do_block:
                        process_entries(do_block[key], do_block, key)

    # Process top-level metrics and plots
    for key in ['metrics', 'plots']:
        if key in data:
            process_entries(data[key], data, key)

    if modified:
        with open(dvc_yaml_path, 'w') as f:
            yaml.dump(data, f)
        log_success(f"Injected 'cache: false' into {dvc_yaml_path} metrics/plots.")
    else:
        log_info("No changes needed in dvc.yaml.")

def get_cache_false_paths(dvc_yaml_path):
    if not os.path.exists(dvc_yaml_path):
        return []

    yaml = YAML()
    with open(dvc_yaml_path, 'r') as f:
        data = yaml.load(f)

    paths = set()
    if not data:
        return []

    params = _load_params(dvc_yaml_path, data or {})

    def extract_from_entries(entries, wdir='.'):
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    for path, config in entry.items():
                        if isinstance(config, dict) and config.get('cache') is False:
                            full_path = os.path.join(wdir, path) if wdir != '.' else path
                            paths.add(Path(full_path).as_posix())
        elif isinstance(entries, dict):
            for path, config in entries.items():
                if isinstance(config, dict) and config.get('cache') is False:
                    full_path = os.path.join(wdir, path) if wdir != '.' else path
                    paths.add(Path(full_path).as_posix())

    if 'stages' in data:
        for stage in data['stages'].values():
            wdir = stage.get('wdir', '.')
            foreach_items = stage.get('foreach', None)
            do_block = stage.get('do', {})

            # Direct stage metrics/plots
            for key in ['metrics', 'plots']:
                if key in stage:
                    extract_from_entries(stage[key], wdir)

            # Foreach/do stage: resolve ${item} for each foreach value
            if foreach_items and isinstance(do_block, dict):
                # Try to resolve foreach variable reference if it is a string template
                if isinstance(foreach_items, str):
                    resolved_items = _resolve_interpolation(foreach_items, params)
                    if resolved_items == foreach_items:
                        log_warn(f"Could not resolve foreach variable reference '{foreach_items}'")
                    foreach_items = resolved_items

                # Only iterate if foreach_items is actually resolved to a list/dict, and NOT a string
                if foreach_items and not isinstance(foreach_items, str):
                    iterable = list(foreach_items.keys()) if isinstance(foreach_items, dict) else foreach_items
                    for item_val in iterable:
                        do_wdir = do_block.get('wdir', wdir)
                        for key in ['metrics', 'plots']:
                            if key in do_block:
                                resolved = _resolve_entries(do_block[key], item_val)
                                extract_from_entries(resolved, do_wdir)

    for key in ['metrics', 'plots']:
        if key in data:
            extract_from_entries(data[key])

    return list(paths)


def get_dvc_out_paths(dvc_lock_path='dvc.lock'):
    """Return repository-relative output paths recorded in dvc.lock."""
    if not os.path.exists(dvc_lock_path):
        return []

    yaml = YAML()
    with open(dvc_lock_path, 'r') as f:
        data = yaml.load(f) or {}

    paths = set()
    for stage in (data.get('stages') or {}).values():
        for entry in stage.get('outs') or []:
            path = entry.get('path') if isinstance(entry, dict) else entry
            if path:
                path = Path(str(path)).as_posix()
                if path != '.dvc-viewer' and not path.startswith('.dvc-viewer/'):
                    paths.add(path)
    return sorted(paths)


def _safe_workspace_path(project_root, relative_path):
    """Resolve a declared result path without allowing it to leave the workspace."""
    path = Path(str(relative_path))
    protected = {'.git', '.env', '.cluster-ci', '.cluster-ci-run.json', '.cluster-ci-logs'}
    if (
        path.is_absolute()
        or '..' in path.parts
        or (path.parts and (path.parts[0] in protected or path.parts[0].startswith('.env.')))
    ):
        return None
    root = Path(project_root).resolve()
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def build_local_results_archive(project_root='.'):
    """Create a ZIP containing declared DVC outputs and small result metadata."""
    project_root = Path(project_root).resolve()
    candidates = set(get_dvc_out_paths(project_root / 'dvc.lock'))
    candidates.update(get_cache_false_paths(project_root / 'dvc.yaml'))
    if (project_root / 'dvc.lock').is_file():
        candidates.add('dvc.lock')

    fd, archive_path = tempfile.mkstemp(prefix='cluster-ci-results-', suffix='.zip')
    os.close(fd)
    archived_files = set()

    try:
        with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED,
                             compresslevel=1, allowZip64=True) as archive:
            for relative_path in sorted(candidates):
                source = _safe_workspace_path(project_root, relative_path)
                if source is None:
                    log_warn(f"Skipping unsafe result path: {relative_path}")
                    continue
                if source.is_symlink():
                    log_warn(f"Skipping symlinked result path: {relative_path}")
                    continue
                if source.is_file():
                    arcname = source.relative_to(project_root).as_posix()
                    if arcname not in archived_files:
                        archive.write(source, arcname)
                        archived_files.add(arcname)
                elif source.is_dir():
                    for child in sorted(source.rglob('*')):
                        if child.is_symlink() or not child.is_file():
                            continue
                        arcname = child.relative_to(project_root).as_posix()
                        if arcname not in archived_files:
                            archive.write(child, arcname)
                            archived_files.add(arcname)

        if not archived_files:
            os.remove(archive_path)
            return None, []

        return archive_path, sorted(archived_files)
    except Exception:
        if os.path.exists(archive_path):
            os.remove(archive_path)
        raise


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _delete_local_transfer(headnode_url, transfer_id, cluster_token=None):
    req = urllib.request.Request(
        f"{headnode_url.rstrip('/')}/api/local_transfers/{transfer_id}", method='DELETE'
    )
    if cluster_token:
        req.add_header('Authorization', f'Bearer {cluster_token}')
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except Exception:
        pass


def _upload_file_in_chunks(path, headnode_url, purpose, cluster_token=None, job_id=None):
    total_size = os.path.getsize(path)
    create_payload = {
        'purpose': purpose,
        'total_size': total_size,
        'sha256': _sha256_file(path),
    }
    if job_id:
        create_payload['job_id'] = job_id
    create_req = urllib.request.Request(
        f"{headnode_url.rstrip('/')}/api/local_transfers",
        data=json.dumps(create_payload).encode('utf-8'),
        method='POST',
        headers={'Content-Type': 'application/json'},
    )
    if cluster_token:
        create_req.add_header('Authorization', f'Bearer {cluster_token}')
    with urllib.request.urlopen(create_req, timeout=30) as response:
        transfer = json.loads(response.read().decode('utf-8'))

    transfer_id = transfer['transfer_id']
    chunk_size = int(transfer['chunk_size'])
    chunk_count = (total_size + chunk_size - 1) // chunk_size
    try:
        with open(path, 'rb') as source:
            for chunk_index in range(chunk_count):
                chunk = source.read(chunk_size)
                chunk_hash = hashlib.sha256(chunk).hexdigest()
                chunk_url = (
                    f"{headnode_url.rstrip('/')}/api/local_transfers/"
                    f"{transfer_id}/chunks/{chunk_index}"
                )
                for attempt in range(1, 4):
                    try:
                        chunk_req = urllib.request.Request(
                            chunk_url,
                            data=chunk,
                            method='PUT',
                            headers={
                                'Content-Type': 'application/octet-stream',
                                'X-Chunk-SHA256': chunk_hash,
                            },
                        )
                        if cluster_token:
                            chunk_req.add_header('Authorization', f'Bearer {cluster_token}')
                        with urllib.request.urlopen(chunk_req, timeout=120):
                            pass
                        break
                    except Exception:
                        if attempt == 3:
                            raise
                        time.sleep(attempt)
                log_info(
                    f"Uploaded chunk {chunk_index + 1}/{chunk_count} "
                    f"({min((chunk_index + 1) * chunk_size, total_size) / (1024 * 1024):.1f}/"
                    f"{total_size / (1024 * 1024):.1f} MB)"
                )

        complete_req = urllib.request.Request(
            f"{headnode_url.rstrip('/')}/api/local_transfers/{transfer_id}/complete",
            data=b'{}',
            method='POST',
            headers={'Content-Type': 'application/json'},
        )
        if cluster_token:
            complete_req.add_header('Authorization', f'Bearer {cluster_token}')
        with urllib.request.urlopen(complete_req, timeout=600) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception:
        _delete_local_transfer(headnode_url, transfer_id, cluster_token)
        raise


def sync_local_results_archive():
    """Return declared DVC outputs to the headnode for a local-mode job."""
    if os.environ.get('IS_LOCAL') != '1':
        log_info("Not a local-mode job; skipping local result archive upload.")
        return True

    headnode_url = os.environ.get('HEADNODE_URL')
    job_id = os.environ.get('JOB_ID')
    cluster_token = os.environ.get('CLUSTER_TOKEN')
    if not headnode_url or not job_id:
        raise RuntimeError("HEADNODE_URL or JOB_ID is missing; cannot upload local results")

    archive_path, archived_files = build_local_results_archive('.')
    if not archive_path:
        log_info("No declared local result files were produced.")
        return True

    archive_size = os.path.getsize(archive_path)

    try:
        log_info(
            f"Uploading {len(archived_files)} declared result file(s) "
            f"({archive_size / (1024 * 1024):.1f} MB) to the headnode..."
        )
        response_data = _upload_file_in_chunks(
            archive_path,
            headnode_url,
            purpose='results',
            cluster_token=cluster_token,
            job_id=job_id,
        )
        if response_data.get('archived_files') != len(archived_files):
            raise RuntimeError(
                "Headnode accepted the archive but reported an unexpected file count"
            )
        log_success("Declared local results synchronized to the headnode.")
        return True
    finally:
        if os.path.exists(archive_path):
            os.remove(archive_path)

def _sync_metrics_http():
    """Upload metrics/plots to headnode via HTTP (local mode)."""
    headnode_url = os.environ.get("HEADNODE_URL")
    job_id = os.environ.get("JOB_ID")
    cluster_token = os.environ.get("CLUSTER_TOKEN")

    if not headnode_url or not job_id:
        log_warn("IS_LOCAL=1 but HEADNODE_URL or JOB_ID is missing in environment. Cannot sync metrics via HTTP.")
        return

    dvc_yaml_path = 'dvc.yaml'
    paths = get_cache_false_paths(dvc_yaml_path)

    files_to_upload = {}
    # Include dvc.lock if it exists
    if os.path.exists('dvc.lock'):
        files_to_upload['dvc.lock'] = 'dvc.lock'

    # Include metrics/plots < 5 MB
    for path in paths:
        if os.path.isfile(path):
            size_mb = os.path.getsize(path) / (1024 * 1024)
            if size_mb < 5:
                files_to_upload[path] = path
            else:
                log_warn(f"Skipping {path} ({size_mb:.1f} MB > 5 MB limit)")

    if not files_to_upload:
        log_info("No metrics or dvc.lock to sync in local mode.")
        return

    import uuid
    import urllib.request
    import urllib.error

    boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
    body = bytearray()

    for field_name, file_path in files_to_upload.items():
        try:
            with open(file_path, 'rb') as f:
                content = f.read()
            body.extend(f"--{boundary}\r\n".encode('utf-8'))
            body.extend(f'Content-Disposition: form-data; name="files"; filename="{field_name}"\r\n'.encode('utf-8'))
            body.extend(b'Content-Type: application/octet-stream\r\n\r\n')
            body.extend(content)
            body.extend(b'\r\n')
        except Exception as e:
            log_warn(f"Failed to read file '{file_path}' for HTTP sync: {e}")

    body.extend(f"--{boundary}--\r\n".encode('utf-8'))

    url = f"{headnode_url.rstrip('/')}/api/jobs/{job_id}/sync_results"
    headers = {
        'Content-Type': f'multipart/form-data; boundary={boundary}',
        'Content-Length': str(len(body))
    }
    if cluster_token:
        headers['Authorization'] = f'Bearer {cluster_token}'

    req = urllib.request.Request(url, data=bytes(body), headers=headers, method='POST')

    try:
        log_info(f"Posting {len(files_to_upload)} metric/lock file(s) to {url}...")
        with urllib.request.urlopen(req, timeout=60) as response:
            payload = response.read()
            if response.status in (200, 201):
                response_data = json.loads(payload.decode('utf-8'))
                synced_files = response_data.get('synced_files')
                if synced_files != len(files_to_upload):
                    raise RuntimeError(
                        f"Headnode saved {synced_files} of {len(files_to_upload)} metric/lock files"
                    )
                log_success(f"Metrics successfully synced to headnode via HTTP (Status {response.status}).")
            else:
                log_warn(f"HTTP sync metrics returned unexpected status: {response.status}")
    except urllib.error.HTTPError as e:
        log_warn(f"HTTP error during metrics sync ({e.code}): {e.reason}")
        try:
            err_body = e.read().decode('utf-8', errors='ignore')
            log_warn(f"Response body: {err_body}")
        except Exception:
            pass
    except urllib.error.URLError as e:
        log_warn(f"URL error during metrics sync: {e.reason}")
        log_warn("Please verify HEADNODE_URL reachability from inside the execution container.")
    except Exception as e:
        log_warn(f"Unexpected error during HTTP metrics sync: {e}")

def _get_git_env():
    env = os.environ.copy()
    cluster_ci_root = str(Path(__file__).resolve().parent.parent.parent)
    current_pypath = env.get("PYTHONPATH", "")
    if cluster_ci_root not in current_pypath.split(os.pathsep):
        env["PYTHONPATH"] = f"{cluster_ci_root}{os.pathsep}{current_pypath}" if current_pypath else cluster_ci_root
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env

def _get_current_branch(cwd=None):
    try:
        res = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace'
        )
        branch = res.stdout.strip() if (res.returncode == 0 and isinstance(res.stdout, str)) else ""
        if branch == "HEAD" or not branch:
            return os.environ.get("TARGET_BRANCH", "main")
        return branch
    except Exception:
        return os.environ.get("TARGET_BRANCH", "main")

def install_dvc_lock_merge_driver(repo_path=None):
    """Install the dvc.lock merge driver in .git/info/attributes and local git config.
    
    Safe, idempotent, and does NOT modify user tracked repository files (.gitattributes).
    """
    cwd = repo_path or os.getcwd()
    try:
        res = subprocess.run(
            ['git', 'rev-parse', '--git-path', 'info/attributes'],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace'
        )
        if res.returncode == 0 and res.stdout and isinstance(res.stdout, str):
            attr_path = res.stdout.strip()
            if not os.path.isabs(attr_path):
                attr_path = os.path.join(cwd, attr_path)
        else:
            attr_path = os.path.join(cwd, '.git', 'info', 'attributes')
    except Exception:
        attr_path = os.path.join(cwd, '.git', 'info', 'attributes')

    try:
        attr_dir = os.path.dirname(attr_path)
        if attr_dir:
            os.makedirs(attr_dir, exist_ok=True)

        pattern = "dvc.lock merge=dvclock"
        already_configured = False
        if os.path.exists(attr_path):
            try:
                with open(attr_path, 'r', encoding='utf-8', errors='replace') as f:
                    for line in f:
                        if line.strip() == pattern:
                            already_configured = True
                            break
            except Exception:
                pass

        if not already_configured:
            prefix_nl = False
            if os.path.exists(attr_path) and os.path.getsize(attr_path) > 0:
                try:
                    with open(attr_path, 'rb') as f:
                        f.seek(-1, os.SEEK_END)
                        if f.read(1) != b'\n':
                            prefix_nl = True
                except Exception:
                    pass
            with open(attr_path, 'a', encoding='utf-8') as f:
                if prefix_nl:
                    f.write("\n")
                f.write(f"{pattern}\n")
            log_info(f"Registered merge attribute in {attr_path}")
    except Exception as e:
        log_warn(f"Could not write merge attribute: {e}")

    try:
        py_exec = sys.executable.replace("\\", "/") if sys.platform.startswith("win") else sys.executable
        driver_script = Path(__file__).resolve().with_name("dvc_lock_merge.py")
        driver_file = str(driver_script).replace("\\", "/") if sys.platform.startswith("win") else str(driver_script)
        driver_cmd = f'"{py_exec}" "{driver_file}" %O %A %B'
        subprocess.run(['git', 'config', 'merge.dvclock.name', 'DVC lock 3-way merge driver'], cwd=cwd, check=False)
        subprocess.run(['git', 'config', 'merge.dvclock.driver', driver_cmd], cwd=cwd, check=False)
        log_info(f"Configured merge.dvclock driver in local git config: {driver_cmd}")
    except Exception as e:
        log_warn(f"Could not configure merge driver in git config: {e}")

def _get_start_commit(cwd=None):
    """Retrieve the job starting commit hash.
    
    Checks in order:
    1. .cluster-ci-start-commit file
    2. .cluster-ci-commit file
    3. CALLER_COMMIT_SHA / JOB_START_COMMIT environment variable
    4. git rev-parse HEAD
    """
    cwd = cwd or os.getcwd()
    start_file = os.path.join(cwd, ".cluster-ci-start-commit")
    if os.path.isfile(start_file):
        try:
            with open(start_file, "r", encoding="utf-8") as f:
                c = f.read().strip()
                if c:
                    return c
        except Exception:
            pass

    commit_file = os.path.join(cwd, ".cluster-ci-commit")
    if os.path.isfile(commit_file):
        try:
            with open(commit_file, "r", encoding="utf-8") as f:
                c = f.read().strip()
                if c:
                    return c
        except Exception:
            pass

    env_sha = os.environ.get("CALLER_COMMIT_SHA") or os.environ.get("JOB_START_COMMIT")
    if env_sha and env_sha.strip():
        return env_sha.strip()

    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8"
        )
        sha = res.stdout.strip() if (res.returncode == 0 and isinstance(res.stdout, str)) else ""
        if sha and len(sha) >= 7:
            return sha
    except Exception:
        pass

    return "HEAD"


def get_allowed_sync_paths(repo_path=None, start_commit=None):
    """Return the set of repository-relative paths allowed for synchronization.
    
    In accordance with Henri's architecture:
    Allowed paths are strictly deduced from dvc.yaml and dvc.lock OF THE STARTING COMMIT:
      - dvc.lock (always permitted)
      - stage outs declared in dvc.yaml (outs, metrics, plots)
      - outs recorded in dvc.lock
    Code files, params files, and dvc.yaml are strictly excluded unless declared in outs.
    """
    cwd = repo_path or os.getcwd()
    allowed = {"dvc.lock"}
    start_commit = start_commit or _get_start_commit(cwd)

    # 1. Read dvc.yaml at start_commit (or local if unavailable)
    dvc_yaml_content = None
    if start_commit and start_commit != "HEAD":
        res = subprocess.run(
            ["git", "show", f"{start_commit}:dvc.yaml"],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        if res.returncode == 0 and isinstance(res.stdout, str):
            dvc_yaml_content = res.stdout

    if not dvc_yaml_content:
        local_yaml = os.path.join(cwd, "dvc.yaml")
        if os.path.isfile(local_yaml):
            try:
                with open(local_yaml, "r", encoding="utf-8", errors="replace") as f:
                    dvc_yaml_content = f.read()
            except Exception:
                pass

    if dvc_yaml_content and isinstance(dvc_yaml_content, str):
        yaml = YAML() if YAML else None
        if yaml:
            try:
                data = yaml.load(dvc_yaml_content) or {}
                stages = data.get("stages") or {}
                if isinstance(stages, dict):
                    for stage_val in stages.values():
                        if isinstance(stage_val, dict):
                            wdir = stage_val.get("wdir", ".")
                            for k in ["outs", "metrics", "plots"]:
                                if k in stage_val:
                                    _collect_entries(stage_val[k], wdir, allowed)
                            do_blk = stage_val.get("do") or {}
                            if isinstance(do_blk, dict):
                                do_wdir = do_blk.get("wdir", wdir)
                                for k in ["outs", "metrics", "plots"]:
                                    if k in do_blk:
                                        _collect_entries(do_blk[k], do_wdir, allowed)
                for k in ["outs", "metrics", "plots"]:
                    if k in data:
                        _collect_entries(data[k], ".", allowed)
            except Exception as e:
                log_warn(f"Failed to parse dvc.yaml for allowed paths: {e}")

    # 2. Read dvc.lock at start_commit (or local)
    dvc_lock_content = None
    if start_commit and start_commit != "HEAD":
        res = subprocess.run(
            ["git", "show", f"{start_commit}:dvc.lock"],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        if res.returncode == 0 and isinstance(res.stdout, str):
            dvc_lock_content = res.stdout

    if not dvc_lock_content:
        local_lock = os.path.join(cwd, "dvc.lock")
        if os.path.isfile(local_lock):
            try:
                with open(local_lock, "r", encoding="utf-8", errors="replace") as f:
                    dvc_lock_content = f.read()
            except Exception:
                pass

    if dvc_lock_content and isinstance(dvc_lock_content, str):
        yaml = YAML() if YAML else None
        if yaml:
            try:
                lock_data = yaml.load(dvc_lock_content) or {}
                for stage in (lock_data.get("stages") or {}).values():
                    if isinstance(stage, dict):
                        for entry in stage.get("outs") or []:
                            p = entry.get("path") if isinstance(entry, dict) else entry
                            if p:
                                p_str = Path(str(p)).as_posix().lstrip("./")
                                if p_str != ".dvc-viewer" and not p_str.startswith(".dvc-viewer/"):
                                    allowed.add(p_str)
            except Exception as e:
                log_warn(f"Failed to parse dvc.lock for allowed paths: {e}")

    return allowed


def _collect_entries(entries, wdir, out_set):
    if isinstance(entries, list):
        for e in entries:
            _collect_entry(e, wdir, out_set)
    elif isinstance(entries, dict):
        for k in entries.keys():
            _collect_entry(k, wdir, out_set)
    elif isinstance(entries, str):
        _collect_entry(entries, wdir, out_set)


def _collect_entry(entry, wdir, out_set):
    if isinstance(entry, dict):
        for k in entry.keys():
            _collect_entry(k, wdir, out_set)
    elif isinstance(entry, str):
        p = entry.strip()
        if p and not p.startswith("${"):
            full = os.path.join(wdir, p) if wdir != "." else p
            norm = Path(full).as_posix().lstrip("./")
            if norm:
                out_set.add(norm)


def is_path_allowed(file_path, allowed_paths):
    """Check if file_path is covered by allowed_paths."""
    norm = Path(file_path).as_posix().lstrip("./")
    if norm == "dvc.lock":
        return True
    for ap in allowed_paths:
        ap_norm = Path(ap).as_posix().lstrip("./")
        if norm == ap_norm or norm.startswith(ap_norm + "/"):
            return True
    return False


def _sync_dvc_lock(cwd, remote_ref, start_commit, env):
    """3-way merge or checkout dvc.lock from remote_ref."""
    local_lock_path = os.path.join(cwd, "dvc.lock")
    base_res = subprocess.run(['git', 'merge-base', 'HEAD', remote_ref], cwd=cwd, capture_output=True, text=True, env=env)
    merge_base = base_res.stdout.strip() if base_res.returncode == 0 and base_res.stdout.strip() else None

    local_modified = False
    remote_modified = False
    base_content = None

    if merge_base:
        show_base = subprocess.run(['git', 'show', f'{merge_base}:dvc.lock'], cwd=cwd, capture_output=True, text=True, env=env)
        if show_base.returncode == 0 and isinstance(show_base.stdout, str):
            base_content = show_base.stdout

    show_remote = subprocess.run(['git', 'show', f'{remote_ref}:dvc.lock'], cwd=cwd, capture_output=True, text=True, env=env)
    remote_content = show_remote.stdout if (show_remote.returncode == 0 and isinstance(show_remote.stdout, str)) else None

    if base_content is not None and remote_content is not None:
        if base_content.strip() != remote_content.strip():
            remote_modified = True

    if os.path.isfile(local_lock_path):
        with open(local_lock_path, "r", encoding="utf-8", errors="replace") as f:
            local_content = f.read()
        if base_content is not None:
            if local_content.strip() != base_content.strip():
                local_modified = True
        else:
            local_modified = True
    else:
        local_content = None

    if local_modified and remote_modified and base_content is not None and remote_content is not None and local_content is not None:
        yaml = YAML() if YAML else None
        from src.runner.dvc_lock_merge import merge_dvc_lock_data
        base_data = yaml.load(base_content) if (yaml and isinstance(base_content, str)) else {}
        local_data = yaml.load(local_content) if (yaml and isinstance(local_content, str)) else {}
        remote_data = yaml.load(remote_content) if (yaml and isinstance(remote_content, str)) else {}
        merged_data = merge_dvc_lock_data(base_data, local_data, remote_data)
        with open(local_lock_path, "w", encoding="utf-8") as f:
            yaml.dump(merged_data, f)
        log_info("Successfully 3-way merged dvc.lock from remote.")
    else:
        chk_res = subprocess.run(['git', 'checkout', remote_ref, '--', 'dvc.lock'], cwd=cwd, capture_output=True, text=True, env=env)
        if chk_res.returncode != 0:
            raise RuntimeError(f"sync_before_node: failed to checkout dvc.lock from {remote_ref}: {chk_res.stderr.strip()}")
        log_info("Restored dvc.lock from remote.")


def push_with_retries(
    current_branch=None,
    max_retries=10,
    base_delay=0.5,
    max_delay=10.0,
    cwd=None,
    files_to_commit=None,
    commit_msg=None,
    start_commit=None,
):
    """Push local commits/outputs to origin with retry loop and remote-based commit.
    
    Creates a commit on top of origin/<current_branch> containing ONLY the allowed
    outputs (dvc.lock, metrics, plots, outs), preserving any human commits on the
    remote tip without moving local HEAD or overwriting human code.
    Fails loudly if reconciliation fails or max retries are exceeded.
    """
    cwd = cwd or os.getcwd()
    install_dvc_lock_merge_driver(repo_path=cwd)
    env = _get_git_env()

    if not current_branch:
        current_branch = _get_current_branch(cwd)

    start_commit = start_commit or _get_start_commit(cwd)
    allowed_paths = get_allowed_sync_paths(cwd, start_commit=start_commit)

    # 1. Determine target output files
    if files_to_commit is not None:
        target_files = [Path(f).as_posix().lstrip("./") for f in files_to_commit if is_path_allowed(f, allowed_paths)]
    else:
        candidates = set()
        # Staged files
        res_staged = subprocess.run(['git', 'diff', '--cached', '--name-only'], cwd=cwd, capture_output=True, text=True, env=env)
        if res_staged.returncode == 0:
            for line in res_staged.stdout.splitlines():
                if line.strip():
                    candidates.add(Path(line.strip()).as_posix().lstrip("./"))
        # Unstaged modified files
        res_unstaged = subprocess.run(['git', 'diff', '--name-only'], cwd=cwd, capture_output=True, text=True, env=env)
        if res_unstaged.returncode == 0:
            for line in res_unstaged.stdout.splitlines():
                if line.strip():
                    candidates.add(Path(line.strip()).as_posix().lstrip("./"))
        # Check dvc.lock
        if os.path.exists(os.path.join(cwd, "dvc.lock")):
            candidates.add("dvc.lock")
        # Check local commits ahead of remote / merge-base (e.g. test environment)
        try:
            head_diff = subprocess.run(['git', 'diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD'], cwd=cwd, capture_output=True, text=True, env=env)
            if head_diff.returncode == 0:
                for line in head_diff.stdout.splitlines():
                    if line.strip():
                        candidates.add(Path(line.strip()).as_posix().lstrip("./"))
        except Exception:
            pass

        target_files = [f for f in sorted(candidates) if is_path_allowed(f, allowed_paths)]

    if not target_files:
        log_info("No output files to commit or push.")
        return True

    if not commit_msg:
        if "dvc.lock" in target_files and len(target_files) > 1:
            commit_msg = "chore(ci): auto-sync metrics and dvc.lock [skip ci]"
        elif "dvc.lock" in target_files:
            commit_msg = "chore(ci): auto-sync dvc.lock [skip ci]"
        else:
            commit_msg = "chore(ci): auto-sync metrics [skip ci]"

    for attempt in range(1, max_retries + 1):
        log_info(f"Push attempt {attempt}/{max_retries} to origin/{current_branch} (files: {target_files})...")

        # 1. Fetch remote branch
        res_fetch = subprocess.run(
            ['git', 'fetch', 'origin', current_branch],
            cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60, env=env
        )
        if res_fetch.returncode != 0:
            err = res_fetch.stderr.strip() if res_fetch.stderr else "git fetch failed"
            log_warn(f"Fetch failed on attempt {attempt}: {err}")
            if attempt == max_retries:
                raise RuntimeError(f"Failed to push to origin/{current_branch}: git fetch failed: {err}")
            backoff = min(base_delay * (2 ** (attempt - 1)), max_delay) + random.uniform(0.1, 0.5)
            time.sleep(backoff)
            continue

        remote_ref = f"origin/{current_branch}"
        verify_res = subprocess.run(['git', 'rev-parse', '--verify', remote_ref], cwd=cwd, capture_output=True, text=True, env=env)
        if verify_res.returncode != 0:
            # Remote branch does not exist yet -> initial push of current commit
            res_push = subprocess.run(
                ['git', 'push', 'origin', f'HEAD:refs/heads/{current_branch}'],
                cwd=cwd, capture_output=True, text=True, timeout=60, env=env
            )
            if res_push.returncode == 0:
                log_success(f"Initial push succeeded to origin/{current_branch}.")
                return True
            else:
                err = res_push.stderr.strip() if res_push.stderr else "Push failed"
                log_warn(f"Initial push failed: {err}")
                if attempt == max_retries:
                    raise RuntimeError(f"Failed initial push to origin/{current_branch}: {err}")
                backoff = min(base_delay * (2 ** (attempt - 1)), max_delay) + random.uniform(0.1, 0.5)
                time.sleep(backoff)
                continue

        remote_tip = verify_res.stdout.strip() if isinstance(verify_res.stdout, str) else ""
        if not remote_tip:
            remote_tip = "HEAD"

        # 2. 3-way merge dvc.lock if needed
        merged_lock_path = None
        if "dvc.lock" in target_files and os.path.isfile(os.path.join(cwd, "dvc.lock")):
            base_res = subprocess.run(['git', 'merge-base', remote_tip, start_commit or 'HEAD'], cwd=cwd, capture_output=True, text=True, env=env)
            merge_base = base_res.stdout.strip() if (base_res.returncode == 0 and isinstance(base_res.stdout, str) and base_res.stdout.strip()) else None

            base_lock = None
            if merge_base:
                sb = subprocess.run(['git', 'show', f'{merge_base}:dvc.lock'], cwd=cwd, capture_output=True, text=True, env=env)
                if sb.returncode == 0 and isinstance(sb.stdout, str):
                    base_lock = sb.stdout

            remote_lock = None
            sr = subprocess.run(['git', 'show', f'{remote_tip}:dvc.lock'], cwd=cwd, capture_output=True, text=True, env=env)
            if sr.returncode == 0 and isinstance(sr.stdout, str):
                remote_lock = sr.stdout

            with open(os.path.join(cwd, "dvc.lock"), "r", encoding="utf-8", errors="replace") as f:
                local_lock = f.read()

            remote_mod = (base_lock is not None and remote_lock is not None and isinstance(base_lock, str) and isinstance(remote_lock, str) and base_lock.strip() != remote_lock.strip())
            local_mod = (base_lock is not None and isinstance(base_lock, str) and base_lock.strip() != local_lock.strip()) if base_lock is not None else True

            if remote_mod and local_mod and base_lock is not None and remote_lock is not None:
                yaml = YAML() if YAML else None
                from src.runner.dvc_lock_merge import merge_dvc_lock_data, MergeConflictError
                try:
                    base_data = yaml.load(base_lock) if (yaml and isinstance(base_lock, str)) else {}
                    local_data = yaml.load(local_lock) if (yaml and isinstance(local_lock, str)) else {}
                    remote_data = yaml.load(remote_lock) if (yaml and isinstance(remote_lock, str)) else {}
                    merged_data = merge_dvc_lock_data(base_data, local_data, remote_data)

                    fd, merged_lock_path = tempfile.mkstemp(prefix="merged_dvc_lock_")
                    os.close(fd)
                    with open(merged_lock_path, "w", encoding="utf-8") as mf:
                        yaml.dump(merged_data, mf)
                    with open(os.path.join(cwd, "dvc.lock"), "w", encoding="utf-8") as lf:
                        yaml.dump(merged_data, lf)
                    log_info("Successfully 3-way merged dvc.lock for push.")
                except MergeConflictError as mce:
                    log_warn(f"Unresolvable conflict merging dvc.lock: {mce}")
                    raise RuntimeError(f"Reconciliation failed during push on branch '{current_branch}'. Unresolvable conflict encountered: {mce}")
                except Exception as exc:
                    log_warn(f"Failed to 3-way merge dvc.lock: {exc}")
                    raise RuntimeError(f"Reconciliation failed during push on branch '{current_branch}': {exc}")

        # 3. Build tree and commit based on remote_tip using temporary index
        fd_idx, temp_index = tempfile.mkstemp(prefix="cluster_git_idx_")
        os.close(fd_idx)

        try:
            temp_env = env.copy()
            temp_env["GIT_INDEX_FILE"] = temp_index

            subprocess.run(["git", "read-tree", remote_tip], cwd=cwd, env=temp_env, check=True, capture_output=True)

            for f in target_files:
                norm_f = Path(f).as_posix().lstrip("./")
                full_f = os.path.join(cwd, norm_f)
                if norm_f == "dvc.lock" and merged_lock_path and os.path.isfile(merged_lock_path):
                    res_hash = subprocess.run(
                        ["git", "hash-object", "-w", merged_lock_path],
                        cwd=cwd, env=temp_env, capture_output=True, text=True
                    )
                    obj_sha = res_hash.stdout.strip() if isinstance(res_hash.stdout, str) else ""
                    if obj_sha:
                        subprocess.run(
                            ["git", "update-index", "--add", "--cacheinfo", "100644", obj_sha, "dvc.lock"],
                            cwd=cwd, env=temp_env, check=True, capture_output=True
                        )
                elif os.path.isfile(full_f):
                    res_hash = subprocess.run(
                        ["git", "hash-object", "-w", full_f],
                        cwd=cwd, env=temp_env, capture_output=True, text=True
                    )
                    obj_sha = res_hash.stdout.strip() if isinstance(res_hash.stdout, str) else ""
                    if obj_sha:
                        mode = "100755" if os.access(full_f, os.X_OK) else "100644"
                        subprocess.run(
                            ["git", "update-index", "--add", "--cacheinfo", mode, obj_sha, norm_f],
                            cwd=cwd, env=temp_env, check=True, capture_output=True
                        )
                elif not os.path.exists(full_f):
                    subprocess.run(
                        ["git", "update-index", "--force-remove", norm_f],
                        cwd=cwd, env=temp_env, check=False, capture_output=True
                    )

            tree_res = subprocess.run(["git", "write-tree"], cwd=cwd, env=temp_env, capture_output=True, text=True)
            if tree_res.returncode != 0:
                raise RuntimeError(f"git write-tree failed: {tree_res.stderr.strip()}")
            new_tree = tree_res.stdout.strip() if isinstance(tree_res.stdout, str) else ""

            res_tree = subprocess.run(
                ["git", "rev-parse", f"{remote_tip}^{{tree}}"],
                cwd=cwd, env=temp_env, capture_output=True, text=True
            )
            remote_tree = res_tree.stdout.strip() if isinstance(res_tree.stdout, str) else ""

            if new_tree and remote_tree and new_tree == remote_tree:
                log_info("Tree identical to remote tip, no new commit needed.")
                return True

            temp_env["GIT_AUTHOR_NAME"] = "cluster-ci-bot"
            temp_env["GIT_AUTHOR_EMAIL"] = "bot@cluster-ci.io"
            temp_env["GIT_COMMITTER_NAME"] = "cluster-ci-bot"
            temp_env["GIT_COMMITTER_EMAIL"] = "bot@cluster-ci.io"

            commit_res = subprocess.run(
                ["git", "commit-tree", new_tree, "-p", remote_tip, "-m", commit_msg],
                cwd=cwd, env=temp_env, capture_output=True, text=True
            )
            if commit_res.returncode != 0:
                raise RuntimeError(f"git commit-tree failed: {commit_res.stderr.strip()}")
            new_commit = commit_res.stdout.strip() if isinstance(commit_res.stdout, str) else "00000000"

            push_res = subprocess.run(
                ["git", "push", "origin", f"{new_commit}:refs/heads/{current_branch}"],
                cwd=cwd, env=env, capture_output=True, text=True, timeout=60
            )
            if push_res.returncode == 0:
                log_success(f"Changes pushed successfully to origin/{current_branch} on attempt {attempt} (commit {new_commit[:8]}).")
                try:
                    curr_branch = _get_current_branch(cwd)
                    res_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True)
                    head_sha = res_head.stdout.strip() if isinstance(res_head.stdout, str) else ""
                    if curr_branch == current_branch and head_sha != start_commit:
                        subprocess.run(["git", "update-ref", f"refs/heads/{current_branch}", new_commit], cwd=cwd, check=False)
                except Exception:
                    pass
                return True

            err_msg = push_res.stderr.strip() if push_res.stderr else "Push rejected"
            log_warn(f"Push attempt {attempt}/{max_retries} failed: {err_msg}")
        finally:
            if os.path.exists(temp_index):
                try:
                    os.remove(temp_index)
                except Exception:
                    pass
            if merged_lock_path and os.path.exists(merged_lock_path):
                try:
                    os.remove(merged_lock_path)
                except Exception:
                    pass

        if attempt == max_retries:
            raise RuntimeError(f"Failed to push to origin/{current_branch} after {max_retries} attempts. Last error: {err_msg}")

        backoff = min(base_delay * (2 ** (attempt - 1)), max_delay) + random.uniform(0.1, 0.5)
        log_info(f"Waiting {backoff:.2f}s before retry {attempt + 1}/{max_retries}...")
        time.sleep(backoff)

    raise RuntimeError(f"Failed to push to origin/{current_branch} after {max_retries} attempts.")


def sync_before_node(current_branch=None, cwd=None, start_commit=None):
    """Synchronize repository with origin before executing a node.
    
    Targeted synchronization: fetches origin/<branch> and restores ONLY dvc.lock
    and output files (metrics, plots, cache:false outs declared in dvc.yaml/dvc.lock
    at start_commit).
    Does NOT move HEAD of the code repository.
    Logs explicit warning if human commits modified code since start_commit.
    Fails loudly on fetch error or merge conflict without silent fallback.
    """
    cwd = cwd or os.getcwd()
    install_dvc_lock_merge_driver(repo_path=cwd)
    env = _get_git_env()

    if not current_branch:
        current_branch = _get_current_branch(cwd)

    start_commit = start_commit or _get_start_commit(cwd)

    log_info(f"Targeted synchronization before node on branch '{current_branch}' (code baseline: {start_commit[:8] if len(start_commit)>=8 else start_commit})...")

    # Check for active rebase in progress
    git_dir_res = subprocess.run(['git', 'rev-parse', '--git-dir'], cwd=cwd, capture_output=True, text=True, env=env)
    if git_dir_res.returncode == 0 and git_dir_res.stdout:
        git_dir = git_dir_res.stdout.strip()
        if not os.path.isabs(git_dir):
            git_dir = os.path.join(cwd, git_dir)
        if os.path.exists(os.path.join(git_dir, 'rebase-merge')) or os.path.exists(os.path.join(git_dir, 'rebase-apply')):
            raise RuntimeError(f"Repository at {cwd} has an active rebase in progress before running node.")

    # 1. Fetch remote branch
    res_fetch = subprocess.run(
        ['git', 'fetch', 'origin', current_branch],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=60,
        env=env
    )
    if res_fetch.returncode != 0:
        err = res_fetch.stderr.strip() if res_fetch.stderr else (res_fetch.stdout.strip() if res_fetch.stdout else "git fetch failed")
        log_warn(f"Failed to fetch origin/{current_branch}: {err}")
        raise RuntimeError(f"sync_before_node failed on branch '{current_branch}': {err}")

    remote_ref = f"origin/{current_branch}"
    verify_res = subprocess.run(['git', 'rev-parse', '--verify', remote_ref], cwd=cwd, capture_output=True, text=True, env=env)
    if verify_res.returncode != 0:
        log_info(f"Remote branch '{remote_ref}' does not exist on origin yet. Skipping sync.")
        return

    # 2. Get allowed paths from start_commit
    allowed_paths = get_allowed_sync_paths(cwd, start_commit=start_commit)

    # 3. Detect human commits modifying code since start_commit
    try:
        rev_range = f"{start_commit}..{remote_ref}" if start_commit and start_commit != "HEAD" else f"HEAD..{remote_ref}"
        log_res = subprocess.run(
            ['git', 'log', '--format=%H|%an|%ae|%s', rev_range],
            cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace', env=env
        )
        if log_res.returncode == 0 and log_res.stdout.strip():
            human_commits = 0
            for line in log_res.stdout.strip().splitlines():
                parts = line.split('|', 3)
                if len(parts) >= 4:
                    c_hash, c_author, c_email, c_subj = parts[0], parts[1], parts[2], parts[3]
                    is_bot = (
                        "cluster-ci-bot" in c_author.lower()
                        or "bot@cluster-ci.io" in c_email.lower()
                        or "[skip ci]" in c_subj.lower()
                        or c_subj.startswith("chore(ci):")
                    )
                    if not is_bot:
                        diff_res = subprocess.run(
                            ['git', 'diff-tree', '--no-commit-id', '--name-only', '-r', c_hash],
                            cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace', env=env
                        )
                        if diff_res.returncode == 0:
                            touched = [t.strip() for t in diff_res.stdout.splitlines() if t.strip()]
                            if any(not is_path_allowed(t, allowed_paths) for t in touched):
                                human_commits += 1
            if human_commits > 0:
                msg = f"{human_commits} commits humains ignorés jusqu'à la relance"
                log_info(f"⚠️  {msg}")
    except Exception as exc:
        log_warn(f"Failed to inspect human commits: {exc}")

    # 4. Find diff between local HEAD and remote_ref
    diff_res = subprocess.run(
        ['git', 'diff', '--name-only', 'HEAD', remote_ref],
        cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace', env=env
    )
    if diff_res.returncode != 0:
        err = diff_res.stderr.strip() if diff_res.stderr else "git diff failed"
        raise RuntimeError(f"sync_before_node: git diff HEAD {remote_ref} failed: {err}")

    diff_files = [f.strip() for f in diff_res.stdout.splitlines() if f.strip()]
    allowed_diff_files = [f for f in diff_files if is_path_allowed(f, allowed_paths)]

    # 5. Targeted restore of allowed files without moving HEAD
    if "dvc.lock" in allowed_diff_files:
        _sync_dvc_lock(cwd, remote_ref, start_commit, env)
        allowed_diff_files = [f for f in allowed_diff_files if f != "dvc.lock"]

    for f in allowed_diff_files:
        cat_res = subprocess.run(['git', 'cat-file', '-e', f'{remote_ref}:{f}'], cwd=cwd, env=env)
        if cat_res.returncode == 0:
            chk_res = subprocess.run(['git', 'checkout', remote_ref, '--', f], cwd=cwd, capture_output=True, text=True, env=env)
            if chk_res.returncode != 0:
                raise RuntimeError(f"sync_before_node: failed to checkout '{f}' from {remote_ref}: {chk_res.stderr.strip()}")
            log_info(f"Restored output '{f}' from {remote_ref}")
        else:
            full_path = os.path.join(cwd, f)
            if os.path.exists(full_path):
                if os.path.isfile(full_path):
                    os.remove(full_path)
                elif os.path.isdir(full_path):
                    shutil.rmtree(full_path)
                log_info(f"Removed deleted output '{f}'")

    log_success(f"Targeted synchronization complete before node on branch '{current_branch}'. HEAD of code preserved.")

def sync_metrics():
    if os.environ.get("IS_LOCAL") == "1":
        log_info("IS_LOCAL=1 detected: Redirecting metrics sync to HTTP endpoint.")
        return _sync_metrics_http()

    install_dvc_lock_merge_driver()

    # Check if dvc.lock has changes or is untracked
    dvc_lock_changed = False
    if os.path.exists('dvc.lock'):
        # Check for modifications
        res_diff = subprocess.run(['git', 'diff', '--quiet', 'dvc.lock'])
        if res_diff.returncode != 0:
            dvc_lock_changed = True
        else:
            # Check if it is untracked
            res_status = subprocess.run(['git', 'status', '--porcelain', 'dvc.lock'], capture_output=True, text=True)
            if '??' in res_status.stdout:
                dvc_lock_changed = True
    else:
        # No dvc.lock found
        pass

    added_any = False

    if dvc_lock_changed:
        subprocess.run(['git', 'add', 'dvc.lock'], check=True)
        log_info("Staged modified dvc.lock for synchronization")
        added_any = True

    # 2. Stage metrics and plots
    dvc_yaml_path = 'dvc.yaml'
    paths = get_cache_false_paths(dvc_yaml_path)

    for path in paths:
        if not os.path.isfile(path):
            continue

        size_mb = os.path.getsize(path) / (1024 * 1024)
        if size_mb < 5:
            # Check if file has modifications or is untracked (even if ignored)
            res_status = subprocess.run(['git', 'status', '--porcelain', '--ignored', path], capture_output=True, text=True)
            if res_status.stdout.strip():
                subprocess.run(['git', 'add', '-f', path], check=True)
                log_info(f"Staged {path} ({size_mb:.2f} MB)")
                added_any = True
        else:
            log_warn(f"WARNING: Le fichier {path} (déclaré comme metric/plot) dépasse 5 Mo. Il ne sera synchronisé ni sur Git, ni sur le réseau P2P. Si vous souhaitez conserver ce fichier, déplacez-le sous la clé outs: dans votre dvc.yaml.")

    # Commit local changes if there are any staged
    has_changes_to_commit = False
    if added_any:
        res = subprocess.run(['git', 'diff', '--cached', '--quiet'])
        if res.returncode != 0:
            has_changes_to_commit = True

    if has_changes_to_commit:
        # Detect what was actually staged using git diff --cached --quiet
        dvc_lock_staged = False
        if os.path.exists('dvc.lock'):
            res_diff_lock = subprocess.run(['git', 'diff', '--cached', '--quiet', 'dvc.lock'])
            if res_diff_lock.returncode != 0:
                dvc_lock_staged = True

        metrics_staged = False
        for path in paths:
            if os.path.isfile(path):
                res_diff_metric = subprocess.run(['git', 'diff', '--cached', '--quiet', path])
                if res_diff_metric.returncode != 0:
                    metrics_staged = True
                    break

        if dvc_lock_staged and metrics_staged:
            commit_msg = 'chore(ci): auto-sync metrics and dvc.lock [skip ci]'
        elif dvc_lock_staged:
            commit_msg = 'chore(ci): auto-sync dvc.lock [skip ci]'
        elif metrics_staged:
            commit_msg = 'chore(ci): auto-sync metrics [skip ci]'
        else:
            commit_msg = 'chore(ci): auto-sync changes [skip ci]'

        log_info(f"Committing changes with message: {commit_msg}")
        subprocess.run(['git', 'config', 'user.name', 'cluster-ci-bot'], check=True)
        subprocess.run(['git', 'config', 'user.email', 'bot@cluster-ci.io'], check=True)
        subprocess.run(['git', 'commit', '-m', commit_msg], check=True)

        current_branch = _get_current_branch()

        # Push all accumulated local commits robustly with retries and rebase
        log_info(f"Pushing all accumulated local commits to origin on branch '{current_branch}'...")
        push_with_retries(current_branch=current_branch)
    else:
        log_info("No new metrics changes to commit. Skipping push.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest='command')

    subparsers.add_parser('inject')
    subparsers.add_parser('sync')
    subparsers.add_parser('sync-local-results')
    subparsers.add_parser('sync-before-node')
    subparsers.add_parser('install-merge-driver')

    args = parser.parse_args()

    if args.command == 'inject':
        inject_cache_false('dvc.yaml')
    elif args.command == 'sync':
        sync_metrics()
    elif args.command == 'sync-local-results':
        sync_local_results_archive()
    elif args.command == 'sync-before-node':
        sync_before_node()
    elif args.command == 'install-merge-driver':
        install_dvc_lock_merge_driver()
    else:
        parser.print_help()
