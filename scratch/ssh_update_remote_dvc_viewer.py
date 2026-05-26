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
        print("Pulling changes in remote dvc-viewer repo...")
        cmd_git = "cd /home/henri/cluster-ci/repositories/UNIL-DESI/dvc-viewer && git reset --hard HEAD && git clean -fd && git pull origin main"
        stdin, stdout, stderr = client.exec_command(cmd_git)
        print("Git Pull STDOUT:\n" + stdout.read().decode('utf-8', errors='replace'))
        print("Git Pull STDERR:\n" + stderr.read().decode('utf-8', errors='replace'))
        
        # 2. Re-install globally/user space on remote to make sure the CLI tool in PATH is updated!
        print("Updating global/user dvc-viewer installation...")
        cmd_install = "export PATH=~/.local/bin:$PATH; cd /home/henri/cluster-ci/repositories/UNIL-DESI/dvc-viewer && pip install --user --upgrade ."
        stdin, stdout, stderr = client.exec_command(cmd_install)
        print("Install STDOUT:\n" + stdout.read().decode('utf-8', errors='replace'))
        print("Install STDERR:\n" + stderr.read().decode('utf-8', errors='replace'))
        
        # 3. Verify the CLI now supports `--host` argument
        print("Verifying dvc-viewer CLI version and help...")
        cmd_verify = "export PATH=~/.local/bin:$PATH; dvc-viewer --help"
        stdin, stdout, stderr = client.exec_command(cmd_verify)
        help_output = stdout.read().decode('utf-8', errors='replace')
        print("Help STDOUT:\n" + help_output)
        
        if "--host" in help_output:
            print("SUCCESS: Remote dvc-viewer binary updated successfully and now supports '--host'!")
        else:
            print("WARNING: '--host' option not found in help output. Please check the logs.")
            
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
    finally:
        client.close()

if __name__ == '__main__':
    main()
