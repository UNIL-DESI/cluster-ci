import os

def verify_html_files(directory):
    html_files = []
    invalid_files = []
    
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith('.html'):
                filepath = os.path.join(root, file)
                html_files.append(filepath)
                try:
                    size = os.path.getsize(filepath)
                    if size == 0:
                        invalid_files.append((filepath, "File is empty"))
                        continue
                    
                    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                        
                    # Basic checks
                    if not content.strip():
                        invalid_files.append((filepath, "Content is whitespace only"))
                    elif "<html" not in content.lower():
                        invalid_files.append((filepath, "Missing <html> tag"))
                    elif "</html>" not in content.lower() and "</body>" not in content.lower():
                        invalid_files.append((filepath, "Missing closing </html> or </body> tag (potentially truncated)"))
                except Exception as e:
                    invalid_files.append((filepath, f"Error reading file: {str(e)}"))
                    
    return html_files, invalid_files

if __name__ == "__main__":
    site_dir = "site"
    if not os.path.exists(site_dir):
        print(f"Directory '{site_dir}' does not exist.")
        exit(1)
        
    all_htmls, val_errors = verify_html_files(site_dir)
    print(f"Total HTML files found: {len(all_htmls)}")
    if val_errors:
        print(f"Found {len(val_errors)} invalid files:")
        for path, err in val_errors:
            print(f" - {path}: {err}")
        exit(1)
    else:
        print("All HTML files verified successfully.")
        exit(0)
