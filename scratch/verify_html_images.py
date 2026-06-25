import os
import re
import urllib.parse

def verify_site_and_images(site_dir):
    print(f"Starting verification of directory: {site_dir}")
    if not os.path.isdir(site_dir):
        print(f"Error: {site_dir} is not a directory.")
        return False

    all_html_files = []
    broken_images = []
    found_images = 0
    checked_images = []

    # Regex to find <img src="..." ...>
    img_regex = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)

    for root, dirs, files in os.walk(site_dir):
        for file in files:
            if file.endswith('.html'):
                filepath = os.path.join(root, file)
                all_html_files.append(filepath)
                size = os.path.getsize(filepath)
                print(f"HTML File: {filepath} ({size} bytes)")
                
                if size == 0:
                    broken_images.append((filepath, "File is empty", ""))
                    continue

                try:
                    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                except Exception as e:
                    broken_images.append((filepath, f"Error reading file: {str(e)}", ""))
                    continue

                # Find all img tags
                matches = img_regex.findall(content)
                for src in matches:
                    found_images += 1
                    # Skip external or data URIs
                    if src.startswith(('http://', 'https://', 'data:', '//')):
                        print(f"  - Ext/Data Image: {src}")
                        continue
                    
                    # Unquote URL path (e.g. %20 -> space)
                    parsed_src = urllib.parse.unquote(src)
                    
                    # MkDocs uses relative paths or root-relative paths
                    # Let's resolve the path relative to the html file
                    html_dir = os.path.dirname(filepath)
                    image_path = os.path.abspath(os.path.join(html_dir, parsed_src))
                    
                    # Check if it exists
                    checked_images.append((filepath, src, image_path))
                    if not os.path.exists(image_path):
                        broken_images.append((filepath, f"Image does not exist at resolved path: {image_path}", src))
                    elif not os.path.isfile(image_path):
                        broken_images.append((filepath, f"Resolved path is not a file: {image_path}", src))
                    else:
                        img_size = os.path.getsize(image_path)
                        if img_size == 0:
                            broken_images.append((filepath, f"Image file is empty: {image_path}", src))
                        else:
                            # Verify PNG signature if it's a png
                            if image_path.lower().endswith('.png'):
                                try:
                                    with open(image_path, 'rb') as img_f:
                                        sig = img_f.read(8)
                                        if sig != b'\x89PNG\r\n\x1a\n':
                                            broken_images.append((filepath, f"Invalid PNG signature at: {image_path}", src))
                                        else:
                                            print(f"  - OK: {src} -> {image_path} ({img_size} bytes)")
                                except Exception as e:
                                    broken_images.append((filepath, f"Cannot read image file: {str(e)}", src))
                            else:
                                print(f"  - OK (Non-PNG): {src} -> {image_path} ({img_size} bytes)")

    print("\n--- Summary of Verification ---")
    print(f"Total HTML files checked: {len(all_html_files)}")
    print(f"Total images referenced: {found_images}")
    print(f"Total relative images checked: {len(checked_images)}")
    
    if broken_images:
        print(f"\n[FAIL] Found {len(broken_images)} issues:")
        for html_file, err, src in broken_images:
            print(f"  In {html_file}:")
            print(f"    Referenced src: '{src}'")
            print(f"    Error: {err}")
        return False
    else:
        print("\n[PASS] All HTML files and images are verified and valid!")
        return True

if __name__ == "__main__":
    import sys
    success = verify_site_and_images("site")
    sys.exit(0 if success else 1)
