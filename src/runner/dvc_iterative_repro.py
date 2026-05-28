import sys
import subprocess
import re
import os
import json
from collections import defaultdict, deque


ITERATIVE_STATUS_FILE = os.path.join(".dvc", "tmp", "iterative-status.json")


def write_status(stage_name):
    """Write the iterative status file for the dvc-viewer to detect running stages."""
    os.makedirs(os.path.dirname(ITERATIVE_STATUS_FILE), exist_ok=True)
    status = {
        "running": True,
        "stage": stage_name,
        "pid": os.getpid(),
    }
    try:
        with open(ITERATIVE_STATUS_FILE, "w") as f:
            json.dump(status, f)
    except OSError:
        pass


def clear_status():
    """Remove the iterative status file."""
    try:
        if os.path.exists(ITERATIVE_STATUS_FILE):
            os.remove(ITERATIVE_STATUS_FILE)
    except OSError:
        pass

def get_dvc_dag(targets):
    """
    Run 'dvc dag --dot' to get the full DAG.
    If targets are specified, filter the DAG to only include ancestors of targets.
    Returns topological sort of the (filtered) stages.
    """
    cmd = ["dvc", "dag", "--dot"]
    print(f"📊 Analyzing DVC DAG: {' '.join(cmd)}")
    
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        print(f"❌ Failed to parse DVC DAG:\n{result.stderr}")
        sys.exit(result.returncode)
        
    dot_string = result.stdout
    
    nodes = set()
    edges = defaultdict(list)
    rev_edges = defaultdict(list)
    in_degree = defaultdict(int)
    
    for line in dot_string.splitlines():
        line = line.strip()
        if not line or line.startswith('strict digraph') or line == '}':
            continue
            
        match_edge = re.match(r'"([^"]+)"\s*->\s*"([^"]+)"', line)
        if match_edge:
            u, v = match_edge.groups()
            nodes.add(u)
            nodes.add(v)
            edges[u].append(v)
            rev_edges[v].append(u)
            in_degree[v] += 1
            if u not in in_degree:
                in_degree[u] = 0
            continue
            
        match_node = re.match(r'"([^"]+)"', line)
        if match_node:
            node = match_node.group(1)
            nodes.add(node)
            if node not in in_degree:
                in_degree[node] = 0

    if not nodes:
        return []

    # If targets are specified, we only want the targets and all their ancestors
    if targets:
        # Validate targets
        valid_targets = []
        for t in targets:
            if t in nodes:
                valid_targets.append(t)
            else:
                print(f"⚠️ Warning: Target '{t}' not found in DAG.")
                valid_targets.append(t) # Keep it anyway, DVC will handle the error
        
        needed_nodes = set()
        queue = deque(valid_targets)
        while queue:
            curr = queue.popleft()
            if curr not in needed_nodes:
                needed_nodes.add(curr)
                for parent in rev_edges[curr]:
                    queue.append(parent)
                    
        # Filter nodes, edges, in_degree
        nodes = needed_nodes
        new_edges = defaultdict(list)
        new_in_degree = defaultdict(int)
        for u in nodes:
            if u not in new_in_degree:
                new_in_degree[u] = 0
            for v in edges[u]:
                if v in nodes:
                    new_edges[u].append(v)
                    new_in_degree[v] += 1
        edges = new_edges
        in_degree = new_in_degree

    # Topological sort
    queue = deque(sorted([u for u in nodes if in_degree[u] == 0]))
    topo_order = []
    
    while queue:
        u = queue.popleft()
        topo_order.append(u)
        
        for v in sorted(edges[u]):
            in_degree[v] -= 1
            if in_degree[v] == 0:
                queue.append(v)
                
    if len(topo_order) != len(nodes):
        print("⚠️ Warning: Cycle detected in DAG, falling back to basic list.")
        return list(nodes)
        
    return topo_order

def main():
    # Note: line buffering is handled by 'python -u' flag in run_research_pipeline.sh
    # Do NOT reconfigure stdout here as it would downgrade -u's write-through mode
    dvc_args = sys.argv[1:]
    
    flags = []
    targets = []
    for arg in dvc_args:
        if arg.startswith('-'):
            flags.append(arg)
        else:
            targets.append(arg)
            
    stages = get_dvc_dag(targets)
    if not stages:
        print("✅ No stages found in the pipeline.")
        return
        
    print(f"📋 Stages to execute sequentially: {stages}")
    
    for stage in stages:
        print(f"\n==================================================")
        print(f"🚀 Executing stage: {stage}")
        print(f"==================================================")
        
        write_status(stage)
        stage_cmd = ["dvc", "repro", stage] + flags
        ret = subprocess.run(stage_cmd)
        if ret.returncode != 0:
            clear_status()
            print(f"❌ Stage {stage} failed with code {ret.returncode}")
            print(f"💾 Committing and pushing failure state to GitHub...")
            subprocess.run(["git", "add", "."], check=False)
            status = subprocess.run(["git", "status", "--porcelain"], stdout=subprocess.PIPE, text=True)
            if status.stdout.strip():
                subprocess.run(["git", "config", "user.name", "cluster-ci"])
                subprocess.run(["git", "config", "user.email", "cluster-ci@cluster.local"])
                commit_msg = f"cluster-ci: failed stage {stage} [skip ci]"
                subprocess.run(["git", "commit", "-m", commit_msg])
            
            target_branch = os.environ.get("TARGET_BRANCH")
            if not target_branch:
                res_branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True)
                if res_branch.returncode == 0:
                    target_branch = res_branch.stdout.strip()
            
            if target_branch and target_branch != "HEAD":
                print(f"Pushing failure state to branch: {target_branch}")
                subprocess.run(["git", "push", "origin", target_branch])
            else:
                print("Pushing failure state to default HEAD branch...")
                subprocess.run(["git", "push", "origin", "HEAD"])
            sys.exit(ret.returncode)
            
        print(f"==================================================")
        print(f"✅ Stage {stage} completed successfully.")
        print(f"==================================================")
            
    clear_status()
    print(f"\n✅ Iterative reproduction completed successfully.")

if __name__ == "__main__":
    main()
