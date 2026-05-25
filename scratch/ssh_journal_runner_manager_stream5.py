import paramiko
import sys
import time

# Force utf-8 stdout
sys.stdout.reconfigure(encoding='utf-8')

def main():
    hostname = '130.223.73.209'
    username = 'henri'
    password = '^Jw6jQTVbsGc3cwc@v^%'
    
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        print(f'Connecting to {hostname}...')
        client.connect(hostname, username=username, password=password, timeout=10)
        
        print('Checking orchestrator script logs...')
        
        # Continuous loop to monitor journalctl for Runner_*.log being deleted or updated
        stdin, stdout, stderr = client.exec_command('sudo -S journalctl -u cluster-runner-manager -f')
        stdin.write(password + '\n')
        stdin.flush()
        
        # Read the stream for 20 seconds
        start_time = time.time()
        while time.time() - start_time < 30:
            if stdout.channel.recv_ready():
                output = stdout.channel.recv(4096).decode('utf-8', errors='ignore')
                print(output, end='')
            time.sleep(0.5)
            
    except Exception as e:
        print(f'Connection failed: {e}')
        sys.exit(1)
    finally:
        client.close()

if __name__ == '__main__':
    main()
