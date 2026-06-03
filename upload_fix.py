import paramiko

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("130.223.73.209", username="henri", password="^Jw6jQTVbsGc3cwc@v^%")

sftp = client.open_sftp()
sftp.put(r"C:\Users\hjamet\Documents\code\cluster-ci\src\scheduler\runner_manager.py", "/home/henri/cluster-ci/src/scheduler/runner_manager.py")
sftp.close()

# Kill the old runner_manager (it will be restarted by systemd or we can restart the service)
stdin, stdout, stderr = client.exec_command("sudo systemctl restart cluster-ci-runners")
print(stdout.read().decode())
print(stderr.read().decode())

# Also kill the zombie runner listeners
client.exec_command("pkill -f Runner.Listener")

client.close()
print("Upload and restart complete!")
