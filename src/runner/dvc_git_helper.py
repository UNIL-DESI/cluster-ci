import os
import sys
import argparse
import subprocess
from pathlib import Path

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
            for key in ['metrics', 'plots']:
                if key in stage:
                    process_entries(stage[key], stage, key)

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
            for key in ['metrics', 'plots']:
                if key in stage:
                    extract_from_entries(stage[key], wdir)

    for key in ['metrics', 'plots']:
        if key in data:
            extract_from_entries(data[key])

    return list(paths)

def sync_metrics():
    added_any = False

    # 1. Stage dvc.lock if it exists
    if os.path.exists('dvc.lock'):
        # Check if dvc.lock has changes before adding
        res_diff = subprocess.run(['git', 'diff', '--quiet', 'dvc.lock'])
        if res_diff.returncode != 0:
            subprocess.run(['git', 'add', 'dvc.lock'], check=True)
            log_info("Staged modified dvc.lock for synchronization")
            added_any = True
        else:
            log_info("dvc.lock is already up to date, skipping staging.")

    # 2. Stage metrics and plots
    dvc_yaml_path = 'dvc.yaml'
    paths = get_cache_false_paths(dvc_yaml_path)

    for path in paths:
        if not os.path.isfile(path):
            continue

        size_mb = os.path.getsize(path) / (1024 * 1024)
        if size_mb < 5:
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
    else:
        log_info("No new metrics changes to commit.")

    # Always push all accumulated local commits robustly
    log_info("Pushing all accumulated local commits to origin...")
    try:
        subprocess.run(['git', 'push', 'origin', 'HEAD'], check=True, capture_output=True, text=True)
        log_success("All changes (including intermediate and final stages) pushed successfully.")
    except subprocess.CalledProcessError as e:
        log_warn(f"Initial push failed, attempting reconciliation (rebase): {e.stderr.strip()}")
        try:
            # Attempt to pull with rebase to handle remote changes
            subprocess.run(['git', 'pull', '--rebase', 'origin', 'HEAD'], check=True, capture_output=True, text=True)
            log_info("Rebase successful, retrying push...")
            subprocess.run(['git', 'push', 'origin', 'HEAD'], check=True, capture_output=True, text=True)
            log_success("All changes pushed successfully after reconciliation.")
        except subprocess.CalledProcessError as rebase_err:
            log_warn(f"Reconciliation failed: {rebase_err.stderr.strip()}")
            # Abort rebase if it's still in progress to leave the repo in a clean state
            subprocess.run(['git', 'rebase', '--abort'], check=False, capture_output=True)
            log_warn("Push abandoned. The pipeline will continue, but local commits were not synchronized.")

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
