import paramiko
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')

def run_cmd(client, cmd, timeout=30, sudo_pass=None):
    print(f"\n> {cmd}")
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    if sudo_pass:
        stdin.write(sudo_pass + '\n')
        stdin.flush()
    out = stdout.read().decode('utf-8', errors='ignore').strip()
    err = stderr.read().decode('utf-8', errors='ignore').strip()
    if out:
        print(out)
    if err:
        print(f"STDERR: {err}")
    return out, err

def main():
    hostname = '130.223.73.209'
    username = 'henri'
    password = '^Jw6jQTVbsGc3cwc@v^%'
    
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        print(f'Connecting to {hostname}...')
        client.connect(hostname, username=username, password=password, timeout=10)
        
        # 1. Git pull latest code
        run_cmd(client, 'cd ~/cluster-ci && git fetch origin main && git reset --hard origin/main')
        
        # 2. Restart the headnode scheduler service
        run_cmd(client, 'sudo -S systemctl restart cluster-scheduler.service', sudo_pass=password)
        
        # 3. Wait a moment and verify it's running
        time.sleep(3)
        run_cmd(client, 'systemctl is-active cluster-scheduler.service')
        
        # 4. Also restart the scheduler loop (uses the same codebase)
        run_cmd(client, 'sudo -S systemctl restart cluster-scheduler-loop.service', sudo_pass=password)
        time.sleep(2)
        run_cmd(client, 'systemctl is-active cluster-scheduler-loop.service')
        
        print('\n✅ Deployment complete!')
        
    except Exception as e:
        print(f'Connection failed: {e}')
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        client.close()

if __name__ == '__main__':
    main()
