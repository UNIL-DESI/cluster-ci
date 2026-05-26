import paramiko
import sys
import io

# Force stdout and stderr to use UTF-8 and replace encoding errors to avoid Windows charmap crashes
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

def main():
    hostname = '130.223.73.209'
    username = 'henri'
    password = '^Jw6jQTVbsGc3cwc@v^%'
    
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        print(f"Connecting to {hostname}...")
        client.connect(hostname, username=username, password=password, timeout=15)
        
        # 1. Update dvc-viewer repo on remote
        print("\n--- STEP 1: Git Pull on remote dvc-viewer repo ---")
        cmd_git = "cd /home/henri/cluster-ci/repositories/UNIL-DESI/dvc-viewer && git reset --hard HEAD && git clean -fd && git pull origin main"
        stdin, stdout, stderr = client.exec_command(cmd_git)
        print("Git Pull STDOUT:\n" + stdout.read().decode('utf-8', errors='replace'))
        print("Git Pull STDERR:\n" + stderr.read().decode('utf-8', errors='replace'))
        
        # 2. Diagnosing Python & Environment on Remote
        print("\n--- STEP 2: Environment Diagnosis on Remote Hoster ---")
        diag_commands = [
            "python --version || echo 'python: not found'",
            "python3 --version || echo 'python3: not found'",
            "python3.10 --version || echo 'python3.10: not found'",
            "python3.12 --version || echo 'python3.12: not found'",
            "pip --version || echo 'pip: not found'",
            "pip3 --version || echo 'pip3: not found'",
            "uv --version || echo 'uv: not found'",
            "which dvc-viewer || echo 'dvc-viewer binary: not found'",
            "echo $PATH"
        ]
        
        for cmd in diag_commands:
            stdin, stdout, stderr = client.exec_command(cmd)
            out = stdout.read().decode('utf-8', errors='replace').strip()
            print(f"Command '{cmd}' -> {out}")

        # 3. Choose and execute installation strategy
        print("\n--- STEP 3: Trying installation strategies ---")
        strategies = [
            # Strategy A: Use `uv` if present (highly recommended as it manages tool chains cleanly)
            ("Strategy A: uv tool install", "export PATH=~/.local/bin:$PATH; cd /home/henri/cluster-ci/repositories/UNIL-DESI/dvc-viewer && uv tool install --force --editable ."),
            
            # Strategy B: Use uv pip install --user
            ("Strategy B: uv pip install", "export PATH=~/.local/bin:$PATH; cd /home/henri/cluster-ci/repositories/UNIL-DESI/dvc-viewer && uv pip install --user --upgrade ."),
            
            # Strategy C: Use python3.12 (if available)
            ("Strategy C: python3.12 -m pip", "export PATH=~/.local/bin:$PATH; cd /home/henri/cluster-ci/repositories/UNIL-DESI/dvc-viewer && python3.12 -m pip install --user --upgrade ."),
            
            # Strategy D: Use python3.10 (if available)
            ("Strategy D: python3.10 -m pip", "export PATH=~/.local/bin:$PATH; cd /home/henri/cluster-ci/repositories/UNIL-DESI/dvc-viewer && python3.10 -m pip install --user --upgrade ."),
            
            # Strategy E: Fallback python3 -m pip
            ("Strategy E: python3 -m pip", "export PATH=~/.local/bin:$PATH; cd /home/henri/cluster-ci/repositories/UNIL-DESI/dvc-viewer && python3 -m pip install --user --upgrade .")
        ]
        
        installed_ok = False
        for name, cmd in strategies:
            print(f"\nAttempting {name}...")
            stdin, stdout, stderr = client.exec_command(cmd)
            out_str = stdout.read().decode('utf-8', errors='replace')
            err_str = stderr.read().decode('utf-8', errors='replace')
            
            print(f"{name} STDOUT:\n" + out_str)
            print(f"{name} STDERR:\n" + err_str)
            
            # Check if successful (typically when error output doesn't contain fatal message and package is installed)
            if "Successfully installed" in out_str or "Installed" in out_str or "Updated" in out_str or "installed package" in out_str.lower():
                print(f"-> SUCCESS with {name}!")
                installed_ok = True
                break
            elif "requires a different Python" in err_str:
                print(f"-> SKIPPED {name} due to Python version conflict.")
            elif "not found" in err_str or "command not found" in err_str or "No such file" in err_str:
                print(f"-> SKIPPED {name} due to missing tool.")
            else:
                # Some command might output stuff to stderr (like pip deprecation warnings) but succeed.
                # Let's verify if the binary is functional
                print("Checking binary after attempt...")
                stdin, stdout, stderr = client.exec_command("export PATH=~/.local/bin:$PATH; dvc-viewer --help")
                help_out = stdout.read().decode('utf-8', errors='replace')
                if "--host" in help_out:
                    print(f"-> SUCCESS verified with {name}!")
                    installed_ok = True
                    break

        if not installed_ok:
            print("\n❌ ALL STANDARD STRATEGIES FAILED! Trying a dedicated Python Virtual Environment on host...")
            # Strategy F: Create a Python 3.10/3.12 or even standard python3 virtual env, and link the binary to ~/.local/bin/dvc-viewer
            venv_cmd = (
                "export PATH=~/.local/bin:$PATH; "
                "cd /home/henri/cluster-ci/repositories/UNIL-DESI/dvc-viewer && "
                "python3.10 -m venv venv && ./venv/bin/pip install --upgrade pip && ./venv/bin/pip install -e . && "
                "mkdir -p ~/.local/bin && ln -sf /home/henri/cluster-ci/repositories/UNIL-DESI/dvc-viewer/venv/bin/dvc-viewer ~/.local/bin/dvc-viewer"
            )
            print("Attempting Strategy F (Virtual Environment)...")
            stdin, stdout, stderr = client.exec_command(venv_cmd)
            print("Venv STDOUT:\n" + stdout.read().decode('utf-8', errors='replace'))
            print("Venv STDERR:\n" + stderr.read().decode('utf-8', errors='replace'))
            
        # 4. Final verification
        print("\n--- STEP 4: Final verification of dvc-viewer CLI version and help ---")
        cmd_verify = "export PATH=~/.local/bin:$PATH; dvc-viewer --help"
        stdin, stdout, stderr = client.exec_command(cmd_verify)
        help_output = stdout.read().decode('utf-8', errors='replace')
        print("Help STDOUT:\n" + help_output)
        
        if "--host" in help_output:
            print("\n🎉 SUCCESS: Remote dvc-viewer binary updated successfully and now supports '--host'!")
        else:
            print("\n❌ FAILURE: '--host' option not found in final verification help output.")
            sys.exit(1)
            
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
    finally:
        client.close()

if __name__ == '__main__':
    main()

