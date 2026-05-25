import paramiko
import sys

def main():
    hostname = '130.223.170.123'
    username = 'henri'
    password = 'pokemone'
    
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        print(f'Connecting to {hostname}...')
        client.connect(hostname, username=username, password=password, timeout=10)
        
        print('Executing command...')
        stdin, stdout, stderr = client.exec_command('sudo -S systemctl status actions.runner.*')
        stdin.write(password + '\n')
        stdin.flush()
        
        output = stdout.read().decode()
        error = stderr.read().decode()
        
        print('Output:\n' + output)
        if error:
            print('Error:\n' + error)
            
        print('Restarting runner...')
        stdin, stdout, stderr = client.exec_command('sudo -S systemctl restart actions.runner.*')
        stdin.write(password + '\n')
        stdin.flush()
        
        output = stdout.read().decode()
        error = stderr.read().decode()
        
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
