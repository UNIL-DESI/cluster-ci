import paramiko
import sys

# Force utf-8 stdout
sys.stdout.reconfigure(encoding='utf-8')

def main():
    hostname = '130.223.170.123'
    username = 'henri'
    password = 'pokemone'
    
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        print(f'Connecting to {hostname}...')
        client.connect(hostname, username=username, password=password, timeout=10)
        
        print('Checking dmesg for OOM/kill with sudo...')
        stdin, stdout, stderr = client.exec_command('sudo -S dmesg -T | grep -i "oom\|kill\|memory\|thermal" | tail -n 20')
        stdin.write(password + '\n')
        stdin.flush()
        
        output = stdout.read().decode('utf-8', errors='ignore')
        error = stderr.read().decode('utf-8', errors='ignore')
        
        print('Dmesg Output:\n' + output)
        if error:
            print('Dmesg Error:\n' + error)
            
    except Exception as e:
        print(f'Connection failed: {e}')
        sys.exit(1)
    finally:
        client.close()

if __name__ == '__main__':
    main()
