#!/usr/bin/env bash
# Liesenberg Home Hub – Installation auf einem Raspberry Pi (Raspberry Pi OS / Debian)
#
#   curl -fsSL https://raw.githubusercontent.com/michaelliesenberg/LiesenbergHome/main/hub/install.sh | sudo bash
#
# Auch zum Aktualisieren: einfach erneut ausführen. Einstellungen bleiben erhalten.
set -eu
REPO="${HUB_REPO:-https://github.com/michaelliesenberg/LiesenbergHome}"
BRANCH="${HUB_BRANCH:-main}"
DIR=/opt/liesenberg-home
SERVICE=liesenberg-home

[ "$(id -u)" = 0 ] || { echo "Bitte mit sudo starten."; exit 1; }
say() { printf '\n\033[1;33m▸ %s\033[0m\n' "$*"; }

# ---- Dienst-Benutzer: bestehende Installation behalten, sonst eigener Benutzer „homehub"
if [ -d "$DIR" ] && [ "$(stat -c %U "$DIR")" != root ]; then
  HUB_USER=$(stat -c %U "$DIR")
else
  HUB_USER=homehub
  id "$HUB_USER" >/dev/null 2>&1 || useradd --system --home "$DIR" --shell /usr/sbin/nologin "$HUB_USER"
fi
say "Hub-Benutzer: $HUB_USER"

say "Pakete installieren"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl avahi-daemon >/dev/null

say "Hub-Dateien holen"
SRC=""
HERE=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")
if [ -n "$HERE" ] && [ -f "$HERE/house_server.py" ]; then
  SRC="$HERE"                                   # lokal (z. B. aus einem ausgecheckten Repo)
else
  TMP=$(mktemp -d)
  curl -fsSL "$REPO/archive/refs/heads/$BRANCH.tar.gz" | tar xz -C "$TMP"
  SRC=$(find "$TMP" -maxdepth 2 -type d -name hub | head -1)
fi
[ -f "$SRC/house_server.py" ] || { echo "Hub-Dateien nicht gefunden"; exit 1; }
mkdir -p "$DIR"
cp "$SRC"/*.py "$SRC"/VERSION "$SRC"/requirements.txt "$DIR"/
install -m 755 -o root -g root "$SRC/hubctl" /usr/local/sbin/hubctl
chown -R "$HUB_USER" "$DIR"
# alte Zugangsdaten-Datei für die einmalige Übernahme lesbar machen
[ -f /etc/liesenberg-home.env ] && chgrp "$HUB_USER" /etc/liesenberg-home.env && chmod 640 /etc/liesenberg-home.env

say "Python-Umgebung"
[ -d "$DIR/venv" ] || sudo -u "$HUB_USER" python3 -m venv "$DIR/venv"
sudo -u "$HUB_USER" "$DIR/venv/bin/pip" install -q --upgrade pip >/dev/null
sudo -u "$HUB_USER" "$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt"

say "Dienst einrichten"
sed "s/__USER__/$HUB_USER/" "$SRC/liesenberg-home.service" > /etc/systemd/system/$SERVICE.service
echo "$HUB_USER ALL=(root) NOPASSWD: /usr/local/sbin/hubctl" > /etc/sudoers.d/liesenberg-hub
chmod 440 /etc/sudoers.d/liesenberg-hub
visudo -cf /etc/sudoers.d/liesenberg-hub >/dev/null

# Im Heimnetz automatisch auffindbar (Bonjour) – die App findet den Hub ohne Adresse
cat > /etc/avahi/services/liesenberg-home.service <<AV
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group><name replace-wildcards="yes">Liesenberg Home Hub (%h)</name>
<service><type>_liesenberghome._tcp</type><port>8080</port><txt-record>path=/setup</txt-record></service>
</service-group>
AV
systemctl reload avahi-daemon 2>/dev/null || systemctl restart avahi-daemon || true

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"

IP=$(hostname -I | awk '{print $1}')
for _ in $(seq 1 30); do curl -fs "http://127.0.0.1:8080/api/health" >/dev/null && break; sleep 1; done
if curl -fs "http://127.0.0.1:8080/api/health" >/dev/null; then
  say "Fertig! Version $(cat $DIR/VERSION)"
  echo "  Einrichtung im Browser (im selben WLAN):"
  echo "    http://$IP:8080/setup"
  echo "    http://$(hostname).local:8080/setup"
else
  echo "Der Hub startet nicht – Protokoll:  journalctl -u $SERVICE -n 50"
  exit 1
fi
