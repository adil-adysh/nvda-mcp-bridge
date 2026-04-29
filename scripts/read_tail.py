from pathlib import Path

path = Path('scripts/test_http_bridge_output.txt')
text = path.read_text(encoding='utf-8', errors='replace').splitlines()
for line in text[-60:]:
    print(line)
