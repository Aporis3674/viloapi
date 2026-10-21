#!/usr/bin/env python3
"""Fixed SSH entrypoint: local API only. No arbitrary command execution."""
import base64
import json
import sys
import urllib.error
import urllib.request


def main(argv):
    if len(argv) != 3 or argv[1] not in ('status', 'client', 'delete') or len(argv[2]) > 90000:
        return 2
    try:
        body = base64.b64decode(argv[2], validate=True)
        if len(body) > 65536 or not isinstance(json.loads(body), dict):
            return 2
        config = json.load(open('/etc/vilo-node/node.json', encoding='utf-8'))
        req = urllib.request.Request('http://127.0.0.1:10086/v1/' + argv[1], data=body,
              headers={'Authorization': 'Bearer ' + config['syncSecret'], 'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=45) as response:
            result = response.read(65537)
            if len(result) > 65536:
                return 2
        print(result.decode())
        return 0
    except (ValueError, KeyError, urllib.error.URLError, TimeoutError, OSError):
        print(json.dumps({'ok': False, 'error': 'Node unavailable'}))
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
