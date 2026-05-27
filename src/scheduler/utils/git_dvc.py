import os
import subprocess
import yaml
import logging

logger = logging.getLogger(__name__)

# Base directory for locally cloned repositories
# We use the same path logic as in the headnode services
REPOS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "repositories")

def find_local_repo(repo_slug):
    """Find the local clone of a repo, handling owner name mismatches."""
    # Try exact match first
    exact = os.path.join(REPOS_DIR, repo_slug)
    if os.path.exists(exact) and os.path.exists(os.path.join(exact, ".git")):
        return exact

    # Fallback: search by repo name only across all owner dirs
    repo_name = repo_slug.split('/')[-1] if '/' in repo_slug else repo_slug
    if os.path.exists(REPOS_DIR):
        for owner_dir in os.listdir(REPOS_DIR):
            if owner_dir.startswith('_'):  # Skip _tmp_artifacts etc.
                continue
            candidate = os.path.join(REPOS_DIR, owner_dir, repo_name)
            if os.path.isdir(candidate) and os.path.exists(os.path.join(candidate, ".git")):
                return candidate
    return None

class GitError(Exception):
    """Custom exception for Git operation failures."""
    pass

def resolve_revision(local_repo_path, rev):
    """Resolve a symbolic revision (like 'main') to an absolute commit SHA."""
    if not local_repo_path:
        raise GitError(f"Local repository path not found for revision resolution: {rev}")

    try:
        # Increase timeout and handle network failures gracefully as requested
        # First, fetch to make sure we have the latest SHAs for branches
        # We use a 60s timeout here for better reliability on large repos
        fetch_res = subprocess.run(["git", "fetch", "--all", "--prune"], cwd=local_repo_path, capture_output=True, text=True, timeout=60)
        if fetch_res.returncode != 0:
            logger.error(f"git fetch failed for {local_repo_path}: {fetch_res.stderr}")
            raise GitError(f"Failed to synchronize repository: {fetch_res.stderr.strip()}")

        # Resolve the revision
        res = subprocess.run(["git", "rev-parse", rev], cwd=local_repo_path, capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            return res.stdout.strip()
        else:
            logger.warning(f"git rev-parse failed for {rev} in {local_repo_path}: {res.stderr}")
            raise GitError(f"Revision '{rev}' not found in repository.")
    except subprocess.TimeoutExpired:
        logger.error(f"Git operation timed out for {local_repo_path}")
        raise GitError("Network timeout: repository synchronization took too long. Please try again.")
    except Exception as e:
        if isinstance(e, GitError):
            raise
        logger.error(f"Error resolving revision {rev}: {e}")
        raise GitError(f"Internal error during repository sync: {str(e)}")

def get_dvc_artifacts(local_repo_path, commit_hash):
    """Discover DVC artifacts (outs, metrics, plots) from dvc.yaml at a specific commit."""
    dvc_artifacts = set()
    if not local_repo_path:
        return dvc_artifacts

    try:
        show_cmd = ["git", "show", f"{commit_hash}:dvc.yaml"]
        show_res = subprocess.run(show_cmd, capture_output=True, text=True, cwd=local_repo_path, timeout=10)
        if show_res.returncode == 0:
            dvc_config = yaml.safe_load(show_res.stdout)
            if dvc_config and 'stages' in dvc_config:
                for stage_name, stage_cfg in dvc_config['stages'].items():
                    for key in ['outs', 'metrics', 'plots']:
                        if key in stage_cfg:
                            items = stage_cfg[key]
                            if isinstance(items, list):
                                for item in items:
                                    if isinstance(item, str):
                                        dvc_artifacts.add(item)
                                    elif isinstance(item, dict):
                                        # Handle format: {"path": {"cache": false}}
                                        for path in item.keys():
                                            dvc_artifacts.add(path)
        elif show_res.returncode != 0:
            logger.info(f"dvc.yaml not found in {local_repo_path} at {commit_hash}")
    except Exception as e:
        logger.warning(f"Failed to parse dvc.yaml in {local_repo_path}: {e}")

    return dvc_artifacts

def filter_artifact_files(all_files, dvc_artifacts):
    """Filter a list of files based on DVC artifacts or aggressive exclusion fallback."""
    filtered_files = []
    for line in all_files:
        # 1. If we have DVC artifacts, strictly filter by them
        if dvc_artifacts:
            # Check if the file is one of the artifacts or inside an artifact directory
            is_artifact = False
            for art in dvc_artifacts:
                if line == art or line.startswith(art + '/'):
                    is_artifact = True
                    break
            if not is_artifact:
                continue
        else:
            # 2. Fallback: Aggressive exclusion of non-artifact files
            # Filter out system and config directories
            if any(line.startswith(p) for p in [".git/", ".github/", ".dvc/", ".idea/", ".vscode/", ".agent/"]):
                continue
            # Filter out common source/config extensions
            if any(line.endswith(ext) for ext in [
                ".py", ".sh", ".md", ".yaml", ".yml", ".json",
                ".toml", ".lock", ".txt", ".gitattributes", ".gitignore", ".dvcignore"
            ]):
                # Exception for common metric/plot suffixes even in fallback
                if not any(x in line.lower() for x in ["metric", "plot", "result", "output", "artifact"]):
                    continue

        filtered_files.append({
            "path": line,
            "is_dir": False,
            "size": 0,
            "isout": True
        })
    return filtered_files
