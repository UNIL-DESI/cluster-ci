import paramiko

def fetch_dmesg(ip, user, password):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(ip, username=user, password=password, timeout=10)
        # Use sudo with password
        cmd = f"echo '{password}' | sudo -S dmesg -T | grep -i oom | tail -n 20"
        stdin, stdout, stderr = client.exec_command(cmd)
        out = stdout.read().decode('utf-8')
        print(f"--- {ip} DMESG OOM ---")
        if out: print(out)
        
        cmd2 = f"echo '{password}' | sudo -S dmesg -T | grep -i kill | tail -n 20"
        stdin, stdout, stderr = client.exec_command(cmd2)
        out2 = stdout.read().decode('utf-8')
        print(f"--- {ip} DMESG KILL ---")
        if out2: print(out2)
        
        # Also check journalctl for cluster-worker
        cmd3 = f"echo '{password}' | sudo -S journalctl -u cluster-worker -n 100 --no-pager"
        stdin, stdout, stderr = client.exec_command(cmd3)
        out3 = stdout.read().decode('utf-8')
        print(f"--- {ip} CLUSTER-WORKER LOG ---")
        if out3: print(out3[-2000:])  # last 2000 chars
        
        # Check docker stats or events
        cmd4 = f"echo '{password}' | sudo -S docker events --since 24h --filter event=oom"
        # actually docker events is streaming, we can't just run it without timeout.

    except Exception as e:
        print(f"Failed to connect to {ip}: {e}")
    finally:
        client.close()

fetch_dmesg("130.223.170.123", "henri", "pokemone")
fetch_dmesg("130.223.169.200", "henri", "pokemone")
