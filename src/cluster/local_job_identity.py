"""Canonical identities for local Cluster-CI jobs."""

import re


DEFAULT_LOCAL_LABEL = "default"
_LOCAL_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def normalize_local_label(label=None):
    """Return a validated local-job label, defaulting only when omitted."""
    if label is None:
        return DEFAULT_LOCAL_LABEL
    if not isinstance(label, str) or not _LOCAL_LABEL_RE.fullmatch(label):
        raise ValueError(
            "Local job labels must be 1-64 characters and contain only "
            "letters, numbers, underscores, or hyphens; the first character "
            "must be a letter or number."
        )
    return label


def local_job_branch(username, label=None):
    """Build the server-controlled branch identity for a local job."""
    safe_username = username or "anonymous"
    normalized_label = normalize_local_label(label)
    if normalized_label == DEFAULT_LOCAL_LABEL:
        # Preserve the historical identity for unlabeled clients and jobs.
        return f"local-draft/{safe_username}"
    return f"local-draft/{safe_username}/{normalized_label}"
