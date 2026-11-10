#!/bin/sh
# Install or update model-agent. Usage: sudo ./install.sh <bind-ip> [engines-dir]
set -eu
BIND="${1:?usage: install.sh <bind-ip> [engines-dir]}"
ENGINES="${2:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"

install -d -m 755 /usr/local/lib/model-agent
install -m 755 "$HERE/model-agent.py" /usr/local/lib/model-agent/model-agent.py
install -d -m 700 /etc/model-agent /etc/model-agent/engines.d
if [ ! -s /etc/model-agent/token ]; then
  (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > /etc/model-agent/token)
fi
if [ -n "$ENGINES" ]; then
  cp -n "$ENGINES"/*.json /etc/model-agent/engines.d/
fi
sed "s/@BIND@/$BIND/" "$HERE/model-agent.service" > /etc/systemd/system/model-agent.service
systemctl daemon-reload
systemctl enable model-agent.service
systemctl restart model-agent.service
echo "model-agent listens on $BIND:9872. Token: /etc/model-agent/token"
