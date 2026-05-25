import paramiko
import sys

# Force utf-8 stdout
sys.stdout.reconfigure(encoding='utf-8')

def main():
    hostname = '130.223.170.123'
    username = 'henri'
    password = '^Jw6jQTVbsGc3cwc@v^%'
    
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        print(f'Connecting to {hostname}...')
        client.connect(hostname, username=username, password=password, timeout=10)
        
        print('Pulling changes on Worker...')
        stdin, stdout, stderr = client.exec_command('cd /home/henri/cluster-ci && git pull origin main')
        
        output = stdout.read().decode('utf-8', errors='ignore')
        error = stderr.read().decode('utf-8', errors='ignore')
        
        print('Pull Output:\n' + output)
        if error:
            print('Pull Error:\n' + error)
            
        print('Restarting orchestrator...')
        stdin, stdout, stderr = client.exec_command('sudo -S systemctl restart cluster-scheduler cluster-scheduler-loop cluster-runner-manager')
        stdin.write(password + '\n')
        stdin.flush()
        
        output = stdout.read().decode('utf-8', errors='ignore')
        error = stderr.read().decode('utf-8', errors='ignore')
        
        print('Restart Output:\n' + output)
        if error:
            print('Restart Error:\n' + error)
            
    except Exception as e:
        print(f'Connection failed: {e}')
        sys.exit(1)
    finally:
        client.close()

if __name__ == '__main__':
    main()
