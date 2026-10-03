"""Real pytest tests for cluster-ci v3 stage planner (W1)."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import pytest
import yaml

from dvc.repo import Repo
from src.config.defaults import DEFAULT_RESOURCES, parse_project_cluster_ci, validate_and_resolve_resources
from src.planner.stage_plan import compute_stage_plan


def _remove_readonly(func, path, exc_info):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


@pytest.fixture
def toy_repo():
    """Create a real temporary Git + DVC repository with a branching DAG."""
    d = tempfile.mkdtemp(prefix="test_toy_repo_")
    try:
        subprocess.run(["git", "init"], cwd=d, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=d, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=d, check=True)
        subprocess.run(["dvc", "init"], cwd=d, check=True, capture_output=True)

        # Code files
        with open(os.path.join(d, "prep_a.py"), "w", encoding="utf-8") as f:
            f.write('with open("out_a.txt", "w") as f: f.write("branch_a\\n")\n')

        with open(os.path.join(d, "proc_b.py"), "w", encoding="utf-8") as f:
            f.write('import sys\nitem = sys.argv[1]\nwith open(f"out_b_{item}.txt", "w") as f: f.write(f"branch_b_{item}\\n")\n')

        with open(os.path.join(d, "join.py"), "w", encoding="utf-8") as f:
            f.write('with open("final.txt", "w") as f: f.write("final\\n")\n')

        with open(os.path.join(d, "params.yaml"), "w", encoding="utf-8") as f:
            f.write("lr: 0.01\n")

        # Project .cluster-ci
        with open(os.path.join(d, ".cluster-ci"), "w", encoding="utf-8") as f:
            f.write("REQUIRED_RAM=16GB\nREQUIRED_VRAM=8GB\nDOCKER_IMAGE=nvcr.io/nvidia/pytorch:custom\nALLOWED_WORKERS=HEC1,HEC2\n")

        # dvc.yaml with 2 independent branches (prep_a and foreach proc_b) + 1 junction node (join_all)
        dvc_yaml = """stages:
  prep_a:
    cmd: python prep_a.py
    deps:
      - prep_a.py
    params:
      - lr
    outs:
      - out_a.txt

  proc_b:
    foreach:
      - item1
      - item2
    do:
      cmd: python proc_b.py ${item}
      deps:
        - proc_b.py
      outs:
        - out_b_${item}.txt
      meta:
        cluster:
          cpus: 8
          ram_gb: 24

  join_all:
    cmd: python join.py
    deps:
      - join.py
      - out_a.txt
      - out_b_item1.txt
      - out_b_item2.txt
    outs:
      - final.txt
    meta:
      cluster:
        vram_gb: 16
"""
        with open(os.path.join(d, "dvc.yaml"), "w", encoding="utf-8") as f:
            f.write(dvc_yaml)

        subprocess.run(["git", "add", "."], cwd=d, check=True)
        subprocess.run(["git", "commit", "-m", "init toy repo"], cwd=d, check=True)

        yield d
    finally:
        shutil.rmtree(d, onerror=_remove_readonly, ignore_errors=True)


def test_dvc_yaml_missing():
    """Fail-fast: error when dvc.yaml is missing."""
    empty_dir = tempfile.mkdtemp()
    try:
        with pytest.raises(FileNotFoundError, match="dvc.yaml not found"):
            compute_stage_plan(empty_dir)
    finally:
        shutil.rmtree(empty_dir, ignore_errors=True)


def test_dvc_yaml_invalid():
    """Fail-fast: error when dvc.yaml contains invalid syntax."""
    bad_dir = tempfile.mkdtemp()
    try:
        with open(os.path.join(bad_dir, "dvc.yaml"), "w") as f:
            f.write("invalid: [broken: yaml")
        with pytest.raises(ValueError, match="Failed to load DVC repository"):
            compute_stage_plan(bad_dir)
    finally:
        shutil.rmtree(bad_dir, ignore_errors=True)


def test_meta_cluster_unknown_key(toy_repo):
    """Fail-fast: explicit exception with stage name on unknown meta.cluster key."""
    dvc_yaml_path = os.path.join(toy_repo, "dvc.yaml")
    with open(dvc_yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    data["stages"]["prep_a"]["meta"] = {"cluster": {"unknown_foo": 42}}
    with open(dvc_yaml_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)

    with pytest.raises(ValueError, match="Stage 'prep_a': unknown key"):
        compute_stage_plan(toy_repo)


def test_meta_cluster_invalid_type(toy_repo):
    """Fail-fast: explicit exception with stage name on invalid meta.cluster types."""
    dvc_yaml_path = os.path.join(toy_repo, "dvc.yaml")
    with open(dvc_yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # Negative cpus
    data["stages"]["prep_a"]["meta"] = {"cluster": {"cpus": -2}}
    with open(dvc_yaml_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)

    with pytest.raises(ValueError, match="Stage 'prep_a': meta.cluster.cpus must be a positive integer"):
        compute_stage_plan(toy_repo)

    # Invalid workers type (not list of strings)
    data["stages"]["prep_a"]["meta"] = {"cluster": {"workers": "HEC1"}}
    with open(dvc_yaml_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)

    with pytest.raises(TypeError, match="Stage 'prep_a': meta.cluster.workers must be a list"):
        compute_stage_plan(toy_repo)


def test_resource_hierarchy(toy_repo):
    """Verify priority: meta.cluster > .cluster-ci > defaults."""
    plan = compute_stage_plan(toy_repo)
    nodes = {n["name"]: n for n in plan["nodes"]}

    # prep_a has no meta.cluster:
    # ram_gb should come from .cluster-ci (16)
    # vram_gb should come from .cluster-ci (8)
    # image should come from .cluster-ci (nvcr.io/nvidia/pytorch:custom)
    # cpus should come from DEFAULT (4)
    # storage_gb should come from DEFAULT (0)
    # workers should come from .cluster-ci (['HEC1', 'HEC2'])
    prep_res = nodes["prep_a"]["resources"]
    assert prep_res["ram_gb"] == 16
    assert prep_res["vram_gb"] == 8
    assert prep_res["image"] == "nvcr.io/nvidia/pytorch:custom"
    assert prep_res["cpus"] == 4
    assert prep_res["storage_gb"] == 0
    assert prep_res["workers"] == ["HEC1", "HEC2"]

    # proc_b@item1 has meta.cluster: cpus: 8, ram_gb: 24:
    # cpus comes from meta (8)
    # ram_gb comes from meta (24)
    # vram_gb comes from .cluster-ci (8)
    proc_res = nodes["proc_b@item1"]["resources"]
    assert proc_res["cpus"] == 8
    assert proc_res["ram_gb"] == 24
    assert proc_res["vram_gb"] == 8

    # join_all has meta.cluster: vram_gb: 16
    join_res = nodes["join_all"]["resources"]
    assert join_res["vram_gb"] == 16
    assert join_res["ram_gb"] == 16  # from .cluster-ci


def test_lifecycle_and_stale_without_heavy_data(toy_repo):
    """Complete lifecycle test:
    1. Before repro: all nodes stale (absent from lock).
    2. After repro: 0 nodes stale (identical to clean dvc status).
    3. Modify branch A code: only prep_a and join_all stale (downstream propagation).
    4. Delete .dvc/cache and all heavy outputs: stale set remains EXACTLY IDENTICAL.
    """
    # 1. Before repro: not in dvc.lock -> all stale
    plan0 = compute_stage_plan(toy_repo)
    for n in plan0["nodes"]:
        assert n["stale"] is True
        assert n["stale_reason"] == "not_in_lock"

    # 2. Run dvc repro
    subprocess.run(["dvc", "repro"], cwd=toy_repo, check=True, capture_output=True)

    # Check that DVC status is clean
    dvc_repo = Repo(toy_repo)
    assert len(dvc_repo.status()) == 0

    plan1 = compute_stage_plan(toy_repo)
    stale1 = [n["name"] for n in plan1["nodes"] if n["stale"]]
    assert len(stale1) == 0, f"Expected 0 stale nodes after repro, got {stale1}"

    # 3. Modify prep_a.py (Branch A)
    with open(os.path.join(toy_repo, "prep_a.py"), "w", encoding="utf-8") as f:
        f.write('with open("out_a.txt", "w") as f: f.write("branch_a_modified\\n")\n')

    plan2 = compute_stage_plan(toy_repo)
    nodes2 = {n["name"]: n for n in plan2["nodes"]}

    assert nodes2["prep_a"]["stale"] is True
    assert "code_dep_changed:prep_a.py" in nodes2["prep_a"]["stale_reason"]
    assert nodes2["join_all"]["stale"] is True
    assert "upstream_stale:prep_a" in nodes2["join_all"]["stale_reason"]

    # Branch B must NOT be stale
    assert nodes2["proc_b@item1"]["stale"] is False
    assert nodes2["proc_b@item2"]["stale"] is False

    # Check priorities:
    # prep_a has join_all downstream -> priority = 2
    # join_all has no downstream -> priority = 1
    # non-stale -> priority = 0
    assert nodes2["prep_a"]["priority"] == 2
    assert nodes2["join_all"]["priority"] == 1
    assert nodes2["proc_b@item1"]["priority"] == 0
    assert nodes2["proc_b@item2"]["priority"] == 0

    # 4. Critical requirement: DELETE .dvc/cache and outputs
    cache_dir = os.path.join(toy_repo, ".dvc", "cache")
    if os.path.exists(cache_dir):
        shutil.rmtree(cache_dir, onerror=_remove_readonly)
    for fname in ["out_a.txt", "out_b_item1.txt", "out_b_item2.txt", "final.txt"]:
        p = os.path.join(toy_repo, fname)
        if os.path.exists(p):
            os.chmod(p, stat.S_IWRITE)
            os.remove(p)

    # Re-evaluate stage plan without ANY heavy data present
    plan3 = compute_stage_plan(toy_repo)
    nodes3 = {n["name"]: n for n in plan3["nodes"]}

    # Status must be strictly identical to plan2
    for name in nodes2:
        assert nodes3[name]["stale"] == nodes2[name]["stale"]
        assert nodes3[name]["stale_reason"] == nodes2[name]["stale_reason"]
        assert nodes3[name]["priority"] == nodes2[name]["priority"]


def test_params_change_causes_stale(toy_repo):
    """Modifying params.yaml makes the stage and its downstreams stale."""
    subprocess.run(["dvc", "repro"], cwd=toy_repo, check=True, capture_output=True)

    with open(os.path.join(toy_repo, "params.yaml"), "w", encoding="utf-8") as f:
        f.write("lr: 0.05\n")

    plan = compute_stage_plan(toy_repo)
    nodes = {n["name"]: n for n in plan["nodes"]}

    assert nodes["prep_a"]["stale"] is True
    assert "params_changed:params.yaml" in nodes["prep_a"]["stale_reason"]
    assert nodes["join_all"]["stale"] is True
    assert "upstream_stale:prep_a" in nodes["join_all"]["stale_reason"]
    assert nodes["proc_b@item1"]["stale"] is False


def test_frozen_and_always_changed(toy_repo):
    """Verify frozen and always_changed stage options."""
    dvc_yaml_path = os.path.join(toy_repo, "dvc.yaml")
    with open(dvc_yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    data["stages"]["prep_a"]["frozen"] = True
    data["stages"]["join_all"]["always_changed"] = True
    with open(dvc_yaml_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)

    # prep_a is not in lock, but frozen -> never stale
    plan = compute_stage_plan(toy_repo)
    nodes = {n["name"]: n for n in plan["nodes"]}

    assert nodes["prep_a"]["stale"] is False
    assert nodes["prep_a"]["stale_reason"] is None
    assert nodes["join_all"]["stale"] is True
    assert nodes["join_all"]["stale_reason"] == "always_changed"


def test_cli_execution(toy_repo):
    """Test CLI execution with python -m src.planner.stage_plan --repo <path> --json."""
    res = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.planner.stage_plan",
            "--repo",
            toy_repo,
            "--json",
        ],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONPATH": "."},
    )
    data = json.loads(res.stdout)
    assert data["version"] == 1
    assert "defaults" in data
    assert "nodes" in data
    assert len(data["nodes"]) == 4  # prep_a, proc_b@item1, proc_b@item2, join_all
