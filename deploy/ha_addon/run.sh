#!/usr/bin/env bash
# Start der TriLola-App: Oberfläche (Ingress) und – falls so eingestellt – der Tracker.
set -euo pipefail

export BLUECAT_ADDON=1
export BLUECAT_DATA_DIR=/config            # fleet.toml, Grundriss, Tracker-Konfiguration (in HA-Backups)
export BLUECAT_CACHE_DIR=/data/tile_cache  # Kartenkacheln (nicht gesichert)
export HOME=/data                          # SSH-Schlüssel/known_hosts, PlatformIO
export PLATFORMIO_CORE_DIR=/data/platformio
export PLATFORMIO_WORKSPACE_DIR=/data/pio_workspace

mkdir -p /config /data/.ssh /data/tile_cache
chmod 700 /data/.ssh

# Schlüssel, den der Installer für die Pis angelegt hat
if [ -f /config/ssh/id_ed25519 ] && ! cmp -s /config/ssh/id_ed25519 /data/.ssh/id_ed25519; then
    cp /config/ssh/id_ed25519 /data/.ssh/id_ed25519
    [ -f /config/ssh/id_ed25519.pub ] && cp /config/ssh/id_ed25519.pub /data/.ssh/id_ed25519.pub
    chmod 600 /data/.ssh/id_ed25519
fi

# OpenSSH nimmt das Home aus /etc/passwd (/root) – dorthin verlinken (deploy gibt -i zusätzlich an)
if [ ! -e /root/.ssh ]; then ln -s /data/.ssh /root/.ssh; fi

cd /opt/trilola
exec python3 deploy/bluecat_gui.py --fleet /config/fleet.toml --ingress --port 8765 --no-browser
