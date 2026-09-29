#!/usr/bin/env python3
"""Bluecat/TriLola – zentrales Rollout aller Knoten aus deploy/fleet.toml.

Läuft auf dem PC (Windows, Linux, macOS), Python ≥ 3.9. Benötigt:
* für Pis:      OpenSSH-Client (ssh/scp; unter Windows 10/11 vorinstalliert)
* für MQTT:     pip install paho-mqtt        (status, provision, ha-cleanup, esp ota)
* für ESP32:    PlatformIO (pio)
* Python < 3.11 zusätzlich: pip install tomli

Befehle (Auswahl, Details mit -h):
    python deploy/bluecat_deploy.py check              fleet.toml prüfen
    python deploy/bluecat_deploy.py ssh-setup all      SSH-Schlüssel auf die Pis (einmalig)
    python deploy/bluecat_deploy.py import-old all     alte Einstellungen von den Pis anzeigen
    python deploy/bluecat_deploy.py pi all             Pis installieren/aktualisieren
    python deploy/bluecat_deploy.py shelly scan        Shellys im Netz suchen
    python deploy/bluecat_deploy.py shelly all         Skript + Konfiguration auf alle Shellys
    python deploy/bluecat_deploy.py esp usb            ESP32 per USB flashen (einmalig)
    python deploy/bluecat_deploy.py esp ota all        ESP32 per WLAN aktualisieren
    python deploy/bluecat_deploy.py status             Übersicht aller Knoten
    python deploy/bluecat_deploy.py ha-cleanup         verwaiste HA-Einträge entfernen
    python deploy/bluecat_deploy.py all                alles in einem Rutsch
    python deploy/bluecat_deploy.py remove <id>        Knoten abmelden und aus fleet.toml löschen

Grafische Oberfläche: deploy/bluecat_gui.py (bzw. rollout.bat / rollout.sh).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import io
import ipaddress
import json
import os
import re
import secrets as pysecrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
# Persönliche Daten (fleet.toml, Cache): neben dem Skript oder – im Home-Assistant-Add-on – im Datenordner
DATA_DIR = os.environ.get("BLUECAT_DATA_DIR") or HERE
DEFAULT_FLEET = os.path.join(DATA_DIR, "fleet.toml")
CACHE_FILE = os.path.join(DATA_DIR, ".cache.json")
IN_ADDON = os.environ.get("BLUECAT_ADDON") == "1"
# [tracker] node = "@addon": der Tracker läuft in der Home-Assistant-App statt auf einem Pi
TRACKER_ADDON = "@addon"
TRACKER_HOME = os.path.join(DATA_DIR, "tracker")      # nur im Add-on: secrets_tri.py + config/
ADDON_SRC_DIR = os.path.join(HERE, "ha_addon")
ADDON_SLUG = "trilola"
ESP_DIR = os.path.join(REPO, "bt_sensor", "esp32_embedded")
PI_SENSOR_DIR = os.path.join(REPO, "bt_sensor", "raspberry_pi_unix")
SHELLY_SCRIPT = os.path.join(REPO, "bt_sensor", "shelly_script", "bluecat_shelly.js")
TRACKER_DIR = os.path.join(REPO, "bt_tracker")
NODE_TYPES = ("pi", "esp32", "shelly")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# ---------------------------------------------------------------------------
# Ausgabe
# ---------------------------------------------------------------------------
def _supports_color():
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def info(msg):
    print(("\033[1;36m» \033[0m" if _supports_color() else "» ") + msg)


def ok(msg):
    print(("\033[1;32m✔ \033[0m" if _supports_color() else "OK ") + msg)


def warn(msg):
    print(("\033[1;33m! \033[0m" if _supports_color() else "WARNUNG ") + msg)


def fail(msg):
    print(("\033[1;31m✘ \033[0m" if _supports_color() else "FEHLER ") + msg)


class DeployError(Exception):
    pass


def interactive() -> bool:
    """False, wenn die GUI (oder ein Skript) den Befehl ohne Terminal startet."""
    return os.environ.get("BLUECAT_NONINTERACTIVE") != "1" and sys.stdin is not None and sys.stdin.isatty()


# ---------------------------------------------------------------------------
# fleet.toml
# ---------------------------------------------------------------------------
def _toml():
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:
        try:
            import tomli as tomllib  # type: ignore
        except ModuleNotFoundError:
            raise DeployError("Python < 3.11: bitte 'pip install tomli' ausführen.")
    return tomllib


def _load_toml(path):
    with open(path, "rb") as handle:
        try:
            return _toml().load(handle)
        except _toml().TOMLDecodeError as error:
            raise DeployError(f"{os.path.basename(path)}: {error}")


SETTINGS_LAYOUT = [
    ("mqtt", ["host", "port", "user", "password"], "MQTT-Broker (Home Assistant)"),
    ("wifi", ["ssid", "password"], "WLAN – nur für die ESP32-Firmware"),
    ("esp32", ["ota_password"], "schützt Firmware-Updates per WLAN"),
    ("shelly", ["user", "password", "subnet"], "Zugang zur Shelly-Weboberfläche, Netz für die Suche"),
    ("tracker", ["node", "target_mac", "engine", "origin_lat", "origin_lon", "origin_bearing_deg", "reference",
                 "reference_lat", "reference_lon", "settings"],
     "TriLola-Tracker; origin leer = aus alter Installation übernehmen"),
]
NODE_KEY_ORDER = ["id", "type", "name", "host", "ssh_user", "ssh_port", "sensor", "ble_mac", "wifi_mac", "legacy_id"]
NODE_DEFAULTS = {"ssh_user": "pi", "ssh_port": 22, "sensor": True}


def toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{toml_key(k)} = {toml_value(v)}" for k, v in value.items()) + " }"
    # JSON-Strings sind gültige TOML-Basic-Strings
    return json.dumps(str(value), ensure_ascii=False)


def toml_key(key) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", str(key)) else json.dumps(str(key), ensure_ascii=False)


def dump_fleet_data(data: dict) -> str:
    """Schreibt die Rohdaten von fleet.toml (wie von tomllib gelesen) wieder als TOML."""
    out = ["# Bluecat/TriLola – Rollout-Konfiguration (enthält Passwörter, wird nicht committet).",
           "# Bearbeiten mit der Rollout-Oberfläche (rollout.bat) oder von Hand;",
           "# prüfen: python deploy/bluecat_deploy.py check", ""]
    known = set()
    for section, keys, comment in SETTINGS_LAYOUT:
        known.add(section)
        values = dict(data.get(section) or {})
        out.append(f"[{section}]" + (f"  # {comment}" if comment else ""))
        for key in keys + [k for k in values if k not in keys]:
            if key not in values or values[key] is None:
                continue
            if key in ("origin_lat", "origin_lon", "reference", "reference_lat", "reference_lon") and values[key] == "":
                continue
            if key == "settings" and not values[key]:
                continue
            out.append(f"{toml_key(key)} = {toml_value(values[key])}")
        out.append("")
    for section, values in data.items():
        if section in known or section == "node" or not isinstance(values, dict):
            continue
        out.append(f"[{toml_key(section)}]")
        out += [f"{toml_key(k)} = {toml_value(v)}" for k, v in values.items()]
        out.append("")
    titles = {"pi": "Raspberry Pis", "esp32": "ESP32", "shelly": "Shellys"}
    nodes = list(data.get("node") or [])
    for node_type in list(titles) + sorted({n.get("type", "") for n in nodes} - set(titles)):
        group = [n for n in nodes if n.get("type", "") == node_type]
        if not group:
            continue
        out.append(f"# ---- {titles.get(node_type, node_type or 'ohne Typ')} " + "-" * 50)
        for node in group:
            out.append("[[node]]")
            for key in NODE_KEY_ORDER + [k for k in node if k not in NODE_KEY_ORDER]:
                if key not in node or node[key] is None or node[key] == "":
                    continue
                if key != "type" and NODE_DEFAULTS.get(key, object()) == node[key]:
                    continue
                if key in ("ssh_user", "ssh_port") and node_type != "pi":
                    continue
                out.append(f"{toml_key(key)} = {toml_value(node[key])}")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def read_fleet_data(path) -> dict:
    if not os.path.exists(path):
        return {}
    return _load_toml(path)


def write_fleet_data(path, data: dict) -> Fleet:
    """Prüft (Round-Trip) und schreibt fleet.toml atomar; alte Version → fleet.toml.bak."""
    text = dump_fleet_data(data)
    try:
        parsed = _toml().loads(text)
    except _toml().TOMLDecodeError as error:
        raise DeployError(f"interner Fehler beim Schreiben von fleet.toml: {error}")
    fleet = fleet_from_data(parsed, path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    os.replace(tmp, path)
    return fleet


def normalize_mac(value) -> str:
    h = re.sub(r"[^0-9a-fA-F]", "", str(value or "")).lower()
    if len(h) != 12:
        return ""
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


def wifi_to_ble_mac(wifi_mac: str) -> str:
    """ESP32: BT-MAC = Basis-MAC (WLAN STA) + 2."""
    h = re.sub(r"[^0-9a-f]", "", wifi_mac.lower())
    if len(h) != 12:
        return ""
    value = (int(h, 16) + 2) & 0xFFFFFFFFFFFF
    h = f"{value:012x}"
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


@dataclass
class Node:
    id: str
    type: str
    name: str = ""
    host: str = ""
    ssh_user: str = "pi"
    ssh_port: int = 22
    tracker: bool = False
    sensor: bool = True
    ble_mac: str = ""
    wifi_mac: str = ""
    legacy_id: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def display(self):
        return self.name or self.id


@dataclass
class Fleet:
    path: str
    mqtt: dict
    wifi: dict
    esp32: dict
    shelly: dict
    tracker: dict
    nodes: List[Node]
    raw: dict = field(default_factory=dict)

    def node(self, node_id) -> Node:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise DeployError(f"Knoten '{node_id}' steht nicht in {os.path.basename(self.path)}.")

    def select(self, node_type, names) -> List[Node]:
        of_type = [n for n in self.nodes if n.type == node_type]
        if not names or names == ["all"]:
            return of_type
        chosen = []
        for name in names:
            node = self.node(name)
            if node.type != node_type:
                raise DeployError(f"'{name}' ist vom Typ {node.type}, nicht {node_type}.")
            chosen.append(node)
        return chosen

    @property
    def tracker_node(self) -> Optional[Node]:
        for n in self.nodes:
            if n.type == "pi" and n.tracker:
                return n
        return None

    @property
    def tracker_in_addon(self) -> bool:
        return self.tracker.get("node") == TRACKER_ADDON

    @property
    def target_mac(self) -> str:
        return normalize_mac(self.tracker.get("target_mac", ""))


def load_fleet(path) -> Fleet:
    if not os.path.exists(path):
        raise DeployError(
            f"{path} fehlt. Vorlage kopieren: deploy/fleet.example.toml → deploy/fleet.toml"
        )
    return fleet_from_data(_load_toml(path), path)


def fleet_from_data(data: dict, path: str) -> Fleet:
    nodes = []
    for raw in data.get("node", []):
        raw = dict(raw)
        known = {k: raw.pop(k) for k in list(raw) if k in Node.__dataclass_fields__}
        known.setdefault("type", "")
        node = Node(**known, extra=raw)
        node.ble_mac = normalize_mac(node.ble_mac)
        node.wifi_mac = normalize_mac(node.wifi_mac)
        nodes.append(node)
    tracker = dict(data.get("tracker", {}))
    fleet = Fleet(
        path=path,
        mqtt=dict(data.get("mqtt", {})),
        wifi=dict(data.get("wifi", {})),
        esp32=dict(data.get("esp32", {})),
        shelly=dict(data.get("shelly", {})),
        tracker=tracker,
        nodes=nodes,
    )
    # Kurzform: [tracker] node = "ron"
    if tracker.get("node") and tracker["node"] != TRACKER_ADDON:
        fleet.node(tracker["node"]).tracker = True
    fleet.raw = data
    return fleet


def validate_fleet(fleet: Fleet) -> List[str]:
    problems = []
    if not fleet.mqtt.get("host"):
        problems.append("[mqtt] host fehlt")
    seen = set()
    for n in fleet.nodes:
        if not ID_RE.match(n.id or ""):
            problems.append(f"ungültige id: {n.id!r}")
        if n.id in seen:
            problems.append(f"id doppelt: {n.id}")
        seen.add(n.id)
        if n.type not in NODE_TYPES:
            problems.append(f"{n.id}: type muss {', '.join(NODE_TYPES)} sein")
        if n.type == "pi" and not n.host:
            problems.append(f"{n.id}: host fehlt (IP des Pi)")
        raw = next((r for r in (fleet.raw.get("node") or []) if r.get("id") == n.id), {})
        for key in ("ble_mac", "wifi_mac"):
            if raw.get(key) and not normalize_mac(raw.get(key)):
                problems.append(f"{n.id}: {key} {raw.get(key)!r} ist keine MAC-Adresse")
        if n.type == "shelly" and not (n.host or n.wifi_mac):
            problems.append(f"{n.id}: host oder wifi_mac nötig")
    trackers = [n for n in fleet.nodes if n.type == "pi" and n.tracker]
    if len(trackers) > 1:
        problems.append("mehr als ein Pi mit tracker = true")
    if fleet.tracker_in_addon and trackers:
        problems.append("Tracker gleichzeitig im Add-on und auf " + trackers[0].id)
    if fleet.tracker.get("target_mac") and not fleet.target_mac:
        problems.append("[tracker] target_mac ist keine MAC-Adresse")
    for key in ("origin_lat", "origin_lon", "origin_bearing_deg"):
        value = fleet.tracker.get(key)
        if value not in (None, "") and not isinstance(value, (int, float)):
            problems.append(f"[tracker] {key} muss eine Zahl sein")
    if any(n.type == "esp32" for n in fleet.nodes):
        if not fleet.wifi.get("ssid"):
            problems.append("[wifi] ssid fehlt (für die ESP32-Firmware)")
        if not fleet.esp32.get("ota_password"):
            problems.append("[esp32] ota_password fehlt (schützt Updates per WLAN)")
    return problems


def load_cache():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as handle:
        json.dump(cache, handle, indent=1)


# ---------------------------------------------------------------------------
# Datei-Generatoren
# ---------------------------------------------------------------------------
def py_literal(value):
    return repr(value)


def render_sensor_secrets(fleet: Fleet, node: Node) -> str:
    lines = [
        "# Erzeugt von deploy/bluecat_deploy.py aus fleet.toml – nicht von Hand ändern.",
        f"SENSOR_ID = {py_literal(node.id)}",
        f"SENSOR_NAME = {py_literal(node.display)}",
        f"STATE_TOPIC = {py_literal(f'bluecat/{node.id}/sensor/state')}",
        f"MESH_TOPIC = {py_literal(f'bluecat/{node.id}/sensor/mesh')}",
        f"AVAILABILITY_TOPIC = {py_literal(f'bluecat/{node.id}/sensor/status')}",
        f"MQTT_BROKER = {py_literal(fleet.mqtt.get('host', ''))}",
        f"MQTT_PORT = {int(fleet.mqtt.get('port', 1883))}",
        f"MQTT_USER = {py_literal(fleet.mqtt.get('user', ''))}",
        f"MQTT_PASSWORD = {py_literal(fleet.mqtt.get('password', ''))}",
        f"TARGET_MAC = {py_literal(fleet.target_mac)}",
        "MESH_ENABLED = True",
        f"MESH_ADVERTISING_ENABLED = {bool(node.extra.get('mesh_advertising', True))!r}",
        f"MESH_ADAPTER = {py_literal(node.extra.get('adapter', 'hci0'))}",
    ]
    return "\n".join(lines) + "\n"


def render_tracker_secrets(fleet: Fleet) -> str:
    t = fleet.tracker
    lines = [
        "# Erzeugt von deploy/bluecat_deploy.py aus fleet.toml – nicht von Hand ändern.",
        f"MQTT_BROKER = {py_literal(fleet.mqtt.get('host', ''))}",
        f"MQTT_PORT = {int(fleet.mqtt.get('port', 1883))}",
        f"MQTT_USER = {py_literal(fleet.mqtt.get('user', ''))}",
        f"MQTT_PASSWORD = {py_literal(fleet.mqtt.get('password', ''))}",
        f"TRACKING_ENGINE = {py_literal(t.get('engine', 'pf'))}",
    ]
    if t.get("origin_lat") not in (None, "") and t.get("origin_lon") not in (None, ""):
        lines.append(f"ORIGIN_LAT = {float(t['origin_lat'])!r}")
        lines.append(f"ORIGIN_LON = {float(t['origin_lon'])!r}")
    lines.append(f"ORIGIN_BEARING_DEG = {float(t.get('origin_bearing_deg', 0.0))!r}")
    for key, value in sorted((t.get("settings") or {}).items()):
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            lines.append(f"{key} = {py_literal(value)}")
    return "\n".join(lines) + "\n"


def c_string(value) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def render_esp_secrets(fleet: Fleet) -> str:
    return "\n".join([
        "#pragma once",
        "// Erzeugt von deploy/bluecat_deploy.py aus fleet.toml – nicht von Hand ändern.",
        f"#define WIFI_SSID {c_string(fleet.wifi.get('ssid', ''))}",
        f"#define WIFI_PASSWORD {c_string(fleet.wifi.get('password', ''))}",
        f"#define MQTT_BROKER {c_string(fleet.mqtt.get('host', ''))}",
        f"#define MQTT_PORT {int(fleet.mqtt.get('port', 1883))}",
        f"#define MQTT_USER {c_string(fleet.mqtt.get('user', ''))}",
        f"#define MQTT_PASSWORD {c_string(fleet.mqtt.get('password', ''))}",
        f"#define OTA_PASSWORD {c_string(fleet.esp32.get('ota_password', ''))}",
        f"#define TARGET_MAC {c_string(fleet.target_mac)}",
        "#define MESH_ENABLED 1",
        '#define MESH_PEER_MACS ""',
        "",
    ])


def build_pi_bundle(fleet: Fleet, node: Node) -> bytes:
    """tar.gz mit Installer, Sensor- und ggf. Tracker-Dateien."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add_bytes(name, data: bytes, mode=0o644):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mode = mode
            ti.mtime = int(time.time())
            tar.addfile(ti, io.BytesIO(data))

        def add_file(path, name, mode=0o644):
            with open(path, "rb") as handle:
                data = handle.read()
            if path.endswith((".sh", ".py", ".txt", ".json", ".md")):
                data = data.replace(b"\r\n", b"\n")  # Windows-Checkout → Unix-Zeilenenden
            add_bytes(name, data, mode)

        add_file(os.path.join(HERE, "remote", "pi_install.sh"), "pi_install.sh", 0o755)
        env = {
            "NODE_ID": node.id,
            "ROLE_SENSOR": "1" if node.sensor else "0",
            "ROLE_TRACKER": "1" if node.tracker else "0",
            "BLUETOOTH_EXPERIMENTAL": "1" if node.extra.get("bluetooth_experimental", True) else "0",
            # auto = USB-Stick nutzen, wenn einer steckt; usb / intern = fest
            "BLUETOOTH": str(node.extra.get("bluetooth", "auto")) if str(node.extra.get("bluetooth", "auto"))
            in {"auto", "usb", "intern"} else "auto",
        }
        add_bytes("deploy.env", "".join(f"{k}={v}\n" for k, v in env.items()).encode())
        add_file(os.path.join(PI_SENSOR_DIR, "bluecat2mqtt.py"), "sensor/bluecat2mqtt.py")
        add_file(os.path.join(PI_SENSOR_DIR, "requirements.txt"), "sensor/requirements.txt")
        add_bytes("sensor/secrets_blue.py", render_sensor_secrets(fleet, node).encode("utf-8"))
        if node.tracker:
            skip_dirs = {"config", "recordings", "__pycache__", ".pytest_cache", "tests", "secrets"}
            for root, dirs, files in os.walk(TRACKER_DIR):
                dirs[:] = [d for d in dirs if d not in skip_dirs]
                for name in files:
                    if name.endswith((".pyc",)) or name in {"secrets_tri.py"}:
                        continue
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, TRACKER_DIR).replace(os.sep, "/")
                    add_file(full, f"tracker/{rel}")
            add_bytes("tracker/secrets_tri.py", render_tracker_secrets(fleet).encode("utf-8"))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# SSH / Raspberry Pi
# ---------------------------------------------------------------------------
def _ssh_target(node: Node):
    return f"{node.ssh_user}@{node.host}"


def _home_ssh_opts():
    """Schlüssel/known_hosts ausdrücklich aus $HOME – OpenSSH nimmt sonst das Home aus /etc/passwd
    (in der HA-App liegt der Schlüssel unter /data/.ssh, nicht /root/.ssh)."""
    opts = ["-o", "UserKnownHostsFile=" + os.path.expanduser("~/.ssh/known_hosts")]
    key = os.path.expanduser("~/.ssh/id_ed25519")
    if os.path.exists(key):
        opts += ["-i", key]
    return opts


def _ssh_opts(node: Node, batch=False):
    opts = ["-o", "StrictHostKeyChecking=accept-new", "-p", str(node.ssh_port)] + _home_ssh_opts()
    key = node.extra.get("ssh_key")
    if key:
        opts += ["-i", os.path.expanduser(key)]
    if batch:
        opts += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
    return opts


def run_tee(cmd, **kwargs):
    """Wie run(), gibt die Ausgabe live weiter und liefert sie zusätzlich zurück."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", **kwargs)
    captured = []
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        captured.append(line)
    return proc.wait(), "".join(captured)


def run(cmd, check=True, **kwargs):
    result = subprocess.run(cmd, **kwargs)
    if check and result.returncode != 0:
        raise DeployError(f"Befehl fehlgeschlagen ({result.returncode}): {' '.join(cmd[:4])} …")
    return result


def fleet_secrets(fleet_or_data) -> List[str]:
    """Alle Passwörter aus fleet.toml (auch frühere OTA-Passwörter) – zum Schwärzen von Ausgaben."""
    if isinstance(fleet_or_data, Fleet):
        data = {"mqtt": fleet_or_data.mqtt, "wifi": fleet_or_data.wifi, "esp32": fleet_or_data.esp32,
                "shelly": fleet_or_data.shelly}
    else:
        data = fleet_or_data or {}
    values = []
    for section in ("mqtt", "wifi", "esp32", "shelly"):
        for key, value in (data.get(section) or {}).items():
            if "password" not in key:
                continue
            for item in (value if isinstance(value, list) else [value]):
                if isinstance(item, str) and len(item) >= 4:
                    values.append(item)
    return sorted(set(values), key=len, reverse=True)


def redact(text: str, secrets: List[str]) -> str:
    for secret in secrets:
        escaped = secret.replace("\\", "\\\\")
        for form in {secret, repr(secret)[1:-1], escaped.replace("'", "\\'"), escaped.replace('"', '\\"')}:
            if form:
                text = text.replace(form, "***")
    return text


def run_redacted(cmd, secrets: List[str], env=None) -> Tuple[int, str]:
    """Wie run(), aber die Ausgabe läuft zeilenweise geschwärzt durch (espota druckt z. B. das Passwort)."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            env=env, text=True, encoding="utf-8", errors="replace", bufsize=1)
    lines = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = redact(line.rstrip("\r\n"), secrets)
        lines.append(line)
        print(line, flush=True)
    return proc.wait(), "\n".join(lines)


def ssh_run(node: Node, remote_cmd: str, tty=False, check=True, capture=False, input_text=None):
    """tty nur im Terminal; ohne Terminal (GUI) BatchMode, damit nichts auf ein Passwort wartet."""
    use_tty = tty and interactive() and input_text is None
    cmd = ["ssh"] + (["-t"] if use_tty else []) + _ssh_opts(node, batch=not interactive()) + \
        [_ssh_target(node), remote_cmd]
    kwargs = {"capture_output": capture, "text": True if (capture or input_text is not None) else None}
    if input_text is not None:
        kwargs["input"] = input_text
    elif not interactive():
        kwargs["stdin"] = subprocess.DEVNULL
    result = run(cmd, check=False, **kwargs)
    if result.returncode == 255 and not interactive():
        raise DeployError(f"{node.id}: SSH-Anmeldung an {_ssh_target(node)} fehlgeschlagen – "
                          "zuerst „SSH-Zugang einrichten“ (Schlüssel), IP/Benutzer prüfen")
    if check and result.returncode != 0:
        raise DeployError(f"{node.id}: Befehl auf dem Pi fehlgeschlagen ({result.returncode})")
    return result


def scp_upload(node: Node, local: str, remote: str):
    cmd = ["scp", "-q", "-o", "StrictHostKeyChecking=accept-new", "-P", str(node.ssh_port)] + _home_ssh_opts()
    if not interactive():
        cmd += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
    if node.extra.get("ssh_key"):
        cmd += ["-i", os.path.expanduser(node.extra["ssh_key"])]
    result = run(cmd + [local, f"{_ssh_target(node)}:{remote}"], check=False,
                 stdin=None if interactive() else subprocess.DEVNULL)
    if result.returncode != 0:
        raise DeployError(f"{node.id}: Upload per scp fehlgeschlagen – SSH-Zugang eingerichtet? "
                          f"({_ssh_target(node)})")


def _install_key_with_password(node: Node, pub: str, password: str):
    """Erstanmeldung mit Passwort (GUI): Schlüssel per paramiko eintragen."""
    try:
        import paramiko  # type: ignore
    except ModuleNotFoundError:
        raise DeployError("Für die Anmeldung mit Passwort: pip install paramiko")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(node.host, port=int(node.ssh_port), username=node.ssh_user, password=password,
                       look_for_keys=False, allow_agent=False, timeout=10, auth_timeout=15)
    except paramiko.AuthenticationException:
        raise DeployError(f"{node.id}: Passwort für {_ssh_target(node)} falsch")
    except (OSError, paramiko.SSHException) as error:
        raise DeployError(f"{node.id}: {node.host} nicht erreichbar ({error})")
    try:
        remote = ("umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; "
                  f"grep -qxF '{pub}' ~/.ssh/authorized_keys || echo '{pub}' >> ~/.ssh/authorized_keys")
        _, stdout, stderr = client.exec_command(remote, timeout=20)
        if stdout.channel.recv_exit_status() != 0:
            raise DeployError(f"{node.id}: authorized_keys nicht schreibbar: {stderr.read().decode(errors='replace')}")
    finally:
        client.close()


def cmd_ssh_setup(fleet: Fleet, names):
    key = os.path.expanduser("~/.ssh/id_ed25519")
    if not os.path.exists(key):
        info("Erzeuge SSH-Schlüssel ~/.ssh/id_ed25519 ...")
        os.makedirs(os.path.dirname(key), exist_ok=True)
        run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", key, "-C", "bluecat-deploy"])
    with open(key + ".pub", "r", encoding="utf-8") as handle:
        pub = handle.read().strip()
    for node in fleet.select("pi", names):
        probe = subprocess.run(["ssh"] + _ssh_opts(node, batch=True) + [_ssh_target(node), "true"],
                               capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if probe.returncode == 0:
            ok(f"{node.id}: Schlüssel funktioniert bereits")
            continue
        password = os.environ.get("BLUECAT_SSH_PASSWORD")
        if password is not None:
            _install_key_with_password(node, pub, password)
        elif not interactive():
            raise DeployError(f"{node.id}: Passwort für {_ssh_target(node)} nötig")
        else:
            info(f"{node.id}: Passwort für {_ssh_target(node)} eingeben (einmalig) ...")
            remote = ("umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; "
                      f"grep -qxF '{pub}' ~/.ssh/authorized_keys || echo '{pub}' >> ~/.ssh/authorized_keys")
            ssh_run(node, remote)
        again = subprocess.run(["ssh"] + _ssh_opts(node, batch=True) + [_ssh_target(node), "true"],
                               capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if again.returncode != 0:
            raise DeployError(f"{node.id}: Schlüssel eingetragen, Anmeldung klappt aber noch nicht: "
                              f"{again.stderr.strip()[-200:]}")
        ok(f"{node.id}: Schlüssel installiert – Anmeldung ohne Passwort funktioniert")


def cmd_import_old(fleet: Fleet, names, as_json=False):
    """Liest alte Einstellungen (MQTT, Ziel-MAC, Ursprung) von den Pis."""
    probe_script = r'''
import ast, glob, json, os, runpy, subprocess
out = {}
files = set(glob.glob(os.path.expanduser("~/**/secrets_blue.py"), recursive=True))
files |= set(glob.glob(os.path.expanduser("~/**/secrets_tri.py"), recursive=True))
files |= set(glob.glob(os.path.expanduser("~/**/secrets/*.py"), recursive=True))
files |= set(glob.glob(os.path.expanduser("~/**/secrets/*/__init__.py"), recursive=True))
def literals(path):
    values = {}
    try:
        tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
    except (OSError, SyntaxError, ValueError):
        return values
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                values[node.targets[0].id] = ast.literal_eval(node.value)
            except Exception:
                pass
    return values
for f in sorted(files):
    if "/bluecat/" in f or "/site-packages/" in f or "/lib/python" in f:
        continue
    d = literals(f)
    try:
        d.update(runpy.run_path(f))
    except Exception:
        pass
    vals = {k: v for k, v in d.items() if k.isupper() and isinstance(v, (str, int, float, bool))}
    if vals:
        out[f] = vals
units = subprocess.run("systemctl list-units --type=service --all --no-legend", shell=True, capture_output=True, text=True).stdout
out["_units"] = [l.split()[0] for l in units.splitlines() if any(x in l for x in ("bluecat", "trilola"))]
print(json.dumps(out))
'''
    results = {}
    for node in fleet.select("pi", names):
        if not as_json:
            info(f"{node.id} ({node.host}):")
        res = subprocess.run(["ssh"] + _ssh_opts(node, batch=True) + [_ssh_target(node), "python3 -"],
                             input=probe_script, capture_output=True, text=True)
        if res.returncode != 0:
            results[node.id] = {"_error": res.stderr.strip()[-300:] or "SSH fehlgeschlagen"}
            if not as_json:
                fail(results[node.id]["_error"])
            continue
        try:
            data = json.loads(res.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            results[node.id] = {"_error": "Antwort nicht lesbar"}
            continue
        results[node.id] = data
        if as_json:
            continue
        data = dict(data)
        print(f"    Dienste: {', '.join(data.pop('_units', [])) or '-'}")
        for path, values in data.items():
            print(f"    {path}")
            for k in ("SENSOR_ID", "MQTT_BROKER", "MQTT_PORT", "MQTT_USER", "MQTT_PASSWORD", "TARGET_MAC",
                      "ORIGIN_LAT", "ORIGIN_LON", "BLE_TAG_ID"):
                if k in values:
                    shown = "***" if "PASSWORD" in k and values[k] else values[k]
                    print(f"        {k} = {shown!r}")
    if as_json:
        print(json.dumps(results, ensure_ascii=False))
    else:
        print("\nWerte bei Bedarf in deploy/fleet.toml übernehmen ([mqtt], [tracker]).")
    return results


def cmd_pi(fleet: Fleet, names, dry_run=False, rollback=False):
    failures = []
    for node in fleet.select("pi", names):
        try:
            if not _deploy_pi_node(fleet, node, dry_run, rollback):
                failures.append(node.id)
        except DeployError as error:
            fail(str(error))
            failures.append(node.id)
    if failures:
        raise DeployError("Pi-Rollout unvollständig: " + ", ".join(failures))


def _deploy_pi_node(fleet: Fleet, node: Node, dry_run=False, rollback=False) -> bool:
    roles = " + ".join(r for r, on in (("Sensor", node.sensor), ("Tracker", node.tracker)) if on)
    info(f"{node.id} ({_ssh_target(node)}): {roles or 'nichts'}")
    sudo_pw = os.environ.get("BLUECAT_SUDO_PASSWORD")
    prefix = "BLUECAT_SUDO_FROM_STDIN=1 " if sudo_pw is not None else ""
    stdin_text = (sudo_pw + "\n") if sudo_pw is not None else None
    if rollback:
        result = ssh_run(node, prefix + "bash ~/bluecat/.incoming/pi_install.sh rollback", tty=True,
                         check=False, input_text=stdin_text)
        return result.returncode == 0
    bundle = build_pi_bundle(fleet, node)
    if dry_run:
        with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tar:
            names_in = tar.getnames()
        ok(f"Bundle {len(bundle) // 1024} KB, {len(names_in)} Dateien (Trockenlauf, nichts übertragen)")
        return True
    with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as tmp:
        tmp.write(bundle)
        local = tmp.name
    try:
        scp_upload(node, local, "/tmp/bluecat_bundle.tgz")
    finally:
        os.unlink(local)
    remote = ("set -e; rm -rf ~/bluecat/.incoming; mkdir -p ~/bluecat/.incoming; "
              "tar xzf /tmp/bluecat_bundle.tgz -C ~/bluecat/.incoming; rm -f /tmp/bluecat_bundle.tgz; "
              + prefix + "bash ~/bluecat/.incoming/pi_install.sh")
    result = ssh_run(node, remote, tty=True, check=False, input_text=stdin_text)
    if result.returncode == 0:
        ok(f"{node.id} fertig")
        return True
    fail(f"{node.id}: Installation meldet Fehler (Ausgabe oben). Rückgängig: "
         f"python deploy/bluecat_deploy.py pi {node.id} --rollback")
    return False


# ---------------------------------------------------------------------------
# Shelly (Gen2+ RPC über HTTP, Digest-Auth SHA-256)
# ---------------------------------------------------------------------------
class ShellyRPC:
    def __init__(self, host, user="admin", password="", timeout=6.0):
        self.host = host
        self.user = user or "admin"
        self.password = password or ""
        self.timeout = timeout
        self._auth = None
        self._nc = 0
        self._id = 0

    def _digest_header(self, challenge: str, method="POST", uri="/rpc"):
        params = dict(re.findall(r'(\w+)="?([^",]+)"?', challenge))
        algorithm = params.get("algorithm", "MD5").upper()
        hfunc = {"SHA-256": hashlib.sha256, "MD5": hashlib.md5}.get(algorithm)
        if hfunc is None:
            raise DeployError(f"{self.host}: unbekannter Digest-Algorithmus {algorithm}")

        def h(text):
            return hfunc(text.encode("utf-8")).hexdigest()

        realm, nonce = params.get("realm", ""), params.get("nonce", "")
        qop = "auth" if "auth" in params.get("qop", "auth") else ""
        self._nc += 1
        nc = f"{self._nc:08x}"
        cnonce = pysecrets.token_hex(8)
        ha1 = h(f"{self.user}:{realm}:{self.password}")
        ha2 = h(f"{method}:{uri}")
        response = h(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}") if qop else h(f"{ha1}:{nonce}:{ha2}")
        parts = [f'username="{self.user}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"',
                 f'algorithm={algorithm}', f'response="{response}"']
        if qop:
            parts += [f"qop={qop}", f"nc={nc}", f'cnonce="{cnonce}"']
        return "Digest " + ", ".join(parts)

    def call(self, method, params=None):
        self._id += 1
        # UTF-8 direkt senden: der JSON-Parser der Shelly-Firmware (mjson) versteht nur \u00XX-Escapes
        body = json.dumps({"id": self._id, "method": method, "params": params or {}},
                          ensure_ascii=False).encode("utf-8")
        for attempt in range(2):
            req = urllib.request.Request(f"http://{self.host}/rpc", data=body, method="POST",
                                         headers={"Content-Type": "application/json; charset=utf-8"})
            if self._auth:
                req.add_header("Authorization", self._auth)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    reply = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as error:
                if error.code == 401 and attempt == 0:
                    if not self.password:
                        raise DeployError(f"{self.host}: Passwort nötig – [shelly] password in fleet.toml setzen")
                    self._auth = self._digest_header(error.headers.get("WWW-Authenticate", ""))
                    continue
                raise DeployError(f"{self.host}: HTTP {error.code} bei {method}")
            except (urllib.error.URLError, OSError) as error:
                raise DeployError(f"{self.host}: nicht erreichbar ({error})")
        if "error" in reply:
            err = reply["error"]
            raise DeployError(f"{self.host}: {method} → {err.get('code')} {err.get('message')}")
        return reply.get("result")


def _probe_shelly(ip, timeout=2.5):
    try:
        with urllib.request.urlopen(f"http://{ip}/shelly", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict) and data.get("mac"):
            return {"ip": str(ip), "mac": normalize_mac(data.get("mac")), "id": data.get("id", ""),
                    "name": data.get("name") or "", "model": data.get("model") or data.get("type", ""),
                    "gen": data.get("gen", 1), "ver": data.get("ver") or data.get("fw", ""),
                    "auth": bool(data.get("auth_en") or data.get("auth"))}
    except Exception:
        return None
    return None


_SCAN_DONE = False


IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
MAC_RE = re.compile(r"\b([0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5})\b")


def read_arp_table() -> Dict[str, str]:
    """MAC → IP aus dem ARP-Cache dieses PCs (nach der Suche enthält er alle aktiven Geräte)."""
    table: Dict[str, str] = {}
    texts = []
    if os.path.exists("/proc/net/arp"):
        try:
            with open("/proc/net/arp", encoding="utf-8", errors="replace") as handle:
                texts.append(handle.read())
        except OSError:
            pass
    commands = [["arp", "-a"]] if os.name == "nt" else [["ip", "neigh"], ["arp", "-an"]]
    for cmd in commands:
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=5, stdin=subprocess.DEVNULL)
            texts.append(res.stdout.decode("utf-8", "replace") + res.stdout.decode("cp850", "replace"))
        except (OSError, subprocess.SubprocessError):
            continue
    for text in texts:
        for line in text.splitlines():
            ip, mac = IP_RE.search(line), MAC_RE.search(line)
            if not ip or not mac:
                continue
            mac = normalize_mac(mac.group(1))
            if mac in ("ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00") or mac.startswith("01:00:5e"):
                continue
            table.setdefault(mac, ip.group(1))
    return table


def scan_shellys(fleet: Fleet, subnet=None) -> List[dict]:
    global _SCAN_DONE
    _SCAN_DONE = True
    subnet = subnet or fleet.shelly.get("subnet")
    if not subnet:
        subnet = ".".join(str(fleet.mqtt.get("host", "192.168.1.1")).split(".")[:3]) + ".0/24"
    net = ipaddress.ip_network(subnet, strict=False)
    info(f"Suche Shellys in {net} ...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        found = [r for r in pool.map(lambda ip: _probe_shelly(ip, timeout=2.5), net.hosts()) if r]
    # Fehlende Shellys aus fleet.toml über den ARP-Cache suchen: träge Geräte (BLE-Skript, schwaches WLAN)
    # verpassen die schnelle Suche oft, sind aber per MAC im Netz sichtbar.
    wanted = {n.wifi_mac: n for n in fleet.nodes if n.type == "shelly" and n.wifi_mac}
    missing = set(wanted) - {d["mac"] for d in found}
    arp_only = {}
    if missing:
        arp = read_arp_table()
        for mac in sorted(missing):
            ip = arp.get(mac)
            if not ip or ipaddress.ip_address(ip) not in net:
                continue
            dev = None
            for _ in range(3):
                dev = _probe_shelly(ip, timeout=6.0)
                if dev:
                    break
            if dev and dev["mac"] == mac:
                info(f"{wanted[mac].id}: über die MAC-Adresse gefunden ({ip}, antwortet langsam)")
                found.append(dev)
            else:
                warn(f"{wanted[mac].id}: ist unter {ip} im Netz, antwortet aber nicht auf HTTP – "
                     "Shelly neu starten (Strom kurz aus) und erneut versuchen")
                arp_only[mac] = ip
    cache = load_cache()
    cache.setdefault("shelly", {})
    for dev in found:
        cache["shelly"][dev["mac"]] = dev["ip"]
    for mac, ip in arp_only.items():
        cache["shelly"][mac] = ip
    save_cache(cache)
    return found


def resolve_shelly_host(fleet: Fleet, node: Node, allow_scan=True) -> str:
    if node.host:
        return node.host
    cache = load_cache().get("shelly", {})
    if node.wifi_mac in cache:
        return cache[node.wifi_mac]
    if allow_scan and not _SCAN_DONE:
        for dev in scan_shellys(fleet):
            if dev["mac"] == node.wifi_mac:
                return dev["ip"]
    raise DeployError(f"{node.id}: Shelly mit WLAN-MAC {node.wifi_mac} nicht im Netz gefunden – eingeschaltet und "
                      "im WLAN? IP in der Shelly-App bzw. im Router nachsehen und beim Gerät als feste IP eintragen")


def _script_is_ours(rpc: ShellyRPC, script) -> bool:
    if str(script.get("name", "")).lower() in {"bluecat", "trilola"}:
        return True
    try:
        head = rpc.call("Script.GetCode", {"id": script["id"], "offset": 0, "len": 400}).get("data", "")
    except DeployError:
        return False
    return "Bluecat BLE-Sensor" in head or "TriLola" in head or "Bluecat-Sensor" in head


def utf8_chunks(text: str, max_bytes: int = 1024):
    """Teilt Text in Stücke von höchstens max_bytes UTF-8-Bytes, ohne Zeichen zu zerschneiden."""
    piece, size = [], 0
    for char in text:
        n = len(char.encode("utf-8"))
        if size + n > max_bytes and piece:
            yield "".join(piece)
            piece, size = [], 0
        piece.append(char)
        size += n
    if piece:
        yield "".join(piece)


def deploy_shelly(fleet: Fleet, node: Node, code: str, fix_settings=False, dry_run=False, host=None):
    host = host or resolve_shelly_host(fleet, node)
    rpc = ShellyRPC(host, fleet.shelly.get("user", "admin"), fleet.shelly.get("password", ""))
    dev = rpc.call("Shelly.GetDeviceInfo")
    mac = normalize_mac(dev.get("mac"))
    if node.wifi_mac and mac != node.wifi_mac:
        raise DeployError(f"{node.id}: {host} ist {mac}, erwartet {node.wifi_mac} – falsche IP?")
    if int(dev.get("gen", 2)) < 2:
        raise DeployError(f"{node.id}: Gen1-Shelly unterstützt keine Skripte")
    info(f"{node.id}: {dev.get('model')} {dev.get('ver')} @ {host} (MAC {mac})")

    kvs = {
        "bluecat.sensor_id": node.id,
        "bluecat.name": node.display,
        "bluecat.legacy_id": node.legacy_id or "",
        "bluecat.target_mac": fleet.target_mac,
    }
    if node.ble_mac:
        kvs["bluecat.ble_mac"] = node.ble_mac

    # Einstellungen prüfen
    settings_changes = []
    ble = rpc.call("BLE.GetConfig") or {}
    if not ble.get("enable", False):
        settings_changes.append(("BLE.SetConfig", {"config": {"enable": True}}, "Bluetooth aktivieren"))
    mqtt_cfg = rpc.call("MQTT.GetConfig") or {}
    want_server = f"{fleet.mqtt.get('host')}:{int(fleet.mqtt.get('port', 1883))}"
    server = str(mqtt_cfg.get("server") or "")
    if not mqtt_cfg.get("enable") or server not in (want_server, str(fleet.mqtt.get("host"))):
        change = {"enable": True, "server": want_server}
        if fleet.mqtt.get("user"):
            change.update({"user": fleet.mqtt["user"], "pass": fleet.mqtt.get("password", "")})
        settings_changes.append(("MQTT.SetConfig", {"config": change}, f"MQTT auf {want_server}"))
    sysc = rpc.call("Sys.GetConfig") or {}
    if (sysc.get("device") or {}).get("eco_mode"):
        settings_changes.append(("Sys.SetConfig", {"config": {"device": {"eco_mode": False}}}, "Eco-Mode aus"))

    if dry_run:
        ok(f"{node.id}: würde KVS {sorted(kvs)} setzen, Skript {len(code)} Zeichen hochladen"
           + (f", Einstellungen: {', '.join(c[2] for c in settings_changes)}" if settings_changes else ""))
        return

    for key, value in kvs.items():
        rpc.call("KVS.Set", {"key": key, "value": value})

    scripts = (rpc.call("Script.List") or {}).get("scripts", [])
    ours = [s for s in scripts if _script_is_ours(rpc, s)]
    target = next((s for s in ours if str(s.get("name", "")).lower() == "bluecat"), ours[0] if ours else None)
    for s in ours:
        if target is not None and s["id"] == target["id"]:
            continue
        info(f"{node.id}: altes Skript '{s.get('name')}' (id {s['id']}) wird deaktiviert")
        if s.get("running"):
            rpc.call("Script.Stop", {"id": s["id"]})
        rpc.call("Script.SetConfig", {"id": s["id"], "config": {"enable": False}})
    if target is None:
        script_id = rpc.call("Script.Create", {"name": "bluecat"})["id"]
    else:
        script_id = target["id"]
        if target.get("running"):
            rpc.call("Script.Stop", {"id": script_id})

    for index, piece in enumerate(utf8_chunks(code, 1024)):
        rpc.call("Script.PutCode", {"id": script_id, "code": piece, "append": index > 0})
    rpc.call("Script.SetConfig", {"id": script_id, "config": {"name": "bluecat", "enable": True}})
    rpc.call("Script.Start", {"id": script_id})
    time.sleep(float(fleet.shelly.get("verify_wait_sec", 2.0)))
    status = rpc.call("Script.GetStatus", {"id": script_id}) or {}
    if not status.get("running") or status.get("errors"):
        raise DeployError(f"{node.id}: Skript läuft nicht ({status.get('errors') or status})")
    ok(f"{node.id}: Skript läuft (id {script_id})")

    if settings_changes:
        if not fix_settings:
            for _, _, label in settings_changes:
                warn(f"{node.id}: Einstellung abweichend – {label} (mit --fix-settings automatisch)")
            return
        restart = False
        for method, params, label in settings_changes:
            result = rpc.call(method, params) or {}
            restart = restart or bool(result.get("restart_required"))
            ok(f"{node.id}: {label}")
        if restart:
            info(f"{node.id}: Neustart für die Einstellungen ...")
            rpc.call("Shelly.Reboot")


def cmd_shelly(fleet: Fleet, names, fix_settings=False, dry_run=False):
    with open(SHELLY_SCRIPT, "r", encoding="utf-8") as handle:
        code = handle.read().replace("\r\n", "\n")
    nodes = fleet.select("shelly", names)
    if any(not n.host for n in nodes):
        cache = load_cache().get("shelly", {})
        if any(n.wifi_mac not in cache for n in nodes if not n.host):
            scan_shellys(fleet)
    failures = []
    for node in nodes:
        try:
            deploy_shelly(fleet, node, code, fix_settings=fix_settings, dry_run=dry_run)
        except DeployError as error:
            fail(str(error))
            failures.append(node.id)
    if failures:
        raise DeployError("Shelly-Rollout unvollständig: " + ", ".join(failures))


def cmd_shelly_scan(fleet: Fleet):
    found = scan_shellys(fleet)
    by_mac = {n.wifi_mac: n for n in fleet.nodes if n.type == "shelly" and n.wifi_mac}
    print(f"{'IP':15s} {'MAC':17s} {'Modell':16s} {'FW':10s} {'Name':24s} fleet.toml")
    for dev in sorted(found, key=lambda d: ipaddress.ip_address(d["ip"])):
        node = by_mac.get(dev["mac"])
        print(f"{dev['ip']:15s} {dev['mac']:17s} {str(dev['model'])[:16]:16s} {str(dev['ver'])[:10]:10s} "
              f"{str(dev['name'])[:24]:24s} {node.id if node else '-'}")
    missing = [n.id for n in by_mac.values() if n.wifi_mac not in {d['mac'] for d in found}]
    if missing:
        warn("nicht gefunden: " + ", ".join(missing))


# ---------------------------------------------------------------------------
# MQTT-Hilfen
# ---------------------------------------------------------------------------
def _mqtt_client(fleet: Fleet, client_id=None):
    try:
        import paho.mqtt.client as mqtt
    except ModuleNotFoundError:
        raise DeployError("Für diesen Befehl: pip install paho-mqtt")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=client_id or f"bluecat_deploy_{pysecrets.token_hex(3)}")
    if fleet.mqtt.get("user"):
        client.username_pw_set(fleet.mqtt["user"], fleet.mqtt.get("password") or None)
    try:
        client.connect(fleet.mqtt["host"], int(fleet.mqtt.get("port", 1883)), 30)
    except OSError as error:
        raise DeployError(f"MQTT-Broker {fleet.mqtt.get('host')} nicht erreichbar: {error}")
    return client


def mqtt_collect(fleet: Fleet, topics, seconds=2.5) -> Dict[str, str]:
    client = _mqtt_client(fleet)
    messages = {}

    def on_message(c, userdata, msg):
        if msg.retain:
            messages[msg.topic] = msg.payload.decode("utf-8", "replace")

    client.on_message = on_message
    for topic in topics:
        client.subscribe(topic, qos=0)
    client.loop_start()
    time.sleep(seconds)
    client.loop_stop()
    client.disconnect()
    return messages


def mqtt_publish_many(fleet: Fleet, items):
    client = _mqtt_client(fleet)
    client.loop_start()
    infos = [client.publish(topic, payload, qos=1, retain=retain) for topic, payload, retain in items]
    for msg_info in infos:
        msg_info.wait_for_publish(5)
    client.loop_stop()
    client.disconnect()


def read_identities(fleet: Fleet) -> Dict[str, dict]:
    raw = mqtt_collect(fleet, ["bluecat/registry/+/identity", "bluecat/+/sensor/status",
                               "bluecat/+/sensor/state", "bluecat/trilola/status",
                               "bluecat/provision/+", "bluecat/config/tracker/engine/state"])
    result = {"_raw": raw}
    for topic, payload in raw.items():
        parts = topic.split("/")
        if len(parts) == 4 and parts[1] == "registry" and parts[3] == "identity" and payload:
            try:
                result[parts[2]] = json.loads(payload)
            except ValueError:
                pass
    return result


# ---------------------------------------------------------------------------
# ESP32
# ---------------------------------------------------------------------------
def write_esp_secrets(fleet: Fleet):
    path = os.path.join(ESP_DIR, "src", "secrets_ble.h")
    content = render_esp_secrets(fleet)
    old = open(path, encoding="utf-8").read() if os.path.exists(path) else None
    if old != content:
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        ok("secrets_ble.h aus fleet.toml erzeugt")
    return path


def provision_messages(fleet: Fleet):
    items = []
    for node in fleet.nodes:
        if node.type == "esp32" and node.ble_mac:
            key = node.ble_mac.replace(":", "")
            items.append((f"bluecat/provision/{key}",
                          json.dumps({"sensor_id": node.id, "name": node.display}, ensure_ascii=False), True))
    return items


def cmd_esp_provision(fleet: Fleet):
    items = provision_messages(fleet)
    if not items:
        warn("keine ESP32 in fleet.toml")
        return
    mqtt_publish_many(fleet, items)
    for topic, payload, _ in items:
        ok(f"{topic} ← {payload}")


def find_pio():
    for name in ("pio", "platformio"):
        path = shutil.which(name)
        if path:
            return [path]
    for candidate in (os.path.expanduser("~/.platformio/penv/Scripts/pio.exe"),
                      os.path.expanduser("~/.platformio/penv/bin/pio")):
        if os.path.exists(candidate):
            return [candidate]
    try:
        import platformio  # noqa: F401
        return [sys.executable, "-m", "platformio"]
    except ImportError:
        raise DeployError("PlatformIO nicht gefunden (pip install platformio oder VS-Code-Erweiterung)")


def set_node_field(fleet: Fleet, node_id: str, key: str, value):
    """Ändert ein Feld eines Knotens direkt in fleet.toml."""
    data = read_fleet_data(fleet.path)
    for raw in data.get("node", []):
        if raw.get("id") == node_id:
            raw[key] = value
    return write_fleet_data(fleet.path, data)


def cmd_esp_usb(fleet: Fleet, port=None, node_id=None):
    node = fleet.node(node_id) if node_id else None
    if node is not None and node.type != "esp32":
        raise DeployError(f"'{node_id}' ist kein ESP32")
    write_esp_secrets(fleet)
    cmd = find_pio() + ["run", "-d", ESP_DIR, "-e", "esp32dev", "-t", "upload"]
    if port:
        cmd += ["--upload-port", port]
    info("Baue die Firmware und flashe per USB (beim ersten Mal lädt PlatformIO die Werkzeuge, "
         "das dauert einige Minuten) ...")
    code, output = run_tee(cmd, stdin=subprocess.DEVNULL)
    if code != 0:
        hint = ""
        if re.search(r"could not open port|No serial data|Failed to connect", output, re.I):
            hint = " – Port belegt oder ESP32 nicht im Flash-Modus (BOOT-Taste beim Verbinden gedrückt halten)"
        raise DeployError("Flashen fehlgeschlagen" + hint)
    macs = re.findall(r"MAC:\s*([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})", output)
    ble = wifi_to_ble_mac(macs[-1]) if macs else ""
    if ble:
        info(f"Chip-MAC {normalize_mac(macs[-1])} → BLE-MAC {ble}")
    if node is not None and ble and node.ble_mac != ble:
        other = next((n for n in fleet.nodes if n.ble_mac == ble and n.id != node.id), None)
        if other is not None:
            warn(f"Diese BLE-MAC war {other.id} zugeordnet – wird jetzt {node.id}")
            set_node_field(fleet, other.id, "ble_mac", "")
        fleet = set_node_field(fleet, node.id, "ble_mac", ble)
        ok(f"{node.id}: BLE-MAC {ble} in fleet.toml eingetragen")
    elif node is None and ble:
        known = next((n for n in fleet.nodes if n.ble_mac == ble), None)
        if known:
            ok(f"Das ist {known.id}")
        else:
            warn(f"Unbekannter ESP32 (BLE-MAC {ble}) – als Gerät anlegen, sonst meldet er sich als "
                 f"esp32_{ble.replace(':', '')[-6:]}")
    try:
        cmd_esp_provision(fleet)
    except DeployError as error:
        warn(f"Provisionierung übersprungen: {error}")
    ok("Geflasht. Der ESP32 startet neu, holt sich seine ID per MQTT und ist danach per WLAN aktualisierbar.")


def ota_passwords(fleet: Fleet) -> List[str]:
    """Aktuelles OTA-Passwort zuerst, danach frühere (noch nicht aktualisierte ESP32 kennen nur diese)."""
    current = str(fleet.esp32.get("ota_password", "") or "")
    previous = fleet.esp32.get("ota_password_previous") or []
    if isinstance(previous, str):
        previous = [previous]
    out = [current]
    for item in previous:
        if isinstance(item, str) and item and item not in out:
            out.append(item)
    return out


def cmd_esp_ota(fleet: Fleet, names):
    write_esp_secrets(fleet)
    cmd_esp_provision(fleet)
    identities = read_identities(fleet)
    by_mac = {normalize_mac(v.get("ble_mac")): v for k, v in identities.items()
              if k != "_raw" and isinstance(v, dict)}
    passwords = ota_passwords(fleet)
    secrets = fleet_secrets(fleet)
    env = dict(os.environ, BLUECAT_OTA_PASSWORD=passwords[0])
    pio = find_pio()
    run(pio + ["run", "-d", ESP_DIR, "-e", "ota"], env=env)
    failures = []
    for node in fleet.select("esp32", names):
        ident = identities.get(node.id) or by_mac.get(node.ble_mac) or {}
        if ident.get("version") and version_tuple(ident.get("version")) < (2, 1):
            warn(f"{node.id}: läuft noch mit Firmware {ident.get('version')} ohne WLAN-Update – "
                 "einmal per USB flashen (übersprungen)")
            continue
        host = node.host or ident.get("ip") or f"bluecat-{node.id}.local"
        info(f"{node.id}: OTA an {host} ...")
        cmd = pio + ["run", "-d", ESP_DIR, "-e", "ota", "-t", "upload", "--upload-port", host]
        done, reason = False, ""
        for index, password in enumerate(passwords):
            if index:
                info(f"{node.id}: neues OTA-Passwort abgelehnt – versuche das frühere ({index}) ...")
            code, output = run_redacted(cmd, secrets, env=dict(env, BLUECAT_OTA_PASSWORD=password))
            if code == 0:
                done = True
                break
            if "Authentication Failed" in output or "Authentication failed" in output:
                reason = "auth"
                continue
            reason = "noresponse" if "No response from the ESP" in output else "other"
            break
        if done:
            ok(f"{node.id}: aktualisiert")
        elif reason == "auth":
            fail(f"{node.id}: OTA-Passwort abgelehnt – der ESP32 hat ein anderes Passwort (einmal per USB flashen)")
            failures.append(node.id)
        elif reason == "noresponse":
            seen = f" (zuletzt gemeldet: {ident.get('ip')})" if ident.get("ip") and ident.get("ip") != host else ""
            fail(f"{node.id}: keine Antwort von {host}{seen} – ist der ESP32 online und die IP aktuell? "
                 "Firmware ≥ 2.1? Sonst einmal per USB flashen.")
            failures.append(node.id)
        else:
            fail(f"{node.id}: OTA fehlgeschlagen – läuft schon Firmware ≥ 2.1 (sonst einmal per USB flashen)?")
            failures.append(node.id)
    if failures:
        raise DeployError("ESP32-OTA unvollständig: " + ", ".join(failures))


# ---------------------------------------------------------------------------
# Status & Aufräumen
# ---------------------------------------------------------------------------
def cmd_status(fleet: Fleet):
    data = read_identities(fleet)
    raw = data.pop("_raw")
    tracker_status = raw.get("bluecat/trilola/status", "?")
    engine = raw.get("bluecat/config/tracker/engine/state", "?")
    print(f"\nTracker: {tracker_status}  (Modell {engine})\n")
    header = f"{'Knoten':26s} {'Typ':7s} {'Status':8s} {'IP':15s} {'Version':9s} {'Lola':6s} Hinweis"
    print(header)
    print("-" * len(header))
    listed = set()
    for node in fleet.nodes:
        ident = data.get(node.id, {})
        listed.add(node.id)
        status = raw.get(f"bluecat/{node.id}/sensor/status", "-")
        state_raw = raw.get(f"bluecat/{node.id}/sensor/state", "")
        lola = "-"
        if state_raw:
            try:
                lola = "sieht" if json.loads(state_raw).get("present") else "nein"
            except ValueError:
                pass
        hint = ""
        if not ident:
            hint = "noch keine Identity (alte Firmware oder offline)"
        elif version_tuple(ident.get("version")) < (2, 1):
            hint = "Firmware aktualisieren"
        if node.type == "pi" and node.tracker:
            hint = (hint + "; " if hint else "") + "Tracker"
        print(f"{node.id:26s} {node.type:7s} {status:8s} {str(ident.get('ip', node.host or '-')):15s} "
              f"{str(ident.get('version', '-')):9s} {lola:6s} {hint}")
    unknown = [k for k in data if k not in listed]
    for sid in unknown:
        ident = data[sid]
        hint = "nicht in fleet.toml"
        if sid.startswith("esp32_"):
            hint = f"unprovisionierter ESP32 – ble_mac = \"{ident.get('ble_mac')}\" in fleet.toml eintragen"
        print(f"{sid:26s} {str(ident.get('implementation', '?'))[:7]:7s} "
              f"{raw.get(f'bluecat/{sid}/sensor/status', '-'):8s} {str(ident.get('ip', '-')):15s} "
              f"{str(ident.get('version', '-')):9s} {'':6s} {hint}")


def version_tuple(value):
    parts = re.findall(r"\d+", str(value or ""))
    return tuple(int(p) for p in parts[:3]) if parts else (0,)


KNOWN_SENSOR_SUFFIXES = (
    "rssi", "presence", "scan_mode", "ble_mac", "enabled", "position_x", "position_y",
    "calibration_tx_power", "calibration_n_factor", "calibration_sigma_db", "calibration_r_min",
    "calibration_r_max", "calibration_q_variance", "calibration_rssi_limit", "calibration_detection_floor",
)


def _confirm(question, yes, dry_run=False):
    if yes:
        return True
    if dry_run or not interactive():
        print(f"{question} → Vorschau, nichts geändert")
        return False
    return input(question + " (j/N): ").strip().lower() in {"j", "ja", "y", "yes"}


def cmd_ha_cleanup(fleet: Fleet, yes=False, extra_ids=(), dry_run=False):
    fleet_ids = {n.id for n in fleet.nodes}
    # 1) Sensoren, die der Tracker kennt, die aber nicht (mehr) in fleet.toml stehen
    tracker_state = mqtt_collect(fleet, ["bluecat/config/sensors/+/enabled/state"], seconds=2.0)
    tracker_ids = {t.split("/")[3] for t, p in tracker_state.items() if p}
    orphans = sorted(tracker_ids - fleet_ids - set(extra_ids))
    if orphans:
        print("Sensoren im Tracker, aber nicht in fleet.toml:")
        for sid in orphans:
            print(f"  {sid}")
        if _confirm("Aus dem Tracker entfernen (Konfiguration wird nach config/removed/ verschoben)?", yes, dry_run):
            mqtt_publish_many(fleet, [(f"bluecat/config/sensors/{sid}/remove/set", "PRESS", False) for sid in orphans])
            ok(f"{len(orphans)} Sensor(en) beim Tracker abgemeldet")
            time.sleep(2.0)
    # Retained Identities/States verschwundener Sensoren (z. B. unprovisionierte ESP32)
    registry = mqtt_collect(fleet, ["bluecat/registry/+/identity"], seconds=1.5)
    ghost_topics = []
    for topic, payload in registry.items():
        sid = topic.split("/")[2]
        if payload and sid not in fleet_ids and sid not in set(extra_ids):
            ghost_topics += [topic, f"bluecat/{sid}/sensor/state", f"bluecat/{sid}/sensor/status"]
    if ghost_topics:
        print("Alte Sensor-Registrierungen ohne Gerät in fleet.toml:")
        for sid in sorted({t.split("/")[2] for t in ghost_topics if t.startswith("bluecat/registry/")}):
            print(f"  {sid}")
    if ghost_topics and _confirm(f"{len(ghost_topics) // 3} alte Sensor-Registrierung(en) löschen?", yes, dry_run):
        mqtt_publish_many(fleet, [(t, "", True) for t in ghost_topics])
    # 2) Discovery-Einträge ohne gültigen Eigentümer
    raw = mqtt_collect(fleet, ["homeassistant/+/+/config", "homeassistant/+/+/+/config"], seconds=3.0)
    valid_ids = {n.id.replace("-", "_") for n in fleet.nodes} | fleet_ids | set(extra_ids)
    stale = []
    for topic, payload in sorted(raw.items()):
        if not payload or "bluecat" not in topic + payload:
            continue
        try:
            unique_id = str(json.loads(payload).get("unique_id", ""))
        except ValueError:
            unique_id = ""
        if topic.count("/") == 4:  # alte, verschachtelte Tracker-Topics
            stale.append((topic, unique_id, "altes Tracker-Topic"))
            continue
        if unique_id.startswith("bluecat_trilola"):
            continue
        match = re.fullmatch(r"bluecat_(.+?)_(" + "|".join(KNOWN_SENSOR_SUFFIXES) + ")", unique_id)
        if match and match.group(1) in valid_ids:
            continue
        stale.append((topic, unique_id, "unbekannter Sensor" if match else "unbekanntes Schema"))
    if not stale:
        ok("Keine verwaisten Bluecat-Einträge gefunden.")
        return
    print(f"{len(stale)} verwaiste Discovery-Einträge:")
    for topic, unique_id, why in stale:
        print(f"  {topic}  ({unique_id or '-'}; {why})")
    if not _confirm("Entfernen?", yes, dry_run):
        return
    mqtt_publish_many(fleet, [(topic, "", True) for topic, _, _ in stale])
    ok("Entfernt. Home Assistant löscht die Entities sofort; leere Geräte ggf. in HA manuell entfernen.")


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------
def cmd_remove(fleet: Fleet, node_id: str, keep_config=False, skip_device=False):
    """Knoten abmelden: Gerät stilllegen, Tracker/HA aufräumen, aus fleet.toml löschen."""
    node = fleet.node(node_id)
    if node.type == "pi" and node.tracker:
        raise DeployError(f"{node.id} betreibt den Tracker – zuerst den Tracker einem anderen Pi zuweisen")
    info(f"Entferne {node.id} ({node.type})")
    if not skip_device:
        try:
            if node.type == "pi":
                sudo_pw = os.environ.get("BLUECAT_SUDO_PASSWORD")
                sudo = "sudo -S -p ''" if sudo_pw is not None else "sudo -n"
                ssh_run(node, f"{sudo} systemctl disable --now bluecat-sensor.service", check=True,
                        input_text=(sudo_pw + "\n") if sudo_pw is not None else None)
                ok(f"{node.id}: Sensor-Dienst gestoppt und deaktiviert")
            elif node.type == "shelly":
                host = resolve_shelly_host(fleet, node)
                rpc = ShellyRPC(host, fleet.shelly.get("user", "admin"), fleet.shelly.get("password", ""))
                for script in (rpc.call("Script.List") or {}).get("scripts", []):
                    if _script_is_ours(rpc, script):
                        if script.get("running"):
                            rpc.call("Script.Stop", {"id": script["id"]})
                        rpc.call("Script.SetConfig", {"id": script["id"], "config": {"enable": False}})
                ok(f"{node.id}: Bluecat-Skript gestoppt und deaktiviert")
        except DeployError as error:
            warn(f"Gerät nicht erreichbar, nur abgemeldet: {error}")
    ids = {node.id} | ({node.legacy_id} if node.legacy_id else set())
    try:
        items = [(f"bluecat/config/sensors/{node.id}/remove/set", "PRESS", False)]
        if node.type == "esp32" and node.ble_mac:
            key = node.ble_mac.replace(":", "")
            items.append((f"bluecat/provision/{key}",
                          json.dumps({"sensor_id": node.id, "enabled": False}), True))
        mqtt_publish_many(fleet, items)
        time.sleep(1.5)
        raw = mqtt_collect(fleet, ["homeassistant/+/+/config", "bluecat/registry/+/identity"], seconds=2.0)
        clear = []
        for sid in ids:
            clear += [f"bluecat/registry/{sid}/identity", f"bluecat/{sid}/sensor/state",
                      f"bluecat/{sid}/sensor/status", f"bluecat/{sid}/switch/scan_mode/state"]
        for topic, payload in raw.items():
            if not payload or not topic.startswith("homeassistant/"):
                continue
            try:
                unique_id = str(json.loads(payload).get("unique_id", ""))
            except ValueError:
                continue
            if any(unique_id.startswith(f"bluecat_{sid}_") for sid in ids):
                clear.append(topic)
        mqtt_publish_many(fleet, [(topic, "", True) for topic in clear])
        ok(f"Beim Tracker abgemeldet, {len(clear)} MQTT/HA-Einträge gelöscht")
        if node.type == "esp32":
            info("Der ESP32 bleibt im Standby (keine Messungen, OTA weiter möglich), bis er neu angelegt wird.")
    except DeployError as error:
        warn(f"MQTT-Aufräumen übersprungen: {error}")
    if not keep_config:
        data = read_fleet_data(fleet.path)
        data["node"] = [n for n in data.get("node", []) if n.get("id") != node.id]
        write_fleet_data(fleet.path, data)
        ok(f"{node.id} aus fleet.toml entfernt")


# ---------------------------------------------------------------------------
# Tracker-Konfiguration zwischen Pi und Add-on umziehen
# ---------------------------------------------------------------------------
TRACKER_REMOTE_CONFIG = "~/bluecat/tracker/config"
CONFIG_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+\.json")


def _pack_json_dir(directory: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in sorted(os.listdir(directory)) if os.path.isdir(directory) else []:
            path = os.path.join(directory, name)
            if os.path.isfile(path) and CONFIG_NAME_RE.fullmatch(name):
                tar.add(path, arcname=name)
    return buf.getvalue()


def _unpack_json_tar(data: bytes, directory: str) -> List[str]:
    """Nur flache *.json-Dateien – keine Pfade, keine Links."""
    os.makedirs(directory, exist_ok=True)
    written = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            raw = member.name[2:] if member.name.startswith("./") else member.name
            name = os.path.basename(raw)
            if not member.isfile() or name != raw or not CONFIG_NAME_RE.fullmatch(name):
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            tmp = os.path.join(directory, name + ".tmp")
            with open(tmp, "wb") as out:
                out.write(handle.read())
            os.replace(tmp, os.path.join(directory, name))
            written.append(name)
    return written


# ---------------------------------------------------------------------------
# Tracker-Stand über MQTT sichern/wiederherstellen (ohne SSH – z. B. wenn die SD-Karte defekt ist)
# ---------------------------------------------------------------------------
BACKUP_DIR = os.path.join(DATA_DIR, "backup")
BACKUP_TOPICS = ["bluecat/config/#", "bluecat/registry/+/identity"]
RESTORE_PARTS = ("floorplan", "georef", "tuning", "positions", "calibration")
_SENSOR_STATE_RE = re.compile(r"^bluecat/config/sensors/([A-Za-z0-9_-]+)/(position|calibration)/state$")


def _backup_summary(retained: Dict[str, str]) -> str:
    def load(topic):
        try:
            return json.loads(retained.get(topic) or "null")
        except ValueError:
            return None
    fp = load("bluecat/config/floorplan/state") or {}
    tun = load("bluecat/config/tracker/tuning/state") or {}
    sensors = {m.group(1) for m in map(_SENSOR_STATE_RE.match, retained) if m}
    overrides = [p["key"] for p in tun.get("params", []) if isinstance(p, dict) and p.get("overridden")]
    geo = load("bluecat/config/tracker/georef/state") or {}
    return (f"{len(sensors)} Geräte, Grundriss {len(fp.get('walls') or [])} Wände / {len(fp.get('rooms') or [])} Räume, "
            f"Kartenbezug {'ja' if geo.get('lat') is not None else 'nein'}, {len(overrides)} geänderte Feintuning-Werte")


def cmd_tracker_backup(fleet: Fleet, out: Optional[str] = None) -> str:
    """Liest den gespeicherten (retained) Stand des Trackers vom Broker – funktioniert auch, wenn der Tracker weg ist."""
    retained = mqtt_collect(fleet, BACKUP_TOPICS, seconds=4.0)
    retained = {t: p for t, p in retained.items() if p != ""}
    if not any(t.startswith("bluecat/config/") for t in retained):
        raise DeployError("Auf dem Broker liegt kein gespeicherter Tracker-Stand (bluecat/config/…)")
    path = out or os.path.join(BACKUP_DIR, f"tracker_{time.strftime('%Y%m%d-%H%M%S')}.json")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "retained": retained},
                  handle, indent=1, ensure_ascii=False)
    ok(f"Gesichert: {path}")
    info(_backup_summary(retained))
    return path


def latest_backup() -> Optional[str]:
    try:
        names = sorted(n for n in os.listdir(BACKUP_DIR) if n.startswith("tracker_") and n.endswith(".json"))
    except OSError:
        return None
    return os.path.join(BACKUP_DIR, names[-1]) if names else None


def restore_messages(retained: Dict[str, str], parts=RESTORE_PARTS):
    """Set-Befehle, die den gesicherten Stand in einen (neuen) Tracker schreiben."""
    def load(topic):
        try:
            return json.loads(retained.get(topic) or "null")
        except ValueError:
            return None
    items = []
    if "floorplan" in parts:
        fp = load("bluecat/config/floorplan/state")
        if isinstance(fp, dict) and (fp.get("walls") or fp.get("rooms") or fp.get("floor_elevation_cm") is not None):
            items.append(("bluecat/config/floorplan/set", json.dumps(fp), False))
    if "georef" in parts:
        geo = load("bluecat/config/tracker/georef/state")
        if isinstance(geo, dict) and geo.get("lat") is not None and geo.get("lon") is not None:
            keep = {k: geo[k] for k in ("lat", "lon", "bearing_deg", "reference", "reference_lat", "reference_lon")
                    if geo.get(k) is not None}
            items.append(("bluecat/config/tracker/georef/set", json.dumps(keep), False))
    if "tuning" in parts:
        tun = load("bluecat/config/tracker/tuning/state") or {}
        overrides = {p["key"]: p["value"] for p in tun.get("params", [])
                     if isinstance(p, dict) and p.get("overridden") and p.get("value") is not None}
        for key, value in sorted(overrides.items()):  # einzeln: ein inzwischen ungültiger Wert kippt nicht alle
            items.append(("bluecat/config/tracker/tuning/set", json.dumps({key: value}), False))
    for topic in sorted(retained):
        m = _SENSOR_STATE_RE.match(topic)
        if not m:
            continue
        sid, kind, data = m.group(1), m.group(2), load(topic)
        if not isinstance(data, dict):
            continue
        base = f"bluecat/config/sensors/{sid}"
        if kind == "position" and "positions" in parts and data.get("configured"):
            items.append((f"{base}/position_x/set", f"{float(data['x_cm']):.1f}", False))
            items.append((f"{base}/position_y/set", f"{float(data['y_cm']):.1f}", False))
            if data.get("height_set") and data.get("height_cm") is not None:
                items.append((f"{base}/height/set", f"{float(data['height_cm']):.1f}", False))
            if data.get("floor_cm") is not None:
                items.append((f"{base}/floor/set", f"{float(data['floor_cm']):.1f}", False))
        elif kind == "calibration" and "calibration" in parts:
            for key, value in sorted(data.items()):
                if isinstance(value, (int, float)) and re.fullmatch(r"[a-z_]+", key):
                    items.append((f"{base}/calibration_{key}/set", f"{float(value):g}", False))
    return items


def cmd_tracker_restore(fleet: Fleet, path: Optional[str] = None, parts=RESTORE_PARTS, dry_run=False):
    path = path or latest_backup()
    if not path or not os.path.exists(path):
        raise DeployError("Keine Sicherung gefunden – zuerst „Tracker-Stand sichern“")
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    retained = data.get("retained") if isinstance(data, dict) else None
    if not isinstance(retained, dict):
        raise DeployError(f"{path} ist keine Tracker-Sicherung")
    info(f"Sicherung vom {data.get('created', '?')}: {_backup_summary(retained)}")
    items = restore_messages(retained, parts)
    if not items:
        warn("Nichts wiederherzustellen")
        return
    if dry_run:
        for topic, payload, _ in items:
            print(f"  {topic} = {payload[:120]}")
        return
    status = mqtt_collect(fleet, ["bluecat/trilola/status"], seconds=2.0).get("bluecat/trilola/status")
    if status != "online":
        raise DeployError("Der Tracker ist nicht online – erst installieren/starten, dann wiederherstellen")
    mqtt_publish_many(fleet, items)
    ok(f"{len(items)} Einstellungen an den Tracker geschickt ({', '.join(parts)})")


def cmd_tracker_stop(fleet: Fleet, node_id: str):
    """Hält nur trilola.service an (Umzug) – ohne die ganze Installation, damit nichts anderes den Umzug stört."""
    node = fleet.node(node_id)
    if node.type != "pi":
        raise DeployError(f"'{node_id}' ist kein Pi")
    pw = os.environ.get("BLUECAT_SUDO_PASSWORD")
    sudo = "sudo -S -p ''" if pw is not None else "sudo -n"
    remote = ("if systemctl cat trilola.service >/dev/null 2>&1 && "
              "{ systemctl is-enabled --quiet trilola.service || systemctl is-active --quiet trilola.service; }; then "
              f"{sudo} systemctl disable --now trilola.service || exit 3; fi; "
              "for i in 1 2 3 4 5 6 7 8 9 10; do systemctl is-active --quiet trilola.service || exit 0; sleep 1; done; exit 4")
    result = ssh_run(node, remote, check=False, input_text=(pw + "\n") if pw is not None else None)
    if result.returncode == 3:
        raise DeployError(f"{node.id}: Tracker ließ sich nicht anhalten – sudo-Passwort nötig?")
    if result.returncode == 4:
        raise DeployError(f"{node.id}: Tracker läuft noch")
    if result.returncode != 0:
        raise DeployError(f"{node.id}: Anhalten fehlgeschlagen ({result.returncode})")
    ok(f"{node.id}: Tracker angehalten (Dienst deaktiviert, Konfiguration bleibt)")


def cmd_tracker_config(fleet: Fleet, action: str, node_id: str, local_dir: str):
    node = fleet.node(node_id)
    if node.type != "pi":
        raise DeployError(f"'{node_id}' ist kein Pi")
    local_dir = os.path.abspath(local_dir)
    if action == "pull":
        cmd = ["ssh"] + _ssh_opts(node, batch=not interactive()) + [
            _ssh_target(node),
            f"cd {TRACKER_REMOTE_CONFIG} 2>/dev/null && tar czf - --no-recursion $(ls *.json 2>/dev/null) || true"]
        result = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL)
        if result.returncode == 255:
            raise DeployError(f"{node.id}: SSH-Anmeldung fehlgeschlagen – zuerst „SSH-Zugang einrichten“")
        if not result.stdout:
            warn(f"{node.id}: keine Tracker-Konfiguration gefunden – der Tracker startet frisch")
            return
        fresh = local_dir + ".new"
        shutil.rmtree(fresh, ignore_errors=True)
        names = _unpack_json_tar(result.stdout, fresh)
        if os.path.isdir(local_dir):  # alten Stand vollständig ersetzen (keine Reste), aber sichern
            target = f"{local_dir}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
            os.replace(local_dir, target)
            info(f"Bisherige Konfiguration gesichert: {target}")
        os.replace(fresh, local_dir)
        ok(f"{len(names)} Dateien von {node.id} übernommen ({', '.join(names[:6])}{' …' if len(names) > 6 else ''})")
    elif action == "push":
        data = _pack_json_dir(local_dir)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            count = len(tar.getnames())
        if not count:
            warn("Keine Tracker-Konfiguration zum Übertragen – der Tracker startet frisch")
            return
        with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as tmp:
            tmp.write(data)
            local = tmp.name
        try:
            scp_upload(node, local, "/tmp/trilola_config.tgz")
        finally:
            os.unlink(local)
        remote = (f"set -e; C={TRACKER_REMOTE_CONFIG}; mkdir -p $C; "
                  "if ls $C/*.json >/dev/null 2>&1; then cp -a $C $C.bak-$(date +%Y%m%d-%H%M%S); fi; "
                  "tar xzf /tmp/trilola_config.tgz -C $C; rm -f /tmp/trilola_config.tgz")
        ssh_run(node, remote)
        ok(f"{count} Dateien nach {node.id} übertragen")
    else:
        raise DeployError(f"unbekannte Aktion {action!r}")


# ---------------------------------------------------------------------------
# Home-Assistant-App (Add-on) per SSH installieren
# ---------------------------------------------------------------------------
ADDON_EXCLUDE_DIRS = {"__pycache__", "config", "recordings", "tests", "plan", "ha_addon", "removed", "fixtures",
                      "secrets", "devcontainer", "logo", "backup"}  # dazu alle versteckten Ordner (.pio, .venv, .tile_cache, .tracker_move …)
ADDON_EXCLUDE_FILES = {"fleet.toml", ".cache.json", ".gui_token", "secrets_tri.py", "secrets_blue.py",
                       "secrets_ble.h", "error.txt"}


def _addon_version(base: str) -> str:
    return f"{base}-{time.strftime('%Y%m%d%H%M%S')}"  # jede Installation ist ein Update für HA


def build_addon_bundle(version: str) -> bytes:
    """Build-Kontext der App: ha_addon/* + Programmcode (ohne persönliche Daten)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add_bytes(name, data, mode=0o644):
            ti = tarfile.TarInfo(f"{ADDON_SLUG}/{name}")
            ti.size = len(data)
            ti.mode = mode
            ti.mtime = int(time.time())
            tar.addfile(ti, io.BytesIO(data))

        for name in sorted(os.listdir(ADDON_SRC_DIR)):
            path = os.path.join(ADDON_SRC_DIR, name)
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as handle:
                data = handle.read()
            if name == "config.yaml":
                data = re.sub(rb'(?m)^version:.*$', f'version: "{version}"'.encode(), data)
            if name.endswith((".sh", ".yaml", ".md")) or name == "Dockerfile":
                data = data.replace(b"\r\n", b"\n")
            add_bytes(name, data, 0o755 if name.endswith(".sh") else 0o644)
        for top in ("deploy", "bt_tracker", "bt_sensor"):
            for root, dirs, files in os.walk(os.path.join(REPO, top)):
                dirs[:] = sorted(d for d in dirs if d not in ADDON_EXCLUDE_DIRS and not d.startswith((".", "fleet"))
                                 and ".bak-" not in d)
                for name in sorted(files):
                    if name in ADDON_EXCLUDE_FILES or name.endswith((".pyc", ".bak", ".tmp")) or \
                            name.startswith(("fleet.toml", ".")):  # versteckte Dateien: Token, known_hosts, Cache
                        continue
                    if name.endswith(".txt") and not name.startswith("requirements"):
                        continue  # alte Logs/Notizen
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, REPO).replace(os.sep, "/")
                    with open(full, "rb") as handle:
                        data = handle.read()
                    if name.endswith((".sh", ".py")):
                        data = data.replace(b"\r\n", b"\n")
                    add_bytes(f"app/{rel}", data, 0o755 if name.endswith(".sh") else 0o644)
        add_bytes("app/VERSION", (version + "\n").encode())  # die laufende App zeigt ihre Version an
    return buf.getvalue()


class HaShell:
    """SSH zur Home-Assistant-App „Terminal & SSH“ bzw. „Advanced SSH & Web Terminal“."""

    def __init__(self, host, port=22, user="root", password=None):
        try:
            import paramiko  # type: ignore
        except ModuleNotFoundError:
            raise DeployError("Für die Installation auf Home Assistant: pip install paramiko")
        self.client = paramiko.SSHClient()
        # Beim ersten Mal merken, danach prüfen (schützt Passwort und fleet.toml vor Umleitung im Netz)
        known = os.path.join(DATA_DIR, ".ha_known_hosts")
        if os.path.exists(known):
            self.client.load_host_keys(known)
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._known = known
        key = os.path.expanduser("~/.ssh/id_ed25519")
        try:
            self.client.connect(host, port=int(port), username=user, password=password or None,
                                key_filename=key if (not password and os.path.exists(key)) else None,
                                look_for_keys=not password, allow_agent=not password,
                                timeout=10, auth_timeout=20)
        except paramiko.BadHostKeyException:
            raise DeployError(f"Der SSH-Schlüssel von {host}:{port} hat sich geändert – abgebrochen. War das "
                              f"eine Neuinstallation der SSH-App, die Datei {known} löschen und erneut versuchen.")
        except paramiko.AuthenticationException:
            raise DeployError(f"Anmeldung an {user}@{host}:{port} fehlgeschlagen – Passwort bzw. Schlüssel "
                              "in der SSH-App von Home Assistant eingetragen?")
        except (OSError, paramiko.SSHException) as error:
            raise DeployError(f"{host}:{port} nicht erreichbar ({error}) – läuft die App „Terminal & SSH“ "
                              "und ist der Port freigegeben?")
        try:
            self.client.save_host_keys(known)
        except OSError:
            pass

    def run(self, command, data: Optional[bytes] = None, check=True, timeout=900):
        stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        if data is not None:
            for i in range(0, len(data), 256 * 1024):
                stdin.write(data[i:i + 256 * 1024])
            stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        if check and code != 0:
            raise DeployError(f"Befehl auf Home Assistant fehlgeschlagen ({code}): {(err or out).strip()[-400:]}")
        return code, out, err

    def close(self):
        self.client.close()


def _read_addon_version() -> str:
    with open(os.path.join(ADDON_SRC_DIR, "config.yaml"), encoding="utf-8") as handle:
        match = re.search(r'(?m)^version:\s*"?([^"\n]+)"?', handle.read())
    return match.group(1).strip() if match else "1.0.0"


def installed_app_version() -> Dict[str, str]:
    """In der App: Version und Installationszeit aus app/VERSION („2.6.0-20260929183012“)."""
    try:
        with open(os.path.join(REPO, "VERSION"), encoding="utf-8") as handle:
            full = handle.read().strip()
    except OSError:
        return {}
    base, _, stamp = full.partition("-")
    built = ""
    if re.fullmatch(r"\d{14}", stamp):
        built = f"{stamp[6:8]}.{stamp[4:6]}.{stamp[0:4]} {stamp[8:10]}:{stamp[10:12]}"
    return {"version": base, "full": full, "installed": built}


def _addon_ssh_key() -> Tuple[str, str]:
    """Eigener Schlüssel der App für die Pis (nicht der des PCs)."""
    key_dir = tempfile.mkdtemp(prefix="trilola_key_")
    key = os.path.join(key_dir, "id_ed25519")
    run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key, "-C", "trilola-homeassistant"],
        stdin=subprocess.DEVNULL)
    with open(key, "r", encoding="utf-8") as handle:
        private = handle.read()
    with open(key + ".pub", "r", encoding="utf-8") as handle:
        public = handle.read().strip()
    shutil.rmtree(key_dir, ignore_errors=True)
    return private, public


def stage_addon():
    """Programmcode nach deploy/ha_addon/app legen – dann ist deploy/ha_addon selbst ein baubarer
    App-Ordner (für den Home-Assistant-Devcontainer: VS Code → „Reopen in Container“)."""
    data = build_addon_bundle(_read_addon_version())
    tmp = tempfile.mkdtemp(prefix="trilola_stage_")
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(tmp, filter="data") if hasattr(tarfile, "data_filter") else tar.extractall(tmp)
        target = os.path.join(ADDON_SRC_DIR, "app")
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(os.path.join(tmp, ADDON_SLUG, "app"), target)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ok(f"Programmcode nach {os.path.relpath(target, REPO)} gelegt – App „TriLola“ ist baubar")


def export_addon(target: str):
    """App-Ordner lokal ablegen – für die Installation per Samba-Freigabe „addons“."""
    data = build_addon_bundle(_addon_version(_read_addon_version()))
    os.makedirs(target, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(target, filter="data") if hasattr(tarfile, "data_filter") else tar.extractall(target)
    ok(f"App-Ordner geschrieben: {os.path.join(os.path.abspath(target), ADDON_SLUG)} – in die Samba-Freigabe "
       "„addons“ kopieren, dann in HA: App-Store → ⋮ → „Nach Updates suchen“ → „Lokale Apps“ → TriLola")


def ha_install_script(start=True, version: str = "") -> str:
    """Shell-Skript für die SSH-App: TriLola installieren, aktualisieren oder neu bauen.

    Läuft in sh, bash *und* zsh (die App „Terminal & SSH“ nutzt zsh – dort wird ``$H "$@"`` nicht in
    Wörter zerlegt, deshalb keine Befehle in Variablen). Neue CLIs heißen ``ha apps``, ältere ``ha addons``.

    Mit ``version`` wartet es, bis der App-Store genau diese Version kennt, bevor es aktualisiert – sonst
    baute es mit der alten Versionsnummer neu, und Home Assistant bot danach ein Update an, das die
    Supervisor-Seite als „No update available“ ablehnt. Danach prüft es die installierte Version und stößt
    die Update-Anzeige in Home Assistant an.
    """
    if version and not re.fullmatch(r"[0-9A-Za-z.+-]{1,64}", version):
        raise DeployError(f"ungültige Versionsnummer {version!r}")
    lines = [
        "command -v ha >/dev/null || exit 90",
        "if ha apps --help >/dev/null 2>&1; then H=apps; else H=addons; fi",
        'hx() { if [ "$H" = apps ]; then ha apps "$@"; else ha addons "$@"; fi; }',
        # der erste Store-Reload lädt alle App-Stores und dauert oft länger, als „ha“ wartet
        "for i in 1 2 3 4; do (ha store reload || ha supervisor reload) >/dev/null 2>&1 && break; sleep 10; done",
        "info=$(hx info local_trilola --raw-json 2>/dev/null)",
    ]
    if version:
        lines += [
            f"V='{version}'",
            'n=0; while [ "$n" -lt 12 ]; do',
            r'''  printf '%s' "$info" | grep -q "\"version_latest\": *\"$V\"" && break''',
            "  n=$((n + 1)); sleep 5; (ha store reload || ha supervisor reload) >/dev/null 2>&1",
            "  info=$(hx info local_trilola --raw-json 2>/dev/null)",
            "done",
            r'''printf '%s' "$info" | grep -q "\"version_latest\": *\"$V\"" || '''
            "echo '» Hinweis: Der App-Store zeigt die neue Version noch nicht – baue trotzdem neu.'",
        ]
    lines += [
        "if printf '%s' \"$info\" | grep -q '\"version\": *\"[0-9]'; then",
        "  if printf '%s' \"$info\" | grep -q '\"update_available\": *true'; then",
        "    echo '» Aktualisiere (Bauen dauert einige Minuten) ...'; hx update local_trilola || exit 91",
        "  else",
        "    echo '» Baue neu (Bauen dauert einige Minuten) ...'",
        "    hx rebuild local_trilola || hx rebuild --force local_trilola || exit 91",
        "  fi",
        "else",
        "  echo '» Installiere (Bauen dauert beim ersten Mal 5–15 Minuten) ...'; hx install local_trilola || exit 91",
        "fi",
    ]
    if start:
        lines.append("hx restart local_trilola 2>/dev/null || hx start local_trilola")
    if version:
        lines += [
            "info=$(hx info local_trilola --raw-json 2>/dev/null)",
            r'''if printf '%s' "$info" | grep -q "\"version\": *\"$V\""; then echo "» Installiert: $V"; '''
            'else echo "» Achtung: installiert ist nicht $V – Einstellungen → Apps → TriLola prüfen"; fi',
        ]
    # Update-Anzeige in Home Assistant sofort auffrischen (sonst bis zu einigen Minuten veraltet); nur, wenn die
    # SSH-App Zugriff auf die HA-API hat – sonst still überspringen.
    lines += [
        'if [ -n "${SUPERVISOR_TOKEN:-}" ] && command -v curl >/dev/null 2>&1; then',
        '  for e in $(curl -s -m 10 -H "Authorization: Bearer $SUPERVISOR_TOKEN" http://supervisor/core/api/states'
        r''' | grep -o '"entity_id": *"update\.[a-z0-9_]*trilola[a-z0-9_]*"' | grep -o 'update\.[a-z0-9_]*'); do''',
        '    curl -s -m 10 -o /dev/null -X POST -H "Authorization: Bearer $SUPERVISOR_TOKEN"'
        ' -H "Content-Type: application/json" -d "{\\"entity_id\\": \\"$e\\"}"'
        ' http://supervisor/core/api/services/homeassistant/update_entity',
        "  done",
        "fi",
        "exit 0",
    ]
    return "\n".join(lines) + "\n"


def cmd_ha_addon(fleet: Fleet, host, port=22, user="root", migrate=False, start=True):
    password = os.environ.get("BLUECAT_HA_PASSWORD") or None
    base = _read_addon_version()
    version = _addon_version(base)
    info(f"Baue App-Paket TriLola {version} ...")
    bundle = build_addon_bundle(version)
    info(f"Paket {len(bundle) // 1024} KB – verbinde mit {user}@{host}:{port} ...")
    sh = HaShell(host, port, user, password)
    try:
        code, _, _ = sh.run("test -d /addons && test -w /addons", check=False)
        if code != 0:
            raise DeployError("Kein Schreibzugriff auf /addons – bitte die App „Terminal & SSH“ oder "
                              "„Advanced SSH & Web Terminal“ verwenden (nicht den SSH-Zugang des Hosts).")
        sh.run("cat > /tmp/trilola_app.tgz", data=bundle)
        # erst vollständig entpacken (außerhalb von /addons), dann austauschen
        sh.run("set -e; rm -rf /tmp/trilola_new; mkdir -p /tmp/trilola_new; "
               "tar xzf /tmp/trilola_app.tgz -C /tmp/trilola_new; rm -rf /addons/trilola; "
               "cp -r /tmp/trilola_new/trilola /addons/trilola; rm -rf /tmp/trilola_new /tmp/trilola_app.tgz")
        ok("App-Dateien nach /addons/trilola kopiert")

        # --- Daten übernehmen ---------------------------------------------------------------
        cfg = "/addon_configs/local_trilola"
        code, _, _ = sh.run("test -d /addon_configs", check=False)
        if code != 0:
            warn("/addon_configs ist in der SSH-App nicht sichtbar – Daten bitte später per "
                 "Samba in addon_configs/local_trilola kopieren")
        else:
            sh.run(f"mkdir -p {cfg}/ssh {cfg}/plan && chmod 700 {cfg}/ssh")
            code, _, _ = sh.run(f"test -f {cfg}/fleet.toml", check=False)
            if code == 0 and not migrate:
                info("fleet.toml liegt schon in der App – bleibt unverändert (Häkchen „Daten übernehmen“ "
                     "überschreibt sie)")
            else:
                if code == 0:
                    sh.run(f"cp {cfg}/fleet.toml {cfg}/fleet.toml.bak")
                with open(fleet.path, "rb") as handle:
                    sh.run(f"umask 077; cat > {cfg}/fleet.toml", data=handle.read())
                if os.path.exists(CACHE_FILE):
                    with open(CACHE_FILE, "rb") as handle:
                        sh.run(f"cat > {cfg}/.cache.json", data=handle.read())
                plan_dir = os.path.join(DATA_DIR, "plan")
                copied = 0
                for name in sorted(os.listdir(plan_dir)) if os.path.isdir(plan_dir) else []:
                    path = os.path.join(plan_dir, name)
                    if os.path.isfile(path) and re.fullmatch(r"[A-Za-z0-9_.-]+", name):
                        with open(path, "rb") as handle:
                            sh.run(f"cat > {cfg}/plan/{name}", data=handle.read())
                        copied += 1
                ok(f"fleet.toml{', Cache' if os.path.exists(CACHE_FILE) else ''} und {copied} Grundriss-Dateien übernommen")
            code, _, _ = sh.run(f"test -f {cfg}/ssh/id_ed25519", check=False)
            if code != 0 or migrate:
                private, public = _addon_ssh_key() if code != 0 else (None, None)
                if private:
                    sh.run(f"umask 077; cat > {cfg}/ssh/id_ed25519", data=private.encode())
                    sh.run(f"umask 077; cat > {cfg}/ssh/id_ed25519.pub", data=(public + "\n").encode())
                else:
                    _, public, _ = sh.run(f"cat {cfg}/ssh/id_ed25519.pub")
                    public = public.strip()
                failed = []
                for node in fleet.select("pi", ["all"]):
                    try:
                        ssh_run(node, "umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; "
                                      f"grep -qxF '{public}' ~/.ssh/authorized_keys || "
                                      f"echo '{public}' >> ~/.ssh/authorized_keys")
                        ok(f"{node.id}: Schlüssel der App eingetragen")
                    except DeployError as error:
                        warn(f"{node.id}: {error} – später in der App „SSH-Zugang einrichten“")
                        failed.append(node.id)

        # --- installieren / aktualisieren ---------------------------------------------------
        script = ha_install_script(start, version)
        code, out, err = sh.run(script, check=False, timeout=3600)
        for line in (out + err).splitlines():
            if line.strip():
                info("  " + line.strip())
        if code == 90:
            warn("Die SSH-App hat keinen Zugriff auf den Befehl „ha“. Bitte in Home Assistant: Einstellungen → "
                 "Apps → App-Store → ⋮ → „Nach Updates suchen“ → „Lokale Apps“ → TriLola installieren.")
        elif code != 0:
            raise DeployError("Installation über den Befehl „ha“ fehlgeschlagen – Details oben. Alternativ in "
                              "Home Assistant: App-Store → ⋮ → „Nach Updates suchen“ → „Lokale Apps“ → TriLola.")
        else:
            ok("TriLola läuft als Home-Assistant-App. Öffnen: Einstellungen → Apps → TriLola → „In der Seitenleiste "
               "anzeigen“ einschalten → TriLola in der Seitenleiste.")
    finally:
        sh.close()


def cmd_check(fleet: Fleet):
    problems = validate_fleet(fleet)
    for n in fleet.nodes:
        extra = []
        if n.type == "pi":
            extra.append(f"{n.ssh_user}@{n.host}" + (" + Tracker" if n.tracker else ""))
        if n.type == "esp32":
            extra.append(f"BLE {n.ble_mac}")
        if n.type == "shelly":
            extra.append(n.host or f"WLAN {n.wifi_mac} → BLE {wifi_to_ble_mac(n.wifi_mac)}")
        print(f"  {n.type:7s} {n.id:28s} {', '.join(extra)}")
    if problems:
        for p in problems:
            fail(p)
        raise DeployError("fleet.toml unvollständig")
    ok(f"{len(fleet.nodes)} Knoten, keine Probleme")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bluecat/TriLola-Rollout", formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=__doc__)
    parser.add_argument("--fleet", default=DEFAULT_FLEET, help="Pfad zu fleet.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    p = sub.add_parser("ssh-setup")
    p.add_argument("nodes", nargs="*", default=["all"])
    p = sub.add_parser("import-old")
    p.add_argument("nodes", nargs="*", default=["all"])
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("pi")
    p.add_argument("nodes", nargs="*", default=["all"])
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--rollback", action="store_true")
    p = sub.add_parser("shelly")
    p.add_argument("nodes", nargs="*", default=["all"], help="'scan', 'all' oder IDs")
    p.add_argument("--fix-settings", action="store_true", help="BLE/MQTT/Eco-Mode automatisch korrigieren")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("esp")
    p.add_argument("action", choices=["secrets", "provision", "usb", "ota"])
    p.add_argument("nodes", nargs="*", default=["all"])
    p.add_argument("--port", help="serieller Port für usb, z. B. COM5")
    sub.add_parser("status")
    p = sub.add_parser("ha-cleanup")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="nur anzeigen")
    p = sub.add_parser("remove")
    p.add_argument("node")
    p.add_argument("--keep-config", action="store_true", help="in fleet.toml stehen lassen")
    p.add_argument("--skip-device", action="store_true", help="Gerät selbst nicht ansprechen")
    p = sub.add_parser("tracker-stop", help="Tracker-Dienst auf einem Pi anhalten (Umzug)")
    p.add_argument("node")
    p = sub.add_parser("tracker-config", help="Tracker-Konfiguration von/zu einem Pi kopieren")
    p.add_argument("action", choices=["pull", "push"])
    p.add_argument("node")
    p.add_argument("--dir", required=True, help="lokaler Ordner mit den *.json")
    p = sub.add_parser("tracker-backup", help="Gespeicherten Tracker-Stand vom MQTT-Broker sichern")
    p.add_argument("--out", help="Zieldatei (Standard: deploy/backup/tracker_<Zeit>.json)")
    p = sub.add_parser("tracker-restore", help="Gesicherten Stand per MQTT in den (neuen) Tracker schreiben")
    p.add_argument("file", nargs="?", help="Sicherung (Standard: die neueste)")
    p.add_argument("--only", help="Auswahl, kommagetrennt: " + ",".join(RESTORE_PARTS))
    p.add_argument("--skip", help="auslassen, kommagetrennt (z. B. calibration)")
    p.add_argument("--dry-run", action="store_true", help="nur anzeigen")
    p = sub.add_parser("ha-addon", help="TriLola als App (Add-on) auf Home Assistant installieren")
    p.add_argument("--host")
    p.add_argument("--export", help="nur den App-Ordner hierhin schreiben (zum Kopieren per Samba)")
    p.add_argument("--stage", action="store_true", help="Code nach deploy/ha_addon/app (Devcontainer)")
    p.add_argument("--port", type=int, default=22)
    p.add_argument("--user", default="root")
    p.add_argument("--migrate", action="store_true", help="fleet.toml, Grundriss & SSH-Schlüssel übernehmen")
    p.add_argument("--no-start", action="store_true")
    p = sub.add_parser("all")
    p.add_argument("--fix-settings", action="store_true")
    p.add_argument("--skip-esp", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.command == "ha-addon" and (args.export or args.stage):  # ohne fleet.toml
            if args.stage:
                stage_addon()
            if args.export:
                export_addon(args.export)
            return 0
        fleet = load_fleet(args.fleet)
        if args.command == "check":
            cmd_check(fleet)
        elif args.command == "ssh-setup":
            cmd_ssh_setup(fleet, args.nodes)
        elif args.command == "import-old":
            cmd_import_old(fleet, args.nodes, as_json=args.json)
        elif args.command == "pi":
            cmd_pi(fleet, args.nodes, dry_run=args.dry_run, rollback=args.rollback)
        elif args.command == "shelly":
            if args.nodes == ["scan"]:
                cmd_shelly_scan(fleet)
            else:
                cmd_shelly(fleet, args.nodes, fix_settings=args.fix_settings, dry_run=args.dry_run)
        elif args.command == "esp":
            if args.action == "secrets":
                write_esp_secrets(fleet)
            elif args.action == "provision":
                cmd_esp_provision(fleet)
            elif args.action == "usb":
                chosen = [n for n in args.nodes if n != "all"]
                if len(chosen) > 1:
                    raise DeployError("esp usb: höchstens ein ESP32 auf einmal")
                cmd_esp_usb(fleet, args.port, chosen[0] if chosen else None)
            else:
                cmd_esp_ota(fleet, args.nodes)
        elif args.command == "status":
            cmd_status(fleet)
        elif args.command == "tracker-stop":
            cmd_tracker_stop(fleet, args.node)
        elif args.command == "tracker-config":
            cmd_tracker_config(fleet, args.action, args.node, args.dir)
        elif args.command == "tracker-backup":
            cmd_tracker_backup(fleet, args.out)
        elif args.command == "tracker-restore":
            parts = [p for p in (args.only.split(",") if args.only else RESTORE_PARTS) if p]
            skip = set(args.skip.split(",")) if args.skip else set()
            unknown = set(parts) - set(RESTORE_PARTS) | (skip - set(RESTORE_PARTS))
            if unknown:
                raise DeployError(f"Unbekannt: {', '.join(sorted(unknown))} (möglich: {', '.join(RESTORE_PARTS)})")
            cmd_tracker_restore(fleet, args.file, tuple(p for p in parts if p not in skip), dry_run=args.dry_run)
        elif args.command == "ha-addon":
            if not args.host:
                raise DeployError("--host (IP von Home Assistant) oder --export ORDNER angeben")
            else:
                cmd_ha_addon(fleet, args.host, args.port, args.user, migrate=args.migrate,
                             start=not args.no_start)
        elif args.command == "ha-cleanup":
            cmd_ha_cleanup(fleet, yes=args.yes, dry_run=args.dry_run)
        elif args.command == "remove":
            cmd_remove(fleet, args.node, keep_config=args.keep_config, skip_device=args.skip_device)
        elif args.command == "all":
            cmd_check(fleet)
            errors = []
            steps = [("Pis", lambda: cmd_pi(fleet, ["all"])),
                     ("Shellys", lambda: cmd_shelly(fleet, ["all"], fix_settings=args.fix_settings))]
            if not args.skip_esp:
                steps.append(("ESP32", lambda: cmd_esp_ota(fleet, ["all"])))
            for label, step in steps:
                info(f"=== {label} ===")
                try:
                    step()
                except DeployError as error:
                    fail(str(error))
                    errors.append(label)
            info("=== Status ===")
            time.sleep(5)
            cmd_status(fleet)
            if errors:
                raise DeployError("Nicht alles erfolgreich: " + ", ".join(errors))
    except DeployError as error:
        fail(str(error))
        return 1
    except KeyboardInterrupt:
        fail("abgebrochen")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
