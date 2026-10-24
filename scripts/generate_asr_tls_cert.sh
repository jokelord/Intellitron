#!/bin/bash
# Generate a local CA and server certificate for Android/Desktop microphone HTTPS.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
YAML_OVERALL="$PROJECT_ROOT/config/config-overall.yaml"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

eval "$(python3 - "$PROJECT_ROOT" "$YAML_OVERALL" <<'PYEOF'
import ipaddress
import shlex
import sys
from pathlib import Path
import yaml

root = Path(sys.argv[1])
overall = yaml.safe_load(Path(sys.argv[2]).read_text(encoding="utf-8")) or {}
asr_path = root / overall["asr"]
asr = yaml.safe_load(asr_path.read_text(encoding="utf-8")) or {}
security = asr["browser_security"]

def resolve(value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path

sans = []
for index, value in enumerate(security["subject_alt_names"], 1):
    try:
        ipaddress.ip_address(value)
        kind = "IP"
    except ValueError:
        kind = "DNS"
    sans.append(f"{kind}.{index} = {value}")

values = {
    "CA_CERT": resolve(security["ca_certificate_path"]),
    "SERVER_CERT": resolve(security["certificate_path"]),
    "SERVER_KEY": resolve(security["private_key_path"]),
    "SAN_LINES": "\\n".join(sans),
}
for key, value in values.items():
    print(f"{key}={shlex.quote(str(value))}")
PYEOF
)"

CA_KEY="${CA_CERT%.crt}.key"
if [ "$FORCE" -ne 1 ] && { [ -e "$CA_CERT" ] || [ -e "$SERVER_CERT" ] || [ -e "$SERVER_KEY" ]; }; then
    echo "TLS files already exist; use --force to replace them."
    exit 1
fi
mkdir -p "$(dirname "$CA_CERT")" "$(dirname "$SERVER_CERT")" "$(dirname "$SERVER_KEY")"
TMP_CONFIG="$(mktemp)"
trap 'rm -f "$TMP_CONFIG"' EXIT
cat > "$TMP_CONFIG" <<EOF
[req]
distinguished_name = dn
req_extensions = req_ext
prompt = no
[dn]
CN = LLM Deployment ASR
[req_ext]
subjectAltName = @alt_names
[alt_names]
$(printf '%b' "$SAN_LINES")
EOF

openssl genrsa -out "$CA_KEY" 4096
openssl req -x509 -new -nodes -key "$CA_KEY" -sha256 -days 3650 \
    -subj "/CN=LLM Deployment Local CA" -out "$CA_CERT"
openssl genrsa -out "$SERVER_KEY" 2048
openssl req -new -key "$SERVER_KEY" -out "${SERVER_CERT}.csr" -config "$TMP_CONFIG"
openssl x509 -req -in "${SERVER_CERT}.csr" -CA "$CA_CERT" -CAkey "$CA_KEY" \
    -CAcreateserial -out "$SERVER_CERT" -days 825 -sha256 \
    -extensions req_ext -extfile "$TMP_CONFIG"
rm -f "${SERVER_CERT}.csr"
chmod 600 "$CA_KEY" "$SERVER_KEY"
echo "ASR TLS certificate generated. Install this CA on Android: $CA_CERT"
