import paramiko
import sys

# Reconfigure stdout to use UTF-8 to prevent encoding errors on Windows when printing special characters
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

def run_ssh(cmd):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("130.223.73.209", username="henri", password="^Jw6jQTVbsGc3cwc@v^%")
    stdin, stdout, stderr = client.exec_command(cmd)
    
    # Read and decode with utf-8, ignoring decoding errors
    out_str = stdout.read().decode('utf-8', errors='ignore')
    err_str = stderr.read().decode('utf-8', errors='ignore')
    
    print("STDOUT:", out_str)
    print("STDERR:", err_str)
    client.close()

if __name__ == "__main__":
    run_ssh(sys.argv[1])

