import sys
import argparse
from py3xui import Api

parser = argparse.ArgumentParser()
parser.add_argument('--url', required=True)
parser.add_argument('--port', type=int, required=True)
parser.add_argument('--username', required=True)
parser.add_argument('--password', required=True)
parser.add_argument('--secret_path', default='')

args = parser.parse_args()

try:
    url = args.url
    if not url.startswith('http'):
        url = f'https://{url}'
    api_url = f"{url}:{args.port}"
    if args.secret_path:
        api_url += f"/{args.secret_path.strip('/')}"
    client = Api(api_url, args.username, args.password, use_tls_verify=False)
    client.login()
    status = client.server.get_status()
    if status:
        print(1)
        sys.exit(0)
    else:
        print(0)
        sys.exit(1)
except Exception as e:
    print(0)
    sys.exit(1) 