import paramiko
import sys

sys.stdout.reconfigure(encoding='utf-8')

def main():
    hostname = '130.223.73.209'
    username = 'henri'
    password = '^Jw6jQTVbsGc3cwc@v^%'
    
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        client.connect(hostname, username=username, password=password, timeout=10)
        
        # Upload the test script to the cluster and run it there
        sftp = client.open_sftp()
        
        remote_script = """\
import re, sys, yaml, subprocess

print("Python:", sys.version)

# 1. Get the dvc.yaml from origin/main (what the API uses)
res_main = subprocess.run(
    ["git", "show", "origin/main:dvc.yaml"],
    capture_output=True, text=True,
    cwd="/home/henri/cluster-ci/repositories/UNIL-DESI/llm-as-recommender"
)
main_data = yaml.safe_load(res_main.stdout) or {}

# 2. Get the dvc.yaml from cluster-draft (what the DVC viewer sees)
res_draft = subprocess.run(
    ["git", "show", "origin/cluster-draft/hjamet:dvc.yaml"],
    capture_output=True, text=True,
    cwd="/home/henri/cluster-ci/repositories/UNIL-DESI/llm-as-recommender"
)
draft_data = yaml.safe_load(res_draft.stdout) or {}

# 3. Run the EXACT extract function from headnode_service.py
def extract_metrics_and_plots_paths(dvc_yaml_data):
    paths = set()
    patterns = []
    
    def resolve_item(item):
        if not item:
            return
        if isinstance(item, list):
            for x in item:
                resolve_item(x)
        elif isinstance(item, dict):
            for k in item.keys():
                if isinstance(k, str):
                    add_path(k)
        elif isinstance(item, str):
            add_path(item)
            
    def add_path(p):
        if "${" in p:
            pattern_str = re.escape(p)
            pattern_str = re.sub(r'\\\\\\$\\\\\\{[^}]+\\\\\\}', '.*', pattern_str)
            try:
                patterns.append(re.compile(f"^{pattern_str}$"))
            except Exception:
                pass
        else:
            paths.add(p)

    if isinstance(dvc_yaml_data, dict):
        stages = dvc_yaml_data.get("stages", {})
        if isinstance(stages, dict):
            for stage_def in stages.values():
                if not isinstance(stage_def, dict):
                    continue
                resolve_item(stage_def.get("metrics"))
                resolve_item(stage_def.get("plots"))
                do_block = stage_def.get("do", {})
                if isinstance(do_block, dict):
                    resolve_item(do_block.get("metrics"))
                    resolve_item(do_block.get("plots"))
        resolve_item(dvc_yaml_data.get("plots"))
        resolve_item(dvc_yaml_data.get("metrics"))
        
    return paths, patterns

print()
print("=" * 60)
print("ORIGIN/MAIN dvc.yaml:")
print("=" * 60)
paths_main, pats_main = extract_metrics_and_plots_paths(main_data)
print(f"  Plain paths: {len(paths_main)}")
for p in sorted(paths_main):
    print(f"    {p}")
print(f"  Regex patterns: {len(pats_main)}")
for p in pats_main:
    print(f"    {p.pattern}")

target = "results/metrics/recbole_metrics_EASE_tomplay.json"
matched = target in paths_main
for p in pats_main:
    if p.match(target):
        matched = True
        print(f"  >> Matched by: {p.pattern}")
print(f"  Target '{target}' matched: {matched}")

print()
print("=" * 60)
print("CLUSTER-DRAFT dvc.yaml:")
print("=" * 60)
paths_draft, pats_draft = extract_metrics_and_plots_paths(draft_data)
print(f"  Plain paths: {len(paths_draft)}")
for p in sorted(paths_draft):
    print(f"    {p}")
print(f"  Regex patterns: {len(pats_draft)}")
for p in pats_draft:
    print(f"    {p.pattern}")

matched2 = target in paths_draft
for p in pats_draft:
    if p.match(target):
        matched2 = True
        print(f"  >> Matched by: {p.pattern}")
print(f"  Target '{target}' matched: {matched2}")

# 4. Test re.escape behavior
print()
print("=" * 60)
print("re.escape BEHAVIOR TEST:")
print("=" * 60)
test_str = "abc_${item}.json"
escaped = re.escape(test_str)
print(f"  re.escape('{test_str}') = {escaped!r}")
# Test the sub
sub1 = re.sub(r'\\\\\\$\\\\\\{[^}]+\\\\\\}', '.*', escaped)
print(f"  After sub (code pattern): {sub1!r}")
# Is it the same?
print(f"  Substitution worked: {sub1 != escaped}")

# 5. Check what files exist at origin/main vs cluster-draft
print()
print("=" * 60)
print("FILES IN GIT:")
print("=" * 60)
# origin/main
res_files_main = subprocess.run(
    ["git", "ls-tree", "-r", "--name-only", "origin/main"],
    capture_output=True, text=True,
    cwd="/home/henri/cluster-ci/repositories/UNIL-DESI/llm-as-recommender"
)
main_files = set(res_files_main.stdout.strip().split("\\n"))

# cluster-draft
res_files_draft = subprocess.run(
    ["git", "ls-tree", "-r", "--name-only", "origin/cluster-draft/hjamet"],
    capture_output=True, text=True,
    cwd="/home/henri/cluster-ci/repositories/UNIL-DESI/llm-as-recommender"
)
draft_files = set(res_files_draft.stdout.strip().split("\\n"))

# Count matching artifacts in each
def count_matches(files, paths, patterns):
    matched = []
    for f in files:
        if f in paths:
            matched.append(f)
        else:
            for p in patterns:
                if p.match(f):
                    matched.append(f)
                    break
    return matched

main_matches = count_matches(main_files, paths_main, pats_main)
draft_matches = count_matches(draft_files, paths_draft, pats_draft)

print(f"  origin/main: {len(main_matches)} matching artifacts")
for m in sorted(main_matches):
    print(f"    {m}")
print(f"  cluster-draft: {len(draft_matches)} matching artifacts")
for m in sorted(draft_matches):
    print(f"    {m}")
"""
        
        # Write script to remote
        with sftp.file('/tmp/test_artifacts.py', 'w') as f:
            f.write(remote_script)
        sftp.close()
        
        # Execute it
        print("Running test on cluster...")
        stdin, stdout, stderr = client.exec_command('python3 /tmp/test_artifacts.py', timeout=30)
        out = stdout.read().decode('utf-8', errors='ignore')
        err = stderr.read().decode('utf-8', errors='ignore')
        if out.strip():
            print(out)
        if err.strip():
            print("STDERR:", err)
                
    except Exception as e:
        print(f'Connection failed: {e}')
        import traceback
        traceback.print_exc()
    finally:
        client.close()

if __name__ == '__main__':
    main()
