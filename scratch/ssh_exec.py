import sys
import io
import paramiko

# Force stdout and stderr to use UTF-8 and replace encoding errors to avoid Windows charmap crashes
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

def ssh_exec(host, username, password, command):
    print(f"[{host}] Executing: {command}")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host, username=username, password=password, timeout=15)
        stdin, stdout, stderr = client.exec_command(command)
        
        # Read output stream-like
        stdout_str = stdout.read().decode('utf-8', errors='replace')
        stderr_str = stderr.read().decode('utf-8', errors='replace')
        exit_status = stdout.channel.recv_exit_status()
        
        print(f"[{host}] Exit code: {exit_status}")
        if stdout_str:
            print(f"--- STDOUT ---\n{stdout_str}")
        if stderr_str:
            print(f"--- STDERR ---\n{stderr_str}")
        return exit_status, stdout_str, stderr_str
    except Exception as e:
        print(f"[{host}] SSH Error: {e}")
        return -1, "", str(e)
    finally:
        client.close()

if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("Usage: python ssh_exec.py <host> <username> <password> <command>")
        sys.exit(1)
    host = sys.argv[1]
    username = sys.argv[2]
    password = sys.argv[3]
    command = " ".join(sys.argv[4:])
    
    exit_code, _, _ = ssh_exec(host, username, password, command)
    sys.exit(exit_code)
