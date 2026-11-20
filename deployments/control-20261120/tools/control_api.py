"""Control-host CLI; reads private UI auth locally and prints only API results."""
import base64
import json
from pathlib import Path
import sys
import urllib.request


def request(path, body=None):
    credentials = json.loads(Path('/etc/abc20261102/ui-login.json').read_text())
    auth = base64.b64encode((credentials['user'] + ':' + credentials['password']).encode()).decode()
    req = urllib.request.Request('http://127.0.0.1:28880/api/' + path,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Authorization': 'Basic ' + auth, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response)


if __name__ == '__main__':
    print(json.dumps(request(sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv) > 2 else None)))
