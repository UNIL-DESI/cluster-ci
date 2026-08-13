import os
import re
import sys
import argparse
import subprocess
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
    print("❌ Error: 'ruamel.yaml' is missing. Please run this script using 'uv run --with ruamel.yaml'.")
    sys.exit(1)

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
            if response.status in (200, 201):
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

def sync_metrics():
    if os.environ.get("IS_LOCAL") == "1":
        log_info("IS_LOCAL=1 detected: Redirecting metrics sync to HTTP endpoint.")
        return _sync_metrics_http()

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

        # Push all accumulated local commits robustly
        log_info("Pushing all accumulated local commits to origin...")
        try:
            subprocess.run(['git', 'push', 'origin', 'HEAD'], check=True, capture_output=True, text=True, timeout=60)
            log_success("All changes pushed successfully.")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            log_warn(f"Initial push failed, attempting reconciliation (rebase): {getattr(e, 'stderr', '') or e}")
            try:
                # Attempt to pull with rebase to handle remote changes
                subprocess.run(['git', 'pull', '--rebase', 'origin', 'HEAD'], check=True, capture_output=True, text=True, timeout=60)
                log_info("Rebase successful, retrying push...")
                subprocess.run(['git', 'push', 'origin', 'HEAD'], check=True, capture_output=True, text=True, timeout=60)
                log_success("All changes pushed successfully after reconciliation.")
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as rebase_err:
                log_warn(f"Reconciliation failed: {getattr(rebase_err, 'stderr', '') or rebase_err}")
                # Abort rebase if it's still in progress to leave the repo in a clean state
                subprocess.run(['git', 'rebase', '--abort'], check=False, capture_output=True)
                log_warn("Push abandoned. The pipeline will continue, but local commits were not synchronized.")
    else:
        log_info("No new metrics changes to commit. Skipping push.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest='command')

    subparsers.add_parser('inject')
    subparsers.add_parser('sync')

    args = parser.parse_args()

    if args.command == 'inject':
        inject_cache_false('dvc.yaml')
    elif args.command == 'sync':
        sync_metrics()
    else:
        parser.print_help()
