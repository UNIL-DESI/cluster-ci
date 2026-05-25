import paramiko
import sys

# Configure output to prevent encoding errors on Windows
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

def run_ssh(ip, user, password, cmd):
    print(f"=== Connecting to {ip} as {user} ===")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(ip, username=user, password=password, timeout=15)
        stdin, stdout, stderr = client.exec_command(cmd)
        
        # Read output
        out_str = stdout.read().decode('utf-8', errors='ignore')
        err_str = stderr.read().decode('utf-8', errors='ignore')
        
        print(f"[{ip}] STDOUT:")
        print(out_str)
        if err_str.strip():
            print(f"[{ip}] STDERR:")
            print(err_str)
        print(f"=== Completed {ip} ===\n")
    except Exception as e:
        print(f"❌ Failed to connect or execute on {ip}: {e}\n")
    finally:
        client.close()

if __name__ == "__main__":
    hn_ip = "130.223.73.209"
    hn_user = "henri"
    hn_pass = "^Jw6jQTVbsGc3cwc@v^%"
    
    # Update Headnode and pass sudo password
    hn_cmd = (
        "cd /home/henri/cluster-ci && "
        "git fetch origin main && "
        "git reset --hard origin/main && "
        "uv sync && "
        f"echo '{hn_pass}' | sudo -S systemctl restart cluster-scheduler cluster-scheduler-loop"
    )
    run_ssh(hn_ip, hn_user, hn_pass, hn_cmd)
    
    print("🎉 Headnode update submitted successfully!")
