import paramiko
import sys

def run(cmd):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("130.223.73.209", username="henri", password="^Jw6jQTVbsGc3cwc@v^%")
    stdin, stdout, stderr = client.exec_command(cmd)
    out = stdout.read().decode('utf-8')
    err = stderr.read().decode('utf-8')
    client.close()
    if out: print(out)
    if err: print("STDERR:", err)

if __name__ == "__main__":
    run(sys.argv[1])
