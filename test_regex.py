import re
with open("test.log", "r", encoding="utf-8") as f:
    text = f.read()
match = re.search(r'Exit code 137|OOM|Out of Memory|exited with -9', text, re.IGNORECASE)
if match:
    print(f"MATCH FOUND: '{match.group(0)}' at index {match.start()}")
else:
    print("NO MATCH")
