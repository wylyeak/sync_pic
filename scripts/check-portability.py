"""Reject deployment artifacts and obvious machine-specific settings."""
from pathlib import Path
import re
import subprocess
root = Path(__file__).resolve().parents[1]
files = subprocess.run(['git', 'ls-files', '-z'], cwd=root, check=True,
                       capture_output=True).stdout.decode().split('\0')
errors = []
for name in filter(None, files):
    path = root/name
    if path.suffix in ('.png', '.jpg', '.db', '.sqlite', '.pem', '.key', '.log') or name == '.env' or name.endswith('/config.toml'):
        errors.append(name + ': runtime/private artifact')
        continue
    text = path.read_text()
    patterns = (r'/home/[A-Za-z0-9_.-]+/', r'192\.168\.\d+\.\d+',
                r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----',
                r'https?://[^\s/]+:[^\s@]+@')
    if any(re.search(pattern, text) for pattern in patterns):
        errors.append(name + ': local address, home path, or embedded credential')
if errors:
    raise SystemExit('\n'.join(errors))
print(f'Portability scan passed: {len(list(filter(None, files)))} tracked files')
