import paramiko

def main():
    hostname = '130.223.73.209'
    username = 'henri'
    password = '^Jw6jQTVbsGc3cwc@v^%'
    
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        print(f"Connecting to {hostname}...")
        client.connect(hostname, username=username, password=password, timeout=15)
        
        # We read the file content
        print("Reading remote pyproject.toml...")
        sftp = client.open_sftp()
        toml_path = '/home/henri/cluster-ci/repositories/UNIL-DESI/llm-as-recommender/pyproject.toml'
        
        with sftp.open(toml_path, 'r') as f:
            content = f.read().decode('utf-8')
            
        old_dep = 'dvc-viewer = { git = "https://github.com/UNIL-Henri/dvc-viewer.git", rev = "5a1dc219d3d7f476bc2b8e9d84c39b24425bbf30" }'
        new_dep = 'dvc-viewer = { path = "../dvc-viewer" }'
        
        if old_dep in content:
            print("Found old dependency format, replacing it...")
            content = content.replace(old_dep, new_dep)
        elif new_dep in content:
            print("Already modified to use path!")
        else:
            print("Warning: old dependency not found exactly. Content snippet:")
            print("\n".join(content.splitlines()[-15:]))
            # Let's try replacing a partial match just in case
            content = content.replace('dvc-viewer = { git = "https://github.com/UNIL-Henri/dvc-viewer.git", rev = "5a1dc219d3d7f476bc2b8e9d84c39b24425bbf30" }', new_dep)
            
        # Write back the content
        with sftp.open(toml_path, 'w') as f:
            f.write(content.encode('utf-8'))
            
        print("Successfully updated pyproject.toml!")
        sftp.close()
        
    except Exception as e:
        print(f"Error: {e}")
    finally:
        client.close()

if __name__ == '__main__':
    main()
