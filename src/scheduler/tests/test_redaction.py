import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import json
import pytest
from redaction import redact_secrets, redact_string, redact_env_vars, is_sensitive_key


def test_is_sensitive_key():
    assert is_sensitive_key("gh_token") is True
    assert is_sensitive_key("GH_TOKEN") is True
    assert is_sensitive_key("token") is True
    assert is_sensitive_key("password") is True
    assert is_sensitive_key("secret") is True
    assert is_sensitive_key("HF_TOKEN") is True
    assert is_sensitive_key("TOGETHER_API_KEY") is True
    assert is_sensitive_key("OPENAI_API_KEY") is True
    assert is_sensitive_key("authorization") is True
    assert is_sensitive_key("Authorization") is True

    # Benign keys
    assert is_sensitive_key("job_id") is False
    assert is_sensitive_key("repo") is False
    assert is_sensitive_key("branch") is False
    assert is_sensitive_key("status") is False
    assert is_sensitive_key("viewer_port") is False
    assert is_sensitive_key("workspace_key") is False


def test_redact_string_patterns():
    # GitHub tokens
    s1 = "Clone using ghp_TESTTESTTEST123456789 on worker"
    assert "ghp_TESTTESTTEST123456789" not in redact_string(s1)
    assert "ghp_****" in redact_string(s1)

    s2 = "Token: github_pat_11AABBCCDDEEFF00112233"
    assert "github_pat_11AABBCCDDEEFF00112233" not in redact_string(s2)
    assert "github_pat_****" in redact_string(s2)

    # Clone URL
    s3 = "git clone https://x-access-token:ghp_SECRETTOKEN123@github.com/UNIL-DESI/repo.git"
    redacted3 = redact_string(s3)
    assert "ghp_SECRETTOKEN123" not in redacted3
    assert "x-access-token" not in redacted3
    assert "https://****@github.com/UNIL-DESI/repo.git" in redacted3

    s4 = "git remote add origin https://ghp_ANOTHERSECRET@github.com/UNIL-DESI/repo.git"
    redacted4 = redact_string(s4)
    assert "ghp_ANOTHERSECRET" not in redacted4
    assert "https://****@github.com/UNIL-DESI/repo.git" in redacted4

    # Other API tokens
    s5 = "HF_TOKEN=hf_abcdef1234567890 and TGP=tgp_0987654321fedcba"
    redacted5 = redact_string(s5)
    assert "hf_abcdef1234567890" not in redacted5
    assert "hf_****" in redacted5
    assert "tgp_0987654321fedcba" not in redacted5
    assert "tgp_****" in redacted5


def test_redact_env_vars_dict():
    raw_env = {
        "HF_TOKEN": "hf_secret123456",
        "TOGETHER_API_KEY": "tgp_key789",
        "BATCH_SIZE": "64",
        "MODEL_NAME": "lightgcn"
    }
    redacted = redact_env_vars(raw_env)
    assert isinstance(redacted, dict)
    assert set(redacted.keys()) == {"HF_TOKEN", "TOGETHER_API_KEY", "BATCH_SIZE", "MODEL_NAME"}
    assert redacted["HF_TOKEN"] == "***REDACTED***"
    assert redacted["TOGETHER_API_KEY"] == "***REDACTED***"
    assert redacted["BATCH_SIZE"] == "***REDACTED***"
    assert redacted["MODEL_NAME"] == "***REDACTED***"


def test_redact_env_vars_json_string():
    raw_env = {
        "HF_TOKEN": "hf_secret123456",
        "LEARNING_RATE": "0.001"
    }
    raw_json = json.dumps(raw_env)
    redacted_json = redact_env_vars(raw_json)
    parsed = json.loads(redacted_json)
    assert set(parsed.keys()) == {"HF_TOKEN", "LEARNING_RATE"}
    assert parsed["HF_TOKEN"] == "***REDACTED***"
    assert parsed["LEARNING_RATE"] == "***REDACTED***"


def test_redact_secrets_recursive_job_dict():
    job = {
        "job_id": "job-1234",
        "repo": "UNIL-DESI/llm-as-recommender",
        "branch": "cluster-draft/hjamet",
        "gh_token": "ghp_TESTTESTTEST00000000000000000000",
        "env_vars": json.dumps({"HF_TOKEN": "hf_val", "CUSTOM": "val"}),
        "ram_required_gb": 100,
        "status": "running"
    }

    cleaned = redact_secrets(job)
    # Check original was not mutated
    assert job["gh_token"] == "ghp_TESTTESTTEST00000000000000000000"

    # Check cleaned
    assert cleaned["job_id"] == "job-1234"
    assert cleaned["repo"] == "UNIL-DESI/llm-as-recommender"
    assert cleaned["gh_token"] == "***REDACTED***"
    parsed_env = json.loads(cleaned["env_vars"])
    assert parsed_env["HF_TOKEN"] == "***REDACTED***"
    assert parsed_env["CUSTOM"] == "***REDACTED***"
    assert cleaned["ram_required_gb"] == 100


def test_redact_secrets_list_of_jobs():
    jobs = [
        {
            "job_id": "1",
            "gh_token": "ghp_AAAABBBBCCCC",
            "env_vars": {"SECRET_KEY": "12345"}
        },
        {
            "job_id": "2",
            "gh_token": None,
            "env_vars": None
        }
    ]

    cleaned = redact_secrets(jobs)
    assert cleaned[0]["gh_token"] == "***REDACTED***"
    assert cleaned[0]["env_vars"]["SECRET_KEY"] == "***REDACTED***"
    assert cleaned[1]["gh_token"] is None
    assert cleaned[1]["env_vars"] is None
