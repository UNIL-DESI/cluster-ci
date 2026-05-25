import paramiko
import sys

def run_headnode_logs(ip, user, password):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    results = []
    try:
        client.connect(ip, username=user, password=password, timeout=10)
        results.append(f"CONNECTED TO HEADNODE: {ip}\n")
        
        # Let's check system scheduler API service logs
        journal_cmd = f"echo '{password}' | sudo -S journalctl -u cluster-scheduler -n 250 --no-pager"
        results.append(f"\nSCHEDULER API LOGS:\n")
        stdin, stdout, stderr = client.exec_command(journal_cmd)
        results.append(stdout.read().decode('utf-8', errors='ignore'))

    except Exception as e:
        results.append(f"Failed to fetch logs: {e}")
    finally:
        client.close()
    return "\n".join(results)

output = run_headnode_logs("130.223.73.209", "henri", "^Jw6jQTVbsGc3cwc@v^%")
with open("scratch/headnode_logs.txt", "w", encoding="utf-8") as f:
    f.write(output)
print("Logs written to scratch/headnode_logs.txt")
