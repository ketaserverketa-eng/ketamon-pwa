#!/usr/bin/env python3
"""
Migration helper: encrypt passwords in data/routers.json using Fernet key in
environment variable KETAMON_ROUTERS_KEY.

Usage: set KETAMON_ROUTERS_KEY then run this script once. It will create a
backup routers.json.bak and replace passwords with Fernet tokens.
"""
import os
import json
import shutil
from pathlib import Path

KEY = os.environ.get('KETAMON_ROUTERS_KEY')
BASE = Path(__file__).resolve().parents[1]
DATA = BASE / 'data' / 'routers.json'

if not DATA.exists():
    print('routers.json not found at', DATA)
    raise SystemExit(1)

if not KEY:
    print('KETAMON_ROUTERS_KEY not set. Aborting.')
    raise SystemExit(1)

try:
    from cryptography.fernet import Fernet
except Exception as e:
    print('cryptography not installed or import failed:', e)
    raise

try:
    f = Fernet(KEY.encode() if isinstance(KEY, str) else KEY)
except Exception as e:
    print('Invalid Fernet key:', e)
    raise SystemExit(1)

with open(DATA, 'r', encoding='utf-8') as fh:
    routers = json.load(fh)

if not isinstance(routers, list):
    print('Unexpected format in routers.json')
    raise SystemExit(1)

bak = DATA.with_suffix('.json.bak')
shutil.copy2(DATA, bak)
print('Backup written to', bak)

changed = 0
for r in routers:
    p = r.get('password')
    if p and isinstance(p, str):
        # detect likely already-crypted token (Fernet tokens start with 'gAAAA')
        if p.startswith('gAAAA'):
            continue
        try:
            token = f.encrypt(p.encode()).decode()
            r['password'] = token
            changed += 1
        except Exception as e:
            print('Failed to encrypt for router', r.get('name'), e)

if changed == 0:
    print('No passwords encrypted (none found or already encrypted).')
else:
    with open(DATA, 'w', encoding='utf-8') as fh:
        json.dump(routers, fh, indent=2, ensure_ascii=False)
    print('Encrypted', changed, 'router password(s) in', DATA)

print('Done')
