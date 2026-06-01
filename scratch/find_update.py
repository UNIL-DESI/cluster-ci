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
        
        # Find cluster-update
        commands = [
            "which cluster-update 2>/dev/null || echo 'NOT IN PATH'",
            "find /home/henri -name 'cluster-update*' -maxdepth 3 2>/dev/null",
            "ls -la /home/henri/cluster-ci/cluster-update 2>/dev/null || echo 'NOT FOUND'",
            "ls -la /home/henri/cluster-ci/*.sh 2>/dev/null || echo 'NO .sh files'",
            # Maybe it's just a git pull + systemctl restart
            "systemctl list-units --type=service | grep -i cluster",
            # Check for any service running
            "ps aux | grep -i 'headnode_service\|cluster.*scheduler' | grep -v grep",
        ]
        
        for cmd in commands:
            print(f"\n=== {cmd[:60]} ===")
            stdin, stdout, stderr = client.exec_command(cmd, timeout=10)
            out = stdout.read().decode('utf-8', errors='ignore')
            err = stderr.read().decode('utf-8', errors='ignore')
            if out.strip():
                print(out.strip())
            if err.strip():
                print("ERR:", err.strip())
                
    except Exception as e:
        print(f'Failed: {e}')
    finally:
        client.close()

if __name__ == '__main__':
    main()
