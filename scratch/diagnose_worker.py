import paramiko
import sys

def run_commands(ip, user, password, commands):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    results = []
    try:
        client.connect(ip, username=user, password=password, timeout=10)
        results.append(f"CONNECTED TO WORKER: {ip}\n")
        
        for cmd in commands:
            results.append(f"\n==================================================")
            results.append(f"COMMAND: {cmd}")
            results.append(f"==================================================")
            
            if "sudo" in cmd and not "-S" in cmd:
                cmd_run = cmd.replace("sudo", f"echo '{password}' | sudo -S")
            else:
                cmd_run = cmd
            
            stdin, stdout, stderr = client.exec_command(cmd_run)
            out = stdout.read().decode('utf-8', errors='ignore')
            err = stderr.read().decode('utf-8', errors='ignore')
            if out:
                results.append(out)
            if err:
                results.append(f"STDERR:\n{err}")
    except Exception as e:
        results.append(f"Failed to connect to {ip}: {e}")
    finally:
        client.close()
    return "\n".join(results)

commands_list = [
    "ps aux | grep -E 'python|worker_agent|cluster-ci'",
    "docker ps -a",
    "docker images",
    "sudo journalctl -u cluster-worker -n 100 --no-pager",
    "ls -la ~/repositories/registry.json",
    "find ~/repositories -maxdepth 2"
]

output = run_commands("130.223.170.123", "henri", "pokemone", commands_list)
with open("scratch/worker_diagnostics.txt", "w", encoding="utf-8") as f:
    f.write(output)
print("Diagnostics written to scratch/worker_diagnostics.txt")
