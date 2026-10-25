"""Stop this local application through its authenticated local UI API."""
import argparse
import json
from urllib.request import Request, urlopen

parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int, default=8765)
args = parser.parse_args()
base = 'http://127.0.0.1:' + str(args.port)
try:
    with urlopen(base + '/api/health', timeout=3) as response:
        health = json.load(response)
    if health.get('service') != 'family-health-bot':
        raise ValueError('Different service')
    with urlopen(base + '/api/state', timeout=3) as response:
        state = json.load(response)
    request = Request(base + '/api/shutdown', data=b'{}', headers={'Content-Type':'application/json', 'X-CSRF-Token':state['csrf']})
    with urlopen(request, timeout=3) as response:
        json.load(response)
    print('Family Health helper stopped.')
except Exception:
    print('The local helper is not running or could not be verified. No process was killed.')
