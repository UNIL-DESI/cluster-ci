import paramiko
import sys

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
        
        print('Checking logs for SIGKILL in syslog...')
        stdin, stdout, stderr = client.exec_command('sudo -S grep "SIGKILL to runner listener" /var/log/syslog | tail -n 20')
        stdin.write(password + '\n')
        stdin.flush()
        
        output = stdout.read().decode('utf-8', errors='ignore')
        error = stderr.read().decode('utf-8', errors='ignore')
        
        print('Syslog Output:\n' + output)
        if error:
            print('Syslog Error:\n' + error)
            
    except Exception as e:
        print(f'Connection failed: {e}')
        sys.exit(1)
    finally:
        client.close()

if __name__ == '__main__':
    main()
