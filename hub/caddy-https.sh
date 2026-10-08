#!/bin/bash
# Optional: eigene Domain statt Tailscale (Portfreigabe 443 im Router nötig).
# Aufruf auf dem Hub:  sudo bash caddy-https.sh home.example.com
# Danach auf der Einrichtungsseite unter „Fernzugriff → Eigene Adresse" https://home.example.com eintragen.
set -u
DOMAIN="${1:-}"
[ -n "$DOMAIN" ] || { echo "Aufruf: sudo bash caddy-https.sh <domain>"; exit 1; }
[ "$(id -u)" = 0 ] || { echo "Bitte mit sudo starten."; exit 1; }

echo "== Belegt schon etwas Port 80/443?"
ss -ltnp | grep -E ':(80|443)\s' || echo "frei"

echo "== Caddy installieren"
apt-get update -qq && apt-get install -y -qq caddy

cat > /etc/caddy/Caddyfile <<CF
$DOMAIN {
    encode gzip
    reverse_proxy 127.0.0.1:8080 {
        flush_interval -1
    }
}
CF

systemctl enable caddy >/dev/null 2>&1
systemctl restart caddy
sleep 20
echo "== Zertifikat / Status"
journalctl -u caddy --since "2 min ago" --no-pager | grep -iE "certificate obtained|error|challenge" | tail -8
echo "== Test (von außen mit dem Handy ohne WLAN prüfen): https://$DOMAIN/api/health"
