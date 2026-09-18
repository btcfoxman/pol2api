#!/usr/bin/env bash
set -euo pipefail

: "${CLOUDFLARED_CONFIG:=/etc/cloudflared/config.yml}"
: "${POL2API_HOSTNAME:=pol2api.aiid.edu.kg}"
: "${POL2API_ORIGIN:=http://127.0.0.1:8799}"
export POL2API_HOSTNAME POL2API_ORIGIN
exec 9>/tmp/ai-gateway-cloudflared.lock
flock -x 9
sudo test -f "$CLOUDFLARED_CONFIG"
temp_config="$(mktemp)"
trap 'rm -f "$temp_config"' EXIT
sudo cat "$CLOUDFLARED_CONFIG" > "$temp_config"

CLOUDFLARED_TMP="$temp_config" python3 - <<'PY'
import os
from pathlib import Path

path = Path(os.environ["CLOUDFLARED_TMP"])
hostname = os.environ["POL2API_HOSTNAME"]
origin = os.environ["POL2API_ORIGIN"]
lines = path.read_text(encoding="utf-8").splitlines()
if not any(line.strip() == "ingress:" for line in lines):
    raise SystemExit("Cloudflared config has no ingress section")
route = f"- hostname: {hostname}"
index = next((i for i, line in enumerate(lines) if line.strip() == route), None)
if index is None:
    fallback = next((i for i, line in enumerate(lines) if line.strip() == "- service: http_status:404"), None)
    if fallback is None:
        raise SystemExit("Expected a final http_status:404 rule")
    lines[fallback:fallback] = [f"  - hostname: {hostname}", f"    service: {origin}"]
else:
    for pos in range(index + 1, len(lines)):
        if lines[pos].strip().startswith("- "):
            raise SystemExit("Existing ingress has no service")
        if lines[pos].strip().startswith("service:"):
            indent = lines[pos][:len(lines[pos]) - len(lines[pos].lstrip())]
            lines[pos] = f"{indent}service: {origin}"
            break
    else:
        raise SystemExit("Existing ingress has no service")
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

sudo cloudflared tunnel --config "$temp_config" ingress validate
if ! cmp -s "$temp_config" <(sudo cat "$CLOUDFLARED_CONFIG"); then
  sudo cp "$CLOUDFLARED_CONFIG" "${CLOUDFLARED_CONFIG}.bak.$(date +%Y%m%d%H%M%S)"
  sudo install -m 600 -o root -g root "$temp_config" "$CLOUDFLARED_CONFIG"
  sudo systemctl restart cloudflared
fi
tunnel_id="$(sudo awk '/^tunnel:/ {print $2; exit}' "$CLOUDFLARED_CONFIG")"
test -n "$tunnel_id"
if [ -f "$HOME/.cloudflared/cert.pem" ]; then
  cloudflared --origincert "$HOME/.cloudflared/cert.pem" tunnel route dns "$tunnel_id" "$POL2API_HOSTNAME"
else
  sudo cloudflared tunnel route dns "$tunnel_id" "$POL2API_HOSTNAME"
fi
