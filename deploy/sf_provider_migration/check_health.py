#!/usr/bin/env python3
"""Only wait for the verified native maintenance response, never arbitrary errors."""
import json
import subprocess
import sys
import time
import urllib.request

backend, site, *options = sys.argv[1:]
assert options in ([], ["--maintenance-cleared"])
code = r'''
import json,sys,urllib.request,urllib.error
request=urllib.request.Request('http://127.0.0.1:8000/api/method/frappe.ping',headers={'Host':sys.argv[1]})
try:
    with urllib.request.urlopen(request,timeout=10) as response:
        assert json.load(response)=={'message':'pong'}
except urllib.error.HTTPError as error:
    body=error.read(8192).decode('utf-8','replace')
    try:
        payload=json.loads(body)
    except (ValueError, TypeError):
        payload={}
    if error.code==503 and isinstance(payload,dict) and payload.get('exc_type')=='SessionStopped':
        raise SystemExit(75)
    print(json.dumps({'health_http_status':error.code,'response':body[:1024]}),file=sys.stderr)
    raise
'''
# Frappe site_cache(ttl=60) is per worker; the Bench CLI clears only its own
# process. One early pong cannot prove every Gunicorn worker has fresh config.
started = time.monotonic()
settled_at = started + 61 if options else started
deadline = started + 75
while True:
    result = subprocess.run(['docker', 'exec', backend, '/home/frappe/frappe-bench/env/bin/python', '-c', code, site], timeout=15)
    if result.returncode == 0 and time.monotonic() >= settled_at:
        break
    if result.returncode not in (0, 75) or time.monotonic() >= deadline:
        raise SystemExit('INTERNAL_HEALTH_FAILED')
    time.sleep(2)
# Cloudflare browser integrity rejects Python-urllib (1010), while this
# observed request header succeeds from the same host. Keep checking JSON/status.
request = urllib.request.Request(
    f'https://{site}/api/method/frappe.ping', headers={'User-Agent': 'Mozilla/5.0'}
)
with urllib.request.urlopen(request, timeout=15) as response:
    assert json.load(response) == {'message': 'pong'}
print('INTERNAL_AND_PUBLIC_PING_OK')
