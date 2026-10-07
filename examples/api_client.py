#!/usr/bin/env python3
"""Read devices without publishing anything. Prompt for credentials locally."""
import argparse
import getpass
import json
import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    args = parser.parse_args()
    with httpx.Client(base_url=args.url.rstrip('/'), timeout=20) as client:
        response = client.post('/api/auth/login', json={'password': getpass.getpass('Administrator password: ')})
        response.raise_for_status()
        token = response.json()['access_token']
        devices = client.get('/api/devices', headers={'Authorization': f'Bearer {token}'})
        devices.raise_for_status()
        print(json.dumps(devices.json(), indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
