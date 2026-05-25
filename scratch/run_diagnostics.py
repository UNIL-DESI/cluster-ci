import paramiko
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

def run_ssh(ip, user, password, cmd):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(ip, username=user, password=password, timeout=15)
        if "sudo " in cmd and not "-S" in cmd:
            cmd = cmd.replace("sudo ", f"echo '{password}' | sudo -S ")
        stdin, stdout, stderr = client.exec_command(cmd)
        out_str = stdout.read().decode('utf-8', errors='replace')
        err_str = stderr.read().decode('utf-8', errors='replace')
        print(f"=== {ip} STDOUT ===")
        print(out_str)
        if err_str.strip():
            print(f"=== {ip} STDERR ===")
            print(err_str)
        return out_str, err_str
    except Exception as e:
        print(f"Failed to connect to {ip}: {e}")
        return "", str(e)
    finally:
        client.close()

if __name__ == "__main__":
    hn_ip = "130.223.73.209"
    hn_pass = "^Jw6jQTVbsGc3cwc@v^%"
    
    print("--- PROCESSES ON HEADNODE MONITORING ---")
    run_ssh(hn_ip, "henri", hn_pass, "ps aux | grep -E 'submit_job|curl|tee|cluster-ci-run'")
