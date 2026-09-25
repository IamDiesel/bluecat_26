#!/usr/bin/env bash
# Bluecat/TriLola – Installation auf einem Raspberry Pi.
# Wird von deploy/bluecat_deploy.py hochgeladen und per "ssh -t" gestartet.
# Idempotent: kann beliebig oft ausgeführt werden.
#
#   ~/bluecat/venv      gemeinsame Python-Umgebung
#   ~/bluecat/sensor    BLE-Sensor (bluecat-sensor.service)
#   ~/bluecat/tracker   TriLola-Tracker (trilola.service), config/ bleibt erhalten
#
# Rückgängig: bash ~/bluecat/.incoming/pi_install.sh rollback
set -euo pipefail

BASE="$HOME/bluecat"
IN="$BASE/.incoming"
UNIT_DIR="${BLUECAT_UNIT_DIR:-/etc/systemd/system}"
STAMP="$(date +%Y%m%d-%H%M%S)"
# shellcheck disable=SC1091
source "$IN/deploy.env"

log() { echo -e "\033[1;36m[bluecat]\033[0m $*"; }
warn() { echo -e "\033[1;33m[bluecat] WARNUNG:\033[0m $*"; }

# sudo: im Terminal einmal fragen; aus der GUI kommt das Passwort als erste Zeile über stdin
setup_sudo() {
    if [ "${BLUECAT_SUDO_FROM_STDIN:-}" = "1" ]; then
        local pw=""
        IFS= read -r pw || true
        if [ -n "$pw" ]; then
            SUDO_DIR="$(mktemp -d)"
            chmod 700 "$SUDO_DIR"
            printf '%s\n' "$pw" > "$SUDO_DIR/pw"
            chmod 600 "$SUDO_DIR/pw"
            printf '#!/bin/sh\ncat "%s/pw"\n' "$SUDO_DIR" > "$SUDO_DIR/askpass"
            chmod 700 "$SUDO_DIR/askpass"
            export SUDO_ASKPASS="$SUDO_DIR/askpass"
            trap 'rm -rf "$SUDO_DIR"' EXIT
            sudo() { command sudo -A "$@"; }
        fi
    fi
    if command sudo -n true 2>/dev/null; then
        return 0
    fi
    if [ -n "${SUDO_ASKPASS:-}" ]; then
        command sudo -A -v 2>/dev/null || { echo "[bluecat] FEHLER: sudo-Passwort falsch" >&2; exit 3; }
    elif [ -t 0 ]; then
        command sudo -v
    else
        echo "[bluecat] FEHLER: sudo verlangt ein Passwort – bitte beim Installieren angeben" >&2
        exit 3
    fi
}

rollback() {
    log "Rollback: neue Dienste stoppen, alte wieder aktivieren"
    sudo systemctl disable --now bluecat-sensor.service 2>/dev/null || true
    if [ -f "$BASE/.rollback" ]; then
        while read -r kind value; do
            case "$kind" in
                unit) sudo systemctl enable --now "$value" || true ;;
                restore) sudo cp "$value" "${value%.bak-*}" && log "wiederhergestellt: ${value%.bak-*}" ;;
            esac
        done < "$BASE/.rollback"
    fi
    sudo systemctl daemon-reload
    sudo systemctl restart trilola.service 2>/dev/null || true
    log "Rollback abgeschlossen."
}

setup_sudo

if [ "${1:-}" = "rollback" ]; then
    rollback
    exit 0
fi

log "Knoten $NODE_ID – Sensor=${ROLE_SENSOR} Tracker=${ROLE_TRACKER}"

# ---------------------------------------------------------------------------
# 1. Python-Umgebung
# ---------------------------------------------------------------------------
if ! python3 -c "import venv, ensurepip" 2>/dev/null; then
    log "Installiere python3-venv ..."
    sudo apt-get update -qq && sudo apt-get install -y -qq python3-venv
fi
if [ ! -x "$BASE/venv/bin/python" ]; then
    log "Lege Python-Umgebung an ($BASE/venv) ..."
    python3 -m venv --system-site-packages "$BASE/venv"
fi
REQ=("$IN/sensor/requirements.txt")
if [ "$ROLE_TRACKER" = "1" ]; then REQ+=("$IN/tracker/requirements-tracker.txt"); fi
if [ "$ROLE_SENSOR" = "1" ] || [ "$ROLE_TRACKER" = "1" ]; then
    log "Installiere Python-Pakete (nur fertige Wheels) ..."
    for r in "${REQ[@]}"; do
        "$BASE/venv/bin/pip" install -q --disable-pip-version-check --prefer-binary -r "$r"
    done
fi

# ---------------------------------------------------------------------------
# 2. Dateien (Konfiguration und Aufzeichnungen bleiben erhalten)
# ---------------------------------------------------------------------------
if [ "$ROLE_SENSOR" = "1" ]; then
    mkdir -p "$BASE/sensor"
    cp -f "$IN/sensor/"* "$BASE/sensor/"
fi

old_python() {  # Interpreter der alten trilola-Installation (aus der gesicherten Dienstdatei)
    local f line
    for f in "$UNIT_DIR"/trilola.service.bak-* "$UNIT_DIR/trilola.service"; do
        [ -f "$f" ] || continue
        line="$(sudo grep -m1 '^ExecStart=' "$f" 2>/dev/null || true)"
        line="${line#ExecStart=}"
        line="${line#[-@+!]}"
        case "$line" in *"$BASE/"*|"") continue ;; esac
        set -- $line
        if [ -x "${1:-}" ]; then
            echo "$1"
            return
        fi
    done
    echo python3
}

read_old_origin() {  # Verzeichnis, Interpreter – gibt ORIGIN_LAT/ORIGIN_LON-Zeilen aus
    cd "$1" && "$2" - 2>/dev/null <<'PY'
import glob, os, re, runpy, sys
def out(lat, lon):
    print("ORIGIN_LAT = %r\nORIGIN_LON = %r" % (float(lat), float(lon)))
    sys.exit(0)
# 1. so importieren wie der alte Tracker ("from secrets.tri import ...")
sys.path.insert(0, os.getcwd())
sys.modules.pop("secrets", None)
for mod in ("secrets.tri", "secrets_tri"):
    try:
        m = __import__(mod, fromlist=["ORIGIN_LAT"])
        out(m.ORIGIN_LAT, m.ORIGIN_LON)
    except SystemExit:
        raise
    except Exception:
        pass
# 2. Dateien direkt ausführen, 3. notfalls nach den Zuweisungen suchen
files = [f for pat in ("secrets/tri.py", "secrets/tri/__init__.py", "secrets_tri.py", "secrets/*.py", "secrets/**/*.py")
         for f in glob.glob(pat, recursive=True)]
for f in dict.fromkeys(files):
    try:
        d = runpy.run_path(f)
        out(d["ORIGIN_LAT"], d["ORIGIN_LON"])
    except SystemExit:
        raise
    except Exception:
        pass
found = {}
for f in dict.fromkeys(files):
    try:
        text = open(f, encoding="utf-8", errors="replace").read()
    except OSError:
        continue
    for key in ("ORIGIN_LAT", "ORIGIN_LON"):
        m = re.search(r"^\s*" + key + r"\s*=\s*\(?\s*([-+]?[0-9]+(?:\.[0-9]+)?)", text, re.M)
        if m and key not in found:
            found[key] = m.group(1)
if len(found) == 2:
    out(found["ORIGIN_LAT"], found["ORIGIN_LON"])
PY
}

find_old_tracker_dir() {
    local line py
    line="$(systemctl show -p ExecStart --value trilola.service 2>/dev/null || true)"
    py="$(echo "$line" | grep -o '[^ ;]*trilola_tracker\.py' | head -n1 || true)"
    if [ -n "$py" ] && [ "$(dirname "$py")" != "$BASE/tracker" ] && [ -f "$py" ]; then
        dirname "$py"; return
    fi
    py="$(find "$HOME" -maxdepth 5 -name trilola_tracker.py -not -path "$BASE/*" 2>/dev/null | head -n1 || true)"
    if [ -n "$py" ]; then dirname "$py"; fi
}

if [ "$ROLE_TRACKER" = "1" ]; then
    mkdir -p "$BASE/tracker/config"
    OLD_DIR="$(find_old_tracker_dir)"
    cp -rf "$IN/tracker/." "$BASE/tracker/"
    if ! ls "$BASE/tracker/config/"*.json >/dev/null 2>&1; then
        if [ -n "$OLD_DIR" ] && [ -d "$OLD_DIR/config" ]; then
            log "Übernehme Tracker-Konfiguration aus $OLD_DIR/config"
            cp -n "$OLD_DIR/config/"*.json "$BASE/tracker/config/" 2>/dev/null || true
        else
            warn "Keine bestehende Tracker-Konfiguration gefunden – Sensoren melden sich per Identity neu an."
        fi
    fi
    # Koordinatenursprung aus der alten Installation übernehmen, falls nicht in fleet.toml
    if ! grep -q '^ORIGIN_LAT' "$BASE/tracker/secrets_tri.py"; then
        ORIGIN=""
        if [ -n "$OLD_DIR" ] && [ -d "$OLD_DIR" ]; then
            # 1. genau so importieren wie der alte Tracker (alter Interpreter, altes Verzeichnis)
            ORIGIN="$(read_old_origin "$OLD_DIR" "$(old_python)" || true)"
            if [ -n "$ORIGIN" ]; then
                log "Übernehme ORIGIN_LAT/LON aus der alten Installation ($OLD_DIR)"
                printf '\n# aus alter Installation übernommen\n%s\n' "$ORIGIN" >> "$BASE/tracker/secrets_tri.py"
                printf '%s\n' "$ORIGIN" > "$BASE/tracker/.origin_backup.py"
            fi
        fi
        [ -n "$ORIGIN" ] || for candidate in "$OLD_DIR/secrets/tri.py" "$OLD_DIR/secrets_tri.py" "$BASE/tracker/.origin_backup.py"; do
            if [ -n "$candidate" ] && [ -f "$candidate" ]; then
                ORIGIN="$(python3 - "$candidate" <<'PY'
import runpy, sys
try:
    d = runpy.run_path(sys.argv[1])
    print(f"ORIGIN_LAT = {float(d['ORIGIN_LAT'])!r}\nORIGIN_LON = {float(d['ORIGIN_LON'])!r}")
except Exception:
    pass
PY
)"
                if [ -n "$ORIGIN" ]; then
                    log "Übernehme ORIGIN_LAT/LON aus $candidate"
                    printf '\n# aus alter Installation übernommen\n%s\n' "$ORIGIN" >> "$BASE/tracker/secrets_tri.py"
                    printf '%s\n' "$ORIGIN" > "$BASE/tracker/.origin_backup.py"
                    break
                fi
            fi
        done
        [ -n "$ORIGIN" ] || warn "ORIGIN_LAT/LON unbekannt – bitte in deploy/fleet.toml unter [tracker] eintragen."
    fi
fi

# ---------------------------------------------------------------------------
# 3. Bluetooth Experimental Mode (-E) per systemd-Drop-in
# ---------------------------------------------------------------------------
if [ "$ROLE_SENSOR" = "1" ] && [ "${BLUETOOTH_EXPERIMENTAL:-1}" = "1" ]; then
    BT_LINE="$(systemctl show -p ExecStart --value bluetooth.service 2>/dev/null || true)"
    BT_ARGV="$(echo "$BT_LINE" | sed -n 's/.*argv\[\]=\([^;]*\);.*/\1/p' | sed 's/[[:space:]]*$//')"
    if [ -n "$BT_ARGV" ] && ! echo " $BT_ARGV " | grep -Eq ' (-E|--experimental) '; then
        log "Aktiviere BlueZ Experimental Mode (-E)"
        sudo mkdir -p "$UNIT_DIR/bluetooth.service.d"
        printf '[Service]\nExecStart=\nExecStart=%s -E\n' "$BT_ARGV" | sudo tee "$UNIT_DIR/bluetooth.service.d/bluecat-experimental.conf" >/dev/null
        sudo chmod 644 "$UNIT_DIR/bluetooth.service.d/bluecat-experimental.conf"
        sudo systemctl daemon-reload
        sudo systemctl restart bluetooth.service
    fi
fi

# ---------------------------------------------------------------------------
# 4. systemd-Dienste
# ---------------------------------------------------------------------------
: > "$BASE/.rollback.new"
write_unit() {  # name, beschreibung, arbeitsverzeichnis, skript
    local name="$1" desc="$2" dir="$3" script="$4" tmp
    tmp="$(mktemp)"
    cat > "$tmp" <<UNIT
# Verwaltet von deploy/bluecat_deploy.py – Änderungen werden überschrieben.
[Unit]
Description=$desc
After=network-online.target bluetooth.service
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$dir
ExecStart=$BASE/venv/bin/python -u $script
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
    if [ -f "$UNIT_DIR/$name" ] && ! sudo grep -q 'bluecat_deploy.py' "$UNIT_DIR/$name"; then
        sudo cp "$UNIT_DIR/$name" "$UNIT_DIR/$name.bak-$STAMP"
        echo "restore $UNIT_DIR/$name.bak-$STAMP" >> "$BASE/.rollback.new"
        log "Alte $name gesichert ($name.bak-$STAMP)"
    fi
    sudo install -m 644 "$tmp" "$UNIT_DIR/$name"
    rm -f "$tmp"
}

# Alte Sensor-Dienste (andere Namen/Pfade) ablösen
for unit_file in "$UNIT_DIR"/*.service; do
    [ -f "$unit_file" ] || continue
    unit="$(basename "$unit_file")"
    case "$unit" in bluecat-sensor.service|trilola.service) continue ;; esac
    if sudo grep -q 'bluecat2mqtt.py' "$unit_file" 2>/dev/null; then
        if systemctl is-enabled --quiet "$unit" 2>/dev/null || systemctl is-active --quiet "$unit" 2>/dev/null; then
            log "Deaktiviere alten Dienst $unit"
            sudo systemctl disable --now "$unit" || true
            echo "unit $unit" >> "$BASE/.rollback.new"
        fi
    fi
done

if [ "$ROLE_SENSOR" = "1" ]; then
    write_unit bluecat-sensor.service "Bluecat BLE-Sensor ($NODE_ID)" "$BASE/sensor" bluecat2mqtt.py
fi
if [ "$ROLE_TRACKER" = "1" ]; then
    write_unit trilola.service "TriLola Tracker" "$BASE/tracker" trilola_tracker.py
fi
# Rollback-Informationen nur beim ersten Ablösen merken
if [ -s "$BASE/.rollback.new" ]; then cat "$BASE/.rollback.new" >> "$BASE/.rollback"; fi
rm -f "$BASE/.rollback.new"

sudo systemctl daemon-reload
if [ "$ROLE_SENSOR" = "1" ]; then
    sudo systemctl enable bluecat-sensor.service >/dev/null 2>&1 || true
    sudo systemctl restart bluecat-sensor.service
else
    sudo systemctl disable --now bluecat-sensor.service 2>/dev/null || true
fi
if [ "$ROLE_TRACKER" = "1" ]; then
    sudo systemctl enable trilola.service >/dev/null 2>&1 || true
    sudo systemctl restart trilola.service
elif [ -f "$UNIT_DIR/trilola.service" ] && { systemctl is-enabled --quiet trilola.service 2>/dev/null || systemctl is-active --quiet trilola.service 2>/dev/null; }; then
    # Tracker läuft jetzt woanders (anderer Pi oder Home-Assistant-App) – hier anhalten, Konfiguration bleibt
    log "Tracker ist umgezogen – trilola.service wird angehalten (Konfiguration bleibt in $BASE/tracker/config)"
    sudo systemctl disable --now trilola.service 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 5. Kontrolle
# ---------------------------------------------------------------------------
sleep "${VERIFY_WAIT_SEC:-6}"
STATUS=0
for unit in $( [ "$ROLE_SENSOR" = "1" ] && echo bluecat-sensor.service ) $( [ "$ROLE_TRACKER" = "1" ] && echo trilola.service ); do
    if systemctl is-active --quiet "$unit"; then
        log "$unit läuft ✔"
    else
        warn "$unit läuft NICHT"
        STATUS=1
    fi
    sudo journalctl -u "$unit" -n 8 --no-pager 2>/dev/null | sed 's/^/    /' || true
done
exit $STATUS
