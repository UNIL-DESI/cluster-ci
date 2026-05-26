import paramiko
import sys

# Reconfigure stdout to use UTF-8 to prevent encoding errors on Windows when printing special characters
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

def run_ssh(ip, user, password, cmd):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(ip, username=user, password=password)
    stdin, stdout, stderr = client.exec_command(cmd)
    
    # Read and decode with utf-8, ignoring decoding errors
    out_str = stdout.read().decode('utf-8', errors='ignore')
    err_str = stderr.read().decode('utf-8', errors='ignore')
    
    print("STDOUT:", out_str)
    print("STDERR:", err_str)
    client.close()

if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("Usage: python ssh_cmd.py <ip> <user> <pass> <cmd>")
        sys.exit(1)
    run_ssh(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])

