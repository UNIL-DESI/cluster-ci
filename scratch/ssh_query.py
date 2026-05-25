import paramiko
import sys

# Reconfigure stdout to use UTF-8 to prevent encoding errors on Windows
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

def run_ssh(ip, user, password, cmd):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(ip, username=user, password=password, timeout=10)
        stdin, stdout, stderr = client.exec_command(cmd)
        out_str = stdout.read().decode('utf-8', errors='ignore')
        err_str = stderr.read().decode('utf-8', errors='ignore')
        print(f"=== {ip} STDOUT ===")
        print(out_str)
        if err_str.strip():
            print(f"=== {ip} STDERR ===")
            print(err_str)
    except Exception as e:
        print(f"Failed to connect to {ip}: {e}")
    finally:
        client.close()

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python ssh_query.py [ip_type] [cmd]")
        sys.exit(1)
        
    ip_type = sys.argv[1]
    cmd = " ".join(sys.argv[2:])
    
    if ip_type == "headnode" or ip_type == "hn":
        run_ssh("130.223.73.209", "henri", "^Jw6jQTVbsGc3cwc@v^%", cmd)
    elif ip_type == "worker1" or ip_type == "w1" or ip_type == "123":
        run_ssh("130.223.170.123", "henri", "pokemone", cmd)
    elif ip_type == "worker2" or ip_type == "w2" or ip_type == "200":
        run_ssh("130.223.169.200", "henri", "pokemone", cmd)
    else:
        print(f"Unknown ip_type: {ip_type}")
