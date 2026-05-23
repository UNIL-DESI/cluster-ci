import paramiko
import sys

def run_ssh(cmd):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("130.223.73.209", username="henri", password="^Jw6jQTVbsGc3cwc@v^%")
    stdin, stdout, stderr = client.exec_command(cmd)
    print("STDOUT:", stdout.read().decode())
    print("STDERR:", stderr.read().decode())
    client.close()

if __name__ == "__main__":
    run_ssh(sys.argv[1])
