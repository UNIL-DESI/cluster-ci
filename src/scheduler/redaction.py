"""Centralized secret redaction module for Cluster-CI.

Ensures that sensitive credentials, tokens, keys, clone URLs, and environment variables
are redacted from API responses and log outputs before being returned to users or clients.
"""

import json
import re
from typing import Any

# Regex patterns for token signatures and authentication strings
PATTERNS = [
    # GitHub Personal Access Tokens (classic, fine-grained, OAuth, etc.)
    (re.compile(r'\bghp_[A-Za-z0-9_]{10,}\b'), 'ghp_****'),
    (re.compile(r'\bgho_[A-Za-z0-9_]{10,}\b'), 'gho_****'),
    (re.compile(r'\bgithub_pat_[A-Za-z0-9_]{10,}\b'), 'github_pat_****'),
    (re.compile(r'\bghu_[A-Za-z0-9_]{10,}\b'), 'ghu_****'),
    (re.compile(r'\bghs_[A-Za-z0-9_]{10,}\b'), 'ghs_****'),
    (re.compile(r'\bghr_[A-Za-z0-9_]{10,}\b'), 'ghr_****'),
    # Catch-all for shorter / test ghp_ patterns (e.g. ghp_TESTTESTTEST...)
    (re.compile(r'ghp_[A-Za-z0-9_]+'), 'ghp_****'),
    (re.compile(r'gho_[A-Za-z0-9_]+'), 'gho_****'),
    (re.compile(r'github_pat_[A-Za-z0-9_]+'), 'github_pat_****'),
    # Third-party model & API providers
    (re.compile(r'\bhf_[A-Za-z0-9]{10,}\b'), 'hf_****'),
    (re.compile(r'\btgp_[A-Za-z0-9_]{10,}\b'), 'tgp_****'),
    (re.compile(r'\bsk-[A-Za-z0-9_\-]{20,}\b'), 'sk-****'),
    # Git Clone URLs with credentials
    # e.g. https://x-access-token:...@github.com or https://token@github.com or https://user:pass@github.com
    (re.compile(r'https://[^/\s@]+@github\.com', re.IGNORECASE), 'https://****@github.com'),
    (re.compile(r'x-access-token:[^@\s]+@', re.IGNORECASE), 'x-access-token:****@'),
    # HTTP Bearer tokens
    (re.compile(r'Bearer\s+[A-Za-z0-9_\-\.]{15,}', re.IGNORECASE), 'Bearer ****'),
]

SENSITIVE_EXACT_KEYS = {
    'gh_token',
    'token',
    'password',
    'secret',
    'authorization',
    'auth',
    'access_token',
    'refresh_token',
    'private_key',
    'secret_key',
    'api_key',
    'client_secret',
    'github_pat',
    'cluster_token',
}

NON_SENSITIVE_KEY_EXCEPTIONS = {
    'workspace_key',
    'cache_key',
    'id_key',
    'sort_key',
    'primary_key',
    'viewer_port',
    'service_url',
    'commit_hash',
    'repo',
    'branch',
    'username',
    'status',
    'job_id',
    'worker_id',
}


def is_sensitive_key(key: Any) -> bool:
    """Determine whether a dict key represents sensitive credential data."""
    if not isinstance(key, str):
        return False
    k = key.strip().lower()
    if k in NON_SENSITIVE_KEY_EXCEPTIONS:
        return False
    if k in SENSITIVE_EXACT_KEYS:
        return True
    k_upper = key.strip().upper()
    if k_upper.endswith('_TOKEN') or k_upper.endswith('_KEY') or k_upper.startswith('SECRET_'):
        return True
    if 'token' in k or 'password' in k or 'secret' in k:
        return True
    return False


def redact_string(text: str) -> str:
    """Apply regex redaction rules to a string."""
    if not text:
        return text
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_env_vars(env_vars_val: Any) -> Any:
    """Mask values in env_vars while strictly preserving environment variable names."""
    if env_vars_val is None:
        return None

    if isinstance(env_vars_val, dict):
        return {
            str(k): ("***REDACTED***" if v is not None else None)
            for k, v in env_vars_val.items()
        }

    if isinstance(env_vars_val, str):
        try:
            parsed = json.loads(env_vars_val)
            if isinstance(parsed, dict):
                redacted_dict = {
                    str(k): ("***REDACTED***" if v is not None else None)
                    for k, v in parsed.items()
                }
                return json.dumps(redacted_dict)
        except Exception:
            pass
        return redact_string(env_vars_val)

    return "***REDACTED***"


def redact_secrets(obj: Any) -> Any:
    """Recursively redact sensitive keys and credential patterns from obj.

    Supports dict, list, tuple, set, str, and primitive types.
    Preserves original data types and structure without in-place mutation.
    """
    if obj is None:
        return None

    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            k_lower = str(k).strip().lower() if isinstance(k, str) else ""

            if k_lower == 'env_vars':
                result[k] = redact_env_vars(v)
            elif is_sensitive_key(k):
                result[k] = "***REDACTED***" if v is not None else None
            else:
                result[k] = redact_secrets(v)
        return result

    if isinstance(obj, list):
        return [redact_secrets(item) for item in obj]

    if isinstance(obj, tuple):
        return tuple(redact_secrets(item) for item in obj)

    if isinstance(obj, set):
        return {redact_secrets(item) for item in obj}

    if isinstance(obj, str):
        return redact_string(obj)

    # Primitive types (int, float, bool, etc.) are returned untouched
    return obj
