import paramiko
import time
import threading
import sys
import io

# Force stdout and stderr to use UTF-8 and replace encoding errors to avoid Windows charmap crashes
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

def run_server(client, host):
    command = "export PATH=~/.local/bin:$PATH; cd /home/henri/cluster-ci/repositories/UNIL-DESI/llm-as-recommender && dvc-viewer --port 8686 --host 127.0.0.1"
    print(f"[{host}] Thread: Starting server with command: {command}")
    try:
        stdin, stdout, stderr = client.exec_command(command)
        # Read output line by line or fully once completed
        # Since it runs until self-destruction, this will block until then.
        stdout_str = stdout.read().decode('utf-8', errors='replace')
        stderr_str = stderr.read().decode('utf-8', errors='replace')
        exit_status = stdout.channel.recv_exit_status()
        print(f"[{host}] Thread: Server exited with status {exit_status}")
        if stdout_str:
            print(f"[{host}] Thread STDOUT:\n{stdout_str}")
        if stderr_str:
            print(f"[{host}] Thread STDERR:\n{stderr_str}")
    except Exception as e:
        print(f"[{host}] Thread Exception: {e}")

def main():
    host = '130.223.73.209'
    username = 'henri'
    password = '^Jw6jQTVbsGc3cwc@v^%'
    
    # 1. First client for the server execution
    server_client = paramiko.SSHClient()
    server_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    # 2. Second client for issuing curl pings
    ping_client = paramiko.SSHClient()
    ping_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        print(f"Connecting to {host}...")
        server_client.connect(host, username=username, password=password, timeout=15)
        ping_client.connect(host, username=username, password=password, timeout=15)
        print("Connected successfully on both SSH sessions.")
        
        # Start server in a background thread
        server_thread = threading.Thread(target=run_server, args=(server_client, host))
        server_thread.daemon = True
        server_thread.start()
        
        # Wait a few seconds for startup
        print("Waiting 4 seconds for the server to initialize...")
        time.sleep(4)
        
        # Ping function
        def ping():
            stdin, stdout, stderr = ping_client.exec_command("curl -s http://127.0.0.1:8686/api/heartbeat")
            out = stdout.read().decode('utf-8').strip()
            err = stderr.read().decode('utf-8').strip()
            return out, err
        
        # Phase 1: Heartbeat verification & Keep-alive
        print("\n--- PHASE 1: Active Heartbeat & Keep-Alive Verification ---")
        for i in range(4):
            print(f"Ping #{i+1} at T={i*4}s...")
            out, err = ping()
            print(f"Response: {out}")
            if err:
                print(f"Ping Error: {err}")
            time.sleep(4)
            
        print("\n--- PHASE 2: Inactivity & Self-destruction Validation ---")
        print("Stopping heartbeat pings. The server should auto-destruct after 15s of inactivity.")
        print("Waiting 20 seconds...")
        for remaining in range(20, 0, -5):
            print(f"Time remaining: {remaining}s...")
            time.sleep(5)
            
        print("\nChecking if server is still active (this ping should fail or receive no response)...")
        out, err = ping()
        if not out:
            print("SUCCESS: No response received. The server has successfully self-destructed due to inactivity!")
        else:
            print(f"WARNING: Server is still active and responded with: {out}")
            
        print("Waiting for server thread to join...")
        server_thread.join(timeout=5)
        
    except Exception as e:
        print(f"Error during validation: {e}")
    finally:
        server_client.close()
        ping_client.close()
        print("SSH Connections closed.")

if __name__ == '__main__':
    main()
