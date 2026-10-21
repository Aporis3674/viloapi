#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
# Usage: sudo bash install_node.sh --host vpn.example.com --email admin@example.com [--ip PUBLIC_IP] [--country Germany]
# ponytail: dedicated Ubuntu/Debian VPS only; adopt existing installations through an explicit migration, never overwrite them.
HOST='' EMAIL='' IP='' COUNTRY='Unknown' NAME='Vilo' SNI='dl.google.com' PORT=8443
REF="${VILO_REF:-main}"
while (($#)); do
  case "$1" in
    --host) HOST="${2:?}";; --email) EMAIL="${2:?}";; --ip) IP="${2:?}";;
    --country) COUNTRY="${2:?}";; --name) NAME="${2:?}";; --sni) SNI="${2:?}";;
    *) printf 'Unknown argument: %s\n' "$1" >&2; exit 1;;
  esac
  shift 2
done
[[ $EUID == 0 ]] || { printf 'Run as root.\n' >&2; exit 1; }
[[ -r /etc/os-release ]] || exit 1
. /etc/os-release
[[ "$ID" == ubuntu || "$ID" == debian ]] || { printf 'Ubuntu/Debian required.\n' >&2; exit 1; }
[[ "$HOST" =~ ^([a-z0-9][a-z0-9-]*\.)+[a-z]{2,63}$ && "$EMAIL" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]] || { printf 'Provide --host and --email. DNS must point to this VPS.\n' >&2; exit 1; }
[[ "$SNI" =~ ^([a-zA-Z0-9-]+\.)+[a-zA-Z]{2,63}$ && "$REF" =~ ^[a-zA-Z0-9._-]+$ ]] || exit 1
if [[ -f /etc/vilo-node/node.json && -f /etc/vilo-node/installed ]]; then
  systemctl is-active --quiet vilo-xray vilo-node nginx || { printf 'Installation exists but services are unhealthy; inspect journalctl before repair.\n' >&2; exit 1; }
  printf 'Already installed. Existing configuration and keys preserved.\n'
  python3 -I -c 'import json,base64; d=json.load(open("/etc/vilo-node/node.json")); print("vilo1."+base64.b64encode(json.dumps(d,ensure_ascii=True).encode()).decode())'
  exit 0
fi
for target in /etc/vilo-node /usr/local/bin/xray /etc/xray /usr/local/etc/xray /etc/nginx/sites-enabled/default /etc/systemd/system/vilo-node.service; do
  [[ ! -e "$target" ]] || { printf 'Existing installation detected: %s. Refusing overwrite.\n' "$target" >&2; exit 1; }
done
command -v systemctl >/dev/null || { printf 'systemd required.\n' >&2; exit 1; }
for p in 80 443 8443 10085 10086 10087; do
  if command -v ss >/dev/null && [[ -n "$(ss -H -ltn "sport = :$p")" ]]; then
    printf 'Port %s already in use.\n' "$p" >&2; exit 1
  fi
done
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl python3 unzip nginx certbot openssl iproute2
# nginx's package default is the only site removed; pre-existing installations were refused above.
rm -f /etc/nginx/sites-enabled/default
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
export VILO_HOST="$HOST" VILO_EMAIL="$EMAIL" VILO_IP="$IP" VILO_COUNTRY="$COUNTRY" VILO_NAME="$NAME" VILO_SNI="$SNI"
python3 -I - "$WORK" <<'PY'
import hashlib, ipaddress, json, os, pathlib, platform, re, socket, sys, urllib.request, zipfile
work = pathlib.Path(sys.argv[1])
def get(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Vilo-Installer/1'})
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read()
ip = os.environ['VILO_IP'] or get('https://api.ipify.org').decode().strip()
address = ipaddress.ip_address(ip)
if address.version != 4 or not address.is_global: raise SystemExit('Public IPv4 required')
host = os.environ['VILO_HOST']
resolved = {item[4][0] for item in socket.getaddrinfo(host, None, socket.AF_INET)}
if resolved != {ip}: raise SystemExit('DNS A record must point directly to this VPS (no CDN proxy)')
if len(os.environ['VILO_NAME']) > 64 or len(os.environ['VILO_COUNTRY']) > 64: raise SystemExit('Name/country too long')
arch = {'x86_64': '64', 'aarch64': 'arm64-v8a'}.get(platform.machine())
if not arch: raise SystemExit('Supported architectures: amd64, arm64')
release = json.loads(get('https://api.github.com/repos/XTLS/Xray-core/releases/latest'))
asset_name = f'Xray-linux-{arch}.zip'
asset = next(a for a in release['assets'] if a['name'] == asset_name)
url = asset['browser_download_url']
if not url.startswith('https://github.com/XTLS/Xray-core/releases/download/'): raise SystemExit('Unexpected release URL')
archive = get(url)
digest = asset.get('digest') or ''
if digest.startswith('sha256:'):
    expected = digest.split(':', 1)[1]
else:
    digest_text = get(url + '.dgst').decode()
    values = re.findall(r'(?i)(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])', digest_text)
    if len(set(values)) != 1: raise SystemExit('Cannot verify release SHA256')
    expected = values[0]
if hashlib.sha256(archive).hexdigest().lower() != expected.lower(): raise SystemExit('Release checksum mismatch')
archive_path = work / 'xray.zip'
archive_path.write_bytes(archive)
# Extract only known files, never archive paths or executable helper scripts.
with zipfile.ZipFile(archive_path) as z:
    for name in ['xray', 'geoip.dat', 'geosite.dat']:
        (work / name).write_bytes(z.read(name))
(work / 'ip').write_text(ip)
(work / 'version').write_text(release['tag_name'])
PY
IP="$(<"$WORK/ip")"
export VILO_IP="$IP"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/node_agent.py" ]]; then
  install -m 600 "$SCRIPT_DIR/node_agent.py" "$WORK/node_agent.py"
else
  curl --fail --show-error --silent --location --proto '=https' --proto-redir '=https' \
    "https://raw.githubusercontent.com/Aporis3674/viloapi/$REF/scripts/node_agent.py" -o "$WORK/node_agent.py"
fi
# Compile, never execute downloaded Python during validation.
python3 -I -c 'import ast,sys; ast.parse(open(sys.argv[1],encoding="utf-8").read())' "$WORK/node_agent.py"
install -d -m 700 /etc/vilo-node
install -d -m 755 /opt/vilo-node /usr/local/share/xray /var/www/vilo-acme/.well-known/acme-challenge
install -m 755 "$WORK/xray" /usr/local/bin/xray
install -m 644 "$WORK/geoip.dat" "$WORK/geosite.dat" /usr/local/share/xray/
install -m 644 "$WORK/node_agent.py" /opt/vilo-node/node_agent.py
/usr/local/bin/xray x25519 > "$WORK/keys"
export VILO_KEYS="$WORK/keys"
python3 -I - <<'PY'
import json, os, pathlib, re, secrets
root = pathlib.Path('/etc/vilo-node')
keys = pathlib.Path(os.environ['VILO_KEYS']).read_text()
private = re.search(r'(?:PrivateKey|Private key):\s*(\S+)', keys)
public = re.search(r'(?:Password|PublicKey|Public key):\s*(\S+)', keys)
if not private or not public: raise SystemExit('Unrecognized x25519 output; no guessed key fields')
sid = secrets.token_hex(8)
cfg = {'version': 1, 'name': os.environ['VILO_NAME'], 'host': os.environ['VILO_HOST'], 'ip': os.environ['VILO_IP'],
       'port': 8443, 'country': os.environ['VILO_COUNTRY'], 'syncSecret': secrets.token_hex(32),
       'connection': {'pbk': public[1], 'sid': sid, 'sni': os.environ['VILO_SNI'], 'realityPort': 443, 'wsPort': 80, 'wsPath': '/vilo-ws'}}
config = {'log': {'loglevel': 'warning'}, 'stats': {},
 'api': {'tag': 'api', 'services': ['StatsService']},
 'policy': {'levels': {'0': {'statsUserUplink': True, 'statsUserDownlink': True}}},
 'inbounds': [
  {'tag': 'api-in', 'listen': '127.0.0.1', 'port': 10085, 'protocol': 'dokodemo-door', 'settings': {'address': '127.0.0.1'}},
  {'tag': 'reality', 'listen': '0.0.0.0', 'port': 443, 'protocol': 'vless', 'settings': {'clients': [], 'decryption': 'none'},
   'streamSettings': {'network': 'tcp', 'security': 'reality', 'realitySettings': {'show': False, 'target': os.environ['VILO_SNI'] + ':443',
    'xver': 0, 'serverNames': [os.environ['VILO_SNI']], 'privateKey': private[1], 'shortIds': [sid]}}},
  {'tag': 'ws', 'listen': '127.0.0.1', 'port': 10087, 'protocol': 'vless', 'settings': {'clients': [], 'decryption': 'none'},
   'streamSettings': {'network': 'ws', 'security': 'none', 'wsSettings': {'path': '/vilo-ws'}}}],
 'outbounds': [{'protocol': 'freedom', 'tag': 'direct'}, {'protocol': 'blackhole', 'tag': 'blocked'}],
 'routing': {'rules': [{'type': 'field', 'inboundTag': ['api-in'], 'outboundTag': 'api'},
                       {'type': 'field', 'ip': ['geoip:private'], 'outboundTag': 'blocked'}]}}
for name, value in [('node.json', cfg), ('xray.json', config)]:
    path = root / name
    path.write_text(json.dumps(value))
    path.chmod(0o600)
PY
/usr/local/bin/xray run -test -config /etc/vilo-node/xray.json
# First start HTTP for ACME; Reality retains 443. HTTPS management uses 8443.
cat > /etc/nginx/sites-available/vilo-node <<EOF
server {
    listen 80;
    server_name $HOST;
    access_log off;
    error_log /var/log/nginx/vilo-error.log crit;
    location /.well-known/acme-challenge/ { root /var/www/vilo-acme; }
    location = /vilo-ws {
        proxy_pass http://127.0.0.1:10087;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
    }
    location / { return 404; }
}
EOF
ln -s /etc/nginx/sites-available/vilo-node /etc/nginx/sites-enabled/vilo-node
nginx -t
systemctl restart nginx
certbot certonly --webroot -w /var/www/vilo-acme -d "$HOST" --email "$EMAIL" --agree-tos --non-interactive
cat >> /etc/nginx/sites-available/vilo-node <<EOF
server {
    listen 8443 ssl;
    server_name $HOST;
    ssl_certificate /etc/letsencrypt/live/$HOST/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$HOST/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    access_log off;
    error_log /var/log/nginx/vilo-error.log crit;
    client_max_body_size 64k;
    client_body_timeout 10s;
    location / {
        proxy_pass http://127.0.0.1:10086;
        proxy_connect_timeout 5s;
        proxy_read_timeout 60s;
    }
}
EOF
install -d /etc/letsencrypt/renewal-hooks/deploy
printf '#!/bin/sh\nnginx -t && systemctl reload nginx\n' > /etc/letsencrypt/renewal-hooks/deploy/vilo-nginx
chmod 700 /etc/letsencrypt/renewal-hooks/deploy/vilo-nginx
cat > /etc/systemd/system/vilo-xray.service <<'EOF'
[Unit]
Description=Vilo Xray
After=network-online.target
Wants=network-online.target
[Service]
ExecStart=/usr/local/bin/xray run -config /etc/vilo-node/xray.json
Restart=on-failure
RestartSec=3
Environment=XRAY_LOCATION_ASSET=/usr/local/share/xray
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
LimitNOFILE=65535
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/vilo-node.service <<'EOF'
[Unit]
Description=Vilo node API and traffic monitor
After=network-online.target vilo-xray.service
Wants=vilo-xray.service
[Service]
ExecStart=/usr/bin/python3 -I /opt/vilo-node/node_agent.py
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/etc/vilo-node
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now vilo-xray vilo-node
nginx -t
systemctl reload nginx
systemctl is-active --quiet vilo-xray vilo-node nginx
touch /etc/vilo-node/installed
printf 'Installed Xray %s. Allow inbound TCP 80, 443, 8443 in your provider firewall. Stats/API internals remain loopback-only.\n' "$(<"$WORK/version")"
printf 'Paste this secret token into the bot private admin chat; do not publish it:\n'
python3 -I -c 'import json,base64; d=json.load(open("/etc/vilo-node/node.json")); print("vilo1."+base64.b64encode(json.dumps(d,ensure_ascii=True).encode()).decode())'
