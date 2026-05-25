import urllib.request
import json

models = ["google/gemma-4-E4B-it", "google/gemma-4-26B-A4B-it"]

for model in models:
    url = f"https://huggingface.co/{model}/raw/main/config.json"
    print(f"--- Analyzing {model} ---")
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            config = json.loads(response.read().decode())
            
            # Helper to recursively search for keys
            moe_found = []
            def search_moe_keys(d, prefix=""):
                if isinstance(d, dict):
                    for k, v in d.items():
                        if 'expert' in k.lower() or 'moe' in k.lower():
                            moe_found.append((prefix + k, v))
                        search_moe_keys(v, prefix + k + ".")
            
            search_moe_keys(config)
            
            if moe_found:
                print(f"MoE keys found:")
                for k, v in moe_found:
                    print(f"  {k}: {v}")
            else:
                print("No MoE specific keys found anywhere in the config.")
            
            print(f"Architectures: {config.get('architectures', [])}")
            print(f"Model type: {config.get('model_type', 'N/A')}")
            
            if 'text_config' in config:
                print("Text Config keys:")
                for k, v in config['text_config'].items():
                    print(f"  {k}: {v}")
            
    except Exception as e:
        print(f"Error fetching {model}: {e}")
    print()
