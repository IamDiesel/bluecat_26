#!/usr/bin/env python3
"""Bluecat/TriLola – Rollout-Oberfläche.

Startet einen kleinen Webserver nur auf diesem PC (127.0.0.1) und öffnet die
Oberfläche im Browser. Alle Aktionen laufen über deploy/bluecat_deploy.py –
die Oberfläche ist nur eine bequeme Bedienung davon.

    python deploy/bluecat_gui.py            (oder rollout.bat / rollout.sh)
    python deploy/bluecat_gui.py --port 8765 --no-browser
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional
import math
import urllib.request
from urllib.parse import parse_qs, urlencode, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bluecat_deploy as bd  # noqa: E402

REPO = bd.REPO
DEPLOY_SCRIPT = os.path.join(HERE, "bluecat_deploy.py")
UI_FILE = os.path.join(HERE, "gui", "index.html")
VENDOR_DIR = os.path.join(HERE, "gui", "vendor")
VENDOR_FILES = {"leaflet.js": "application/javascript; charset=utf-8", "leaflet.css": "text/css; charset=utf-8"}
TILE_CACHE_DIR = os.environ.get("BLUECAT_CACHE_DIR") or os.path.join(bd.DATA_DIR, ".tile_cache")
PLAN_DIR = os.path.join(bd.DATA_DIR, "plan")  # Grundriss-Bild (privat, nicht im Git)
# Home Assistant Ingress: Anfragen kommen nur über den Supervisor (Anmeldung macht HA)
# Nur der Supervisor – nicht localhost: mit Host-Netz teilt die App sich localhost mit HA und anderen Apps.
INGRESS_PEERS = set((os.environ.get("BLUECAT_INGRESS_PEERS") or "172.30.32.2").split(","))
PLAN_IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}
PLAN_IMAGE_MAX_BYTES = 20 * 1024 * 1024
CALIBRATION_COMMANDS = {"start", "cancel", "delete", "clear", "fit", "apply_fit", "autocal", "apply_autocal"}
TILE_SOURCES = {
    # Kartenkacheln werden über den lokalen Server geladen und zwischengespeichert (Nutzungsregeln:
    # erkennbarer User-Agent, sparsamer Abruf). Zoom > 19 skaliert der Browser hoch.
    "osm": ("https://tile.openstreetmap.org/{z}/{x}/{y}.png", 19),
    "sat": ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", 19),
}
TILE_MAX_AGE_SEC = 30 * 24 * 3600
USER_AGENT = "TriLola-Rollout/2.1 (privater BLE-Innenraum-Tracker; lokale Einrichtungsoberflaeche)"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
MAX_LOG_LINES = 20000

# Meldungen, die das Speichern nicht verhindern (Einstellungen noch unvollständig)
SOFT_PROBLEMS = ("[wifi]", "[esp32]", "[mqtt] host")


def read_repo_versions() -> Dict[str, str]:
    def grab(path, pattern):
        try:
            with open(path, encoding="utf-8") as handle:
                match = re.search(pattern, handle.read())
            return match.group(1) if match else ""
        except OSError:
            return ""
    return {
        "esp32": grab(os.path.join(bd.ESP_DIR, "src", "main.cpp"), r'#define FW_VERSION "([^"]+)"'),
        "shelly": grab(bd.SHELLY_SCRIPT, r'SCRIPT_VERSION = "([^"]+)"'),
        "pi": grab(os.path.join(bd.PI_SENSOR_DIR, "bluecat2mqtt.py"), r'SENSOR_VERSION = "([^"]+)"'),
        "tracker": grab(os.path.join(bd.TRACKER_DIR, "network", "ha_discovery.py"), r'TRACKER_VERSION = "([^"]+)"'),
    }


PLAN_TOPICS = {
    "bluecat/config/floorplan/state": "floorplan",
    "bluecat/config/tracker/georef/state": "georef",
    "bluecat/trilola/radio_map": "radio_map",
    "bluecat/trilola/live": "live",
    "bluecat/trilola/gps/state": "gps",
    "bluecat/config/calibration/state": "calibration",
    "bluecat/config/tracker/tuning/state": "tuning",
}
TRACKER_DISCOVERY_TOPIC = "homeassistant/sensor/bluecat_trilola_gps/config"  # trägt die Tracker-Version

# ---------------------------------------------------------------------------
# Live-Status über MQTT
# ---------------------------------------------------------------------------
class LiveState:
    TOPICS = ["bluecat/registry/+/identity", "bluecat/+/sensor/status", "bluecat/+/sensor/state",
              "bluecat/trilola/status", "bluecat/trilola/room/state", "bluecat/config/tracker/engine/state",
              "bluecat/provision/+", "bluecat/config/sensors/+/position/state",
              "bluecat/config/sensors/+/enabled/state", "bluecat/config/floorplan/state",
              "bluecat/config/tracker/georef/state", "bluecat/trilola/radio_map", "bluecat/trilola/live",
              "bluecat/trilola/gps/state", "bluecat/config/calibration/state",
              "bluecat/config/tracker/tuning/state", TRACKER_DISCOVERY_TOPIC]

    def __init__(self):
        self.lock = threading.Lock()
        self.client = None
        self.config = None
        self.connected = False
        self.error = ""
        self.identities: Dict[str, dict] = {}
        self.status: Dict[str, str] = {}
        self.state: Dict[str, dict] = {}
        self.tracker = {"status": "", "room": "", "engine": ""}
        self.provision: Dict[str, dict] = {}
        self.positions: Dict[str, dict] = {}
        self.enabled: Dict[str, bool] = {}
        self.plan = {"floorplan": None, "georef": None, "radio_map": None, "live": None, "live_at": None,
                     "gps": None, "calibration": None, "tuning": None}

    def configure(self, mqtt_cfg: dict):
        cfg = (str(mqtt_cfg.get("host") or ""), int(mqtt_cfg.get("port") or 1883),
               str(mqtt_cfg.get("user") or ""), str(mqtt_cfg.get("password") or ""))
        if cfg == self.config:
            return
        self.config = cfg
        self._stop()
        with self.lock:
            self.identities.clear()
            self.status.clear()
            self.state.clear()
            self.provision.clear()
            self.positions.clear()
            self.enabled.clear()
            self.plan = {k: None for k in self.plan}
            self.tracker = {"status": "", "room": "", "engine": ""}
            self.connected = False
            self.error = ""
        if not cfg[0]:
            self.error = "kein Broker eingetragen"
            return
        try:
            import paho.mqtt.client as mqtt
        except ModuleNotFoundError:
            self.error = "paho-mqtt fehlt (pip install paho-mqtt)"
            return
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"bluecat_gui_{secrets.token_hex(3)}")
        if cfg[2]:
            client.username_pw_set(cfg[2], cfg[3] or None)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(1, 15)
        self.client = client  # vor dem Verbinden setzen: die Callbacks prüfen darauf
        try:
            client.connect_async(cfg[0], cfg[1], 30)
            client.loop_start()
        except Exception as error:  # noqa: BLE001
            self.error = str(error)
            self.client = None

    def _stop(self):
        old, self.client = self.client, None
        if old is None:
            return

        def shutdown():  # im Hintergrund: ein hängender Verbindungsversuch soll die Oberfläche nicht blockieren
            try:
                old.disconnect()
                old.loop_stop()
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=shutdown, daemon=True).start()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if client is not self.client:
            return  # alter Client nach Einstellungsänderung
        if getattr(reason_code, "is_failure", False):
            self.connected = False
            self.error = f"Broker lehnt ab: {reason_code}"
            return
        self.connected = True
        self.error = ""
        for topic in self.TOPICS:
            client.subscribe(topic)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        if client is not self.client:
            return
        self.connected = False
        if not self.error:
            self.error = "Verbindung getrennt"

    def _on_message(self, client, userdata, msg):
        if client is not None and client is not self.client:
            return
        payload = msg.payload.decode("utf-8", "replace")
        parts = msg.topic.split("/")
        now = time.time()
        with self.lock:
            if len(parts) == 4 and parts[1] == "registry" and parts[3] == "identity":
                if payload:
                    try:
                        self.identities[parts[2]] = json.loads(payload)
                    except ValueError:
                        pass
                else:
                    self.identities.pop(parts[2], None)
            elif len(parts) == 4 and parts[2] == "sensor" and parts[3] == "status":
                if payload:
                    self.status[parts[1]] = payload
                else:
                    self.status.pop(parts[1], None)
            elif len(parts) == 4 and parts[2] == "sensor" and parts[3] == "state":
                if not payload:
                    self.state.pop(parts[1], None)
                    return
                try:
                    data = json.loads(payload)
                except ValueError:
                    return
                self.state[parts[1]] = {"present": bool(data.get("present")), "rssi": data.get("rssi"),
                                        "at": None if msg.retain else now}
                if msg.retain and parts[1] in self.state:
                    self.state[parts[1]]["retained"] = True
            elif msg.topic == "bluecat/trilola/status":
                self.tracker["status"] = payload
            elif msg.topic == "bluecat/trilola/room/state":
                self.tracker["room"] = payload
            elif msg.topic == "bluecat/config/tracker/engine/state":
                self.tracker["engine"] = payload
            elif msg.topic == TRACKER_DISCOVERY_TOPIC:
                try:
                    device = (json.loads(payload) if payload else {}).get("device") or {}
                    self.tracker["version"] = str(device.get("sw_version") or "")
                except (ValueError, AttributeError):
                    pass
            elif len(parts) == 6 and parts[1] == "config" and parts[2] == "sensors" and parts[5] == "state":
                sid, field_name = parts[3], parts[4]
                if field_name == "position":
                    if payload:
                        try:
                            self.positions[sid] = json.loads(payload)
                        except ValueError:
                            pass
                    else:
                        self.positions.pop(sid, None)
                elif field_name == "enabled":
                    if payload:
                        self.enabled[sid] = payload.strip().upper() == "ON"
                    else:
                        self.enabled.pop(sid, None)
            elif msg.topic in PLAN_TOPICS:
                key = PLAN_TOPICS[msg.topic]
                try:
                    self.plan[key] = json.loads(payload) if payload else None
                except ValueError:
                    return
                if key == "live":
                    self.plan["live_at"] = now
            elif len(parts) == 3 and parts[1] == "provision":
                if payload:
                    try:
                        self.provision[parts[2]] = json.loads(payload)
                    except ValueError:
                        pass
                else:
                    self.provision.pop(parts[2], None)

    def publish(self, topic, payload, retain=False):
        client = self.client
        if client is None or not self.connected:
            raise bd.DeployError("Keine Verbindung zum MQTT-Broker – Einstellungen prüfen")
        data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        info = client.publish(topic, data, qos=1, retain=retain)
        info.wait_for_publish(5)

    def plan_snapshot(self):
        with self.lock:
            return {"positions": copy.deepcopy(self.positions), "enabled": dict(self.enabled),
                    "plan": copy.deepcopy(self.plan), "identities": copy.deepcopy(self.identities),
                    "status": dict(self.status), "state": copy.deepcopy(self.state),
                    "tracker": dict(self.tracker), "connected": self.connected, "error": self.error}

    def snapshot(self):
        with self.lock:
            return {
                "connected": self.connected, "error": self.error,
                "identities": copy.deepcopy(self.identities), "status": dict(self.status),
                "state": copy.deepcopy(self.state), "tracker": dict(self.tracker),
                "provision": copy.deepcopy(self.provision),
            }


# ---------------------------------------------------------------------------
# Jobs: jeder Job = Folge von bluecat_deploy.py-Aufrufen, nacheinander
# ---------------------------------------------------------------------------
class Job:
    def __init__(self, title, steps, node=None, kind=""):
        self.id = uuid.uuid4().hex[:10]
        self.title = title
        self.node = node
        self.kind = kind
        self.steps = steps          # [{"label", "args", "env", "parse_json"}]
        self.status = "wartet"      # wartet | läuft | ok | fehler | teilweise | abgebrochen
        self.lines: List[dict] = []
        self.created = time.time()
        self.started = None
        self.ended = None
        self.result = None
        self.proc = None
        self.cancelled = False
        self.step_results: List[bool] = []

    def log(self, text, kind=""):
        if len(self.lines) >= MAX_LOG_LINES:
            del self.lines[:1000]
        self.lines.append({"t": time.time(), "text": text, "kind": kind or classify(text)})

    def summary(self):
        return {"id": self.id, "title": self.title, "node": self.node, "kind": self.kind, "status": self.status,
                "created": self.created, "started": self.started, "ended": self.ended,
                "lines": len(self.lines), "result": self.result}


def classify(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith(("OK ", "✔")) or " läuft ✔" in stripped:
        return "ok"
    if stripped.startswith(("FEHLER", "✘")) or "FEHLER:" in stripped:
        return "error"
    if stripped.startswith(("WARNUNG", "!")) or "WARNUNG:" in stripped:
        return "warn"
    if stripped.startswith(("»", "[bluecat]", "===")):
        return "info"
    return ""


class JobRunner:
    def __init__(self, fleet_path):
        self.fleet_path = fleet_path
        self.jobs: Dict[str, Job] = {}
        self.order: List[str] = []
        self.queue: List[Job] = []
        self.cv = threading.Condition()
        threading.Thread(target=self._worker, daemon=True).start()

    def submit(self, job: Job) -> Job:
        with self.cv:
            self.jobs[job.id] = job
            self.order.append(job.id)
            if len(self.order) > 60:
                old = self.order.pop(0)
                if self.jobs[old].status not in ("wartet", "läuft"):
                    self.jobs.pop(old, None)
            self.queue.append(job)
            self.cv.notify()
        return job

    def cancel(self, job_id):
        job = self.jobs.get(job_id)
        if job is None:
            return False
        with self.cv:
            job.cancelled = True
            if job in self.queue:
                self.queue.remove(job)
                job.status = "abgebrochen"
                job.ended = time.time()
                return True
        proc = job.proc
        if proc is not None and proc.poll() is None:
            job.log("Abbruch angefordert …", "warn")
            kill_tree(proc)
        return True

    def list(self):
        return [self.jobs[j].summary() for j in reversed(self.order) if j in self.jobs]

    def busy_nodes(self):
        return {j.node for j in self.jobs.values() if j.status in ("wartet", "läuft") and j.node}

    def _worker(self):
        while True:
            with self.cv:
                while not self.queue:
                    self.cv.wait()
                job = self.queue.pop(0)
            try:
                self._run(job)
            except Exception:  # noqa: BLE001
                job.log(traceback.format_exc(), "error")
                job.status = "fehler"
                job.ended = time.time()

    def _run(self, job: Job):
        job.status = "läuft"
        job.started = time.time()
        for index, step in enumerate(job.steps):
            if job.cancelled:
                break
            if len(job.steps) > 1:
                job.log(f"=== {step['label']} ===", "head")
            ok = self._run_step(job, step)
            job.step_results.append(ok)
            if not ok and step.get("critical"):
                job.log("FEHLER Abbruch – die weiteren Schritte hängen von diesem ab.", "error")
                break
        job.ended = time.time()
        if job.cancelled:
            job.status = "abgebrochen"
        elif all(job.step_results):
            job.status = "ok"
        elif any(job.step_results):
            job.status = "teilweise"
        else:
            job.status = "fehler"
        job.log({"ok": "Fertig.", "teilweise": "Fertig, aber nicht alles erfolgreich.",
                 "fehler": "Fehlgeschlagen.", "abgebrochen": "Abgebrochen."}[job.status],
                {"ok": "ok", "abgebrochen": "warn"}.get(job.status, "error"))

    def _run_step(self, job: Job, step: dict) -> bool:
        if step.get("call"):
            try:
                step["call"](job)
                return True
            except Exception as error:  # noqa: BLE001
                job.log(f"FEHLER {error}", "error")
                return False
        if step.get("argv"):
            cmd = step["argv"]
        else:
            cmd = [sys.executable, "-u", DEPLOY_SCRIPT, "--fleet", self.fleet_path] + step["args"]
        env = dict(os.environ)
        env.update({"BLUECAT_NONINTERACTIVE": "1", "NO_COLOR": "1", "PYTHONIOENCODING": "utf-8",
                    "PYTHONUNBUFFERED": "1", "PLATFORMIO_NO_ANSI": "true"})
        env.update(step.get("env") or {})
        kwargs = {}
        if os.name == "nt":
            # eigene Prozessgruppe (Strg+C im Fenster trifft nur die Oberfläche); die Konsole wird geerbt,
            # sonst öffnet Windows für jedes ssh/scp ein eigenes Fenster
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env=env, cwd=REPO, **kwargs)
        except OSError as error:
            job.log(f"FEHLER Start fehlgeschlagen: {error}", "error")
            return False
        job.proc = proc
        try:
            secrets = bd.fleet_secrets(bd.read_fleet_data(self.fleet_path))
        except Exception:  # noqa: BLE001
            secrets = []
        secrets += [v for k, v in (step.get("env") or {}).items() if "PASSWORD" in k and isinstance(v, str) and len(v) >= 4]
        captured = []
        buffer = b""
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(1)
            if not chunk:
                break
            buffer += chunk
            while True:
                match = re.search(rb"\r\n|\n|\r", buffer)
                if not match:
                    break
                raw, buffer = buffer[:match.start()], buffer[match.end():]
                line = bd.redact(strip_ansi(raw.decode("utf-8", "replace")), secrets)
                if line.strip():
                    captured.append(line)
                    if not step.get("parse_json") or not line.lstrip().startswith("{"):
                        job.log(line)
        if buffer.strip():
            line = bd.redact(strip_ansi(buffer.decode("utf-8", "replace")), secrets)
            captured.append(line)
            if not step.get("parse_json") or not line.lstrip().startswith("{"):
                job.log(line)
        code = proc.wait()
        job.proc = None
        if step.get("parse_json"):
            for line in reversed(captured):
                if line.lstrip().startswith("{"):
                    try:
                        job.result = json.loads(line)
                    except ValueError:
                        pass
                    break
        return code == 0


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(text):
    return ANSI_RE.sub("", text)


def kill_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Tracker als Unterprozess (nur in der Home-Assistant-App, [tracker] node = "@addon")
# ---------------------------------------------------------------------------
class LocalTracker:
    RESTART_DELAYS = (5, 15, 60, 300)

    def __init__(self, home: str):
        self.home = home
        self.proc: Optional[subprocess.Popen] = None
        self.wanted = False
        self.lines: List[str] = []
        self.started_at = None
        self.last_exit = None
        self.crashes = 0
        self.lock = threading.RLock()
        self._next_start = 0.0
        threading.Thread(target=self._watchdog, daemon=True, name="tracker-watchdog").start()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _log(self, line: str):
        self.lines.append(time.strftime("%H:%M:%S ") + line.rstrip())
        del self.lines[:-300]

    def write_secrets(self, fleet: "bd.Fleet"):
        os.makedirs(os.path.join(self.home, "config"), exist_ok=True)
        path = os.path.join(self.home, "secrets_tri.py")
        fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(bd.render_tracker_secrets(fleet))
        os.replace(path + ".tmp", path)

    def start(self, fleet: "bd.Fleet"):
        with self.lock:
            self.wanted = True
            if self.running():
                return
            self._next_start = 0.0
            self.write_secrets(fleet)
            env = dict(os.environ, TRILOLA_HOME=self.home, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
            script = os.path.join(bd.TRACKER_DIR, "trilola_tracker.py")
            self.proc = subprocess.Popen([sys.executable, "-u", script], cwd=self.home, env=env,
                                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         start_new_session=True)
            self.started_at = time.time()
            self._log(f"Tracker gestartet (PID {self.proc.pid})")
            proc = self.proc

            def pump():
                assert proc.stdout is not None
                for raw in proc.stdout:
                    self._log(raw.decode("utf-8", "replace"))
            threading.Thread(target=pump, daemon=True).start()

    def stop(self, timeout=15.0):
        with self.lock:
            self.wanted = False
            proc = self.proc
            if proc is None or proc.poll() is not None:
                return
            self._log("Tracker wird beendet …")
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:  # noqa: BLE001
                proc.terminate()
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self._log("Tracker reagiert nicht – wird hart beendet")
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:  # noqa: BLE001
                proc.kill()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                raise bd.DeployError("Tracker in der App lässt sich nicht beenden")
        self._log(f"Tracker beendet ({proc.returncode})")

    def restart(self, fleet: "bd.Fleet"):
        self.stop()
        self.crashes = 0
        self.start(fleet)

    def _watchdog(self):
        while True:
            time.sleep(2.0)
            with self.lock:
                if not self.wanted or self.proc is None or self.proc.poll() is None:
                    continue
                if self._next_start == 0.0:
                    self.last_exit = self.proc.returncode
                    delay = self.RESTART_DELAYS[min(self.crashes, len(self.RESTART_DELAYS) - 1)]
                    self.crashes += 1
                    self._next_start = time.time() + delay
                    self._log(f"Tracker unerwartet beendet ({self.last_exit}) – Neustart in {delay} s")
                    continue
                if time.time() < self._next_start:
                    continue
                self._next_start = 0.0
                fleet_path = os.environ.get("BLUECAT_FLEET") or bd.DEFAULT_FLEET
                try:
                    fleet = bd.load_fleet(fleet_path)
                    if not fleet.tracker_in_addon:
                        self.wanted = False
                        self._log("Tracker läuft laut fleet.toml nicht mehr in der App – kein Neustart")
                        continue
                    self.proc = None
                    self.start(fleet)
                except Exception as error:  # noqa: BLE001
                    self._log(f"Neustart fehlgeschlagen: {error}")

    def status(self) -> dict:
        running = self.running()
        if running and self.started_at and time.time() - self.started_at > 600:
            self.crashes = 0
        return {"wanted": self.wanted, "running": running, "pid": self.proc.pid if running else None,
                "since": self.started_at if running else None, "last_exit": self.last_exit,
                "crashes": self.crashes, "log": self.lines[-60:]}


# ---------------------------------------------------------------------------
# Anwendung
# ---------------------------------------------------------------------------
class App:
    def __init__(self, fleet_path):
        self.fleet_path = os.path.abspath(fleet_path)
        self.token = load_token(os.path.join(os.path.dirname(self.fleet_path), ".gui_token"))
        self.live = LiveState()
        self.jobs = JobRunner(self.fleet_path)
        self.sudo_passwords: Dict[str, str] = {}   # nur im Speicher, nie auf Platte
        self.scan = {"running": False, "found": [], "at": None, "error": ""}
        self.fleet_lock = threading.Lock()
        self._pio_cache = (0.0, None)
        self.ingress = False
        self.addon = bd.IN_ADDON
        self.local_tracker = LocalTracker(bd.TRACKER_HOME) if self.addon else None
        self._ensure_fleet_file()
        self._apply_live_config()

    # ---- Hintergrund (nur App) -------------------------------------------
    def start_background(self):
        os.environ["BLUECAT_FLEET"] = self.fleet_path
        if self.local_tracker is None:
            return
        try:
            fleet = bd.load_fleet(self.fleet_path)
        except bd.DeployError:
            return
        if fleet.tracker_in_addon:
            self.local_tracker.start(fleet)

    def stop_background(self):
        if self.local_tracker is not None:
            self.local_tracker.stop()

    def _call_local_tracker(self, what: str):
        def call(job):
            if self.local_tracker is None:
                raise bd.DeployError("Der Tracker kann nur in der Home-Assistant-App lokal laufen")
            if what == "stop":
                self.local_tracker.stop()
                job.log("OK Tracker in der App angehalten", "ok")
                return
            fleet = bd.load_fleet(self.fleet_path)
            if not fleet.tracker_in_addon:
                raise bd.DeployError("In fleet.toml läuft der Tracker nicht in der App")
            self.local_tracker.restart(fleet)
            time.sleep(4)
            if not self.local_tracker.running():
                for line in self.local_tracker.lines[-15:]:
                    job.log(line)
                raise bd.DeployError("Tracker in der App startet nicht – Protokoll oben")
            for line in self.local_tracker.lines[-6:]:
                job.log("  " + line)
            job.log("OK Tracker läuft in der App", "ok")
        return call

    # ---- fleet.toml ----------------------------------------------------
    def _ensure_fleet_file(self):
        if os.path.exists(self.fleet_path):
            return
        data = {"mqtt": {"host": "", "port": 1883, "user": "", "password": ""},
                "wifi": {"ssid": "", "password": ""}, "esp32": {"ota_password": ""},
                "shelly": {"user": "admin", "password": "", "subnet": ""},
                "tracker": {"node": "", "target_mac": "", "engine": "pf", "origin_bearing_deg": 0.0},
                "node": []}
        with open(self.fleet_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(bd.dump_fleet_data(data))

    def rev(self):
        try:
            stat = os.stat(self.fleet_path)
            return f"{stat.st_mtime_ns}-{stat.st_size}"
        except OSError:
            return ""

    def fleet_data(self):
        data = bd.read_fleet_data(self.fleet_path)
        for section, keys, _ in bd.SETTINGS_LAYOUT:
            data.setdefault(section, {})
        data.setdefault("node", [])
        return data

    def _apply_live_config(self):
        try:
            self.live.configure(self.fleet_data().get("mqtt", {}))
        except bd.DeployError as error:
            self.live.error = str(error)

    def problems(self, data=None):
        try:
            data = data if data is not None else self.fleet_data()
            fleet = bd.fleet_from_data(data, self.fleet_path)
            return bd.validate_fleet(fleet)
        except bd.DeployError as error:
            return [str(error)]
        except Exception as error:  # noqa: BLE001
            return [f"Konfiguration nicht lesbar: {error}"]

    def save_fleet(self, data, rev):
        with self.fleet_lock:
            if rev and rev != self.rev():
                return 409, {"error": "fleet.toml wurde inzwischen geändert (z. B. durch einen Job oder "
                                      "von Hand). Bitte neu laden."}
            data = sanitize_fleet(data)
            remember_previous_ota_password(bd.read_fleet_data(self.fleet_path), data)
            problems = self.problems(data)
            hard = [p for p in problems if not p.startswith(SOFT_PROBLEMS)]
            if hard:
                return 400, {"error": "Nicht gespeichert", "problems": hard}
            try:
                bd.write_fleet_data(self.fleet_path, data)
            except bd.DeployError as error:
                return 400, {"error": str(error), "problems": []}
        self._apply_live_config()
        return 200, {"rev": self.rev(), "problems": problems}

    # ---- Status ----------------------------------------------------------
    def pio_available(self):
        at, value = self._pio_cache
        if time.time() - at < 30 and value is not None:
            return value
        try:
            bd.find_pio()
            value = True
        except bd.DeployError:
            value = False
        self._pio_cache = (time.time(), value)
        return value

    def state(self):
        live = self.live.snapshot()
        try:
            data = self.fleet_data()
        except bd.DeployError:
            data = {"node": []}
        nodes = data.get("node", [])
        by_mac = {}
        for sid, ident in live["identities"].items():
            mac = bd.normalize_mac(ident.get("ble_mac"))
            if mac:
                by_mac[mac] = sid
        devices = {}
        claimed = set()
        shelly_cache = bd.load_cache().get("shelly", {})
        now = time.time()
        for node in nodes:
            nid = node.get("id", "")
            sid = nid if nid in live["identities"] or nid in live["status"] else None
            note = ""
            if sid is None and node.get("type") == "esp32":
                cand = by_mac.get(bd.normalize_mac(node.get("ble_mac")))
                if cand:
                    sid, note = cand, f"meldet sich noch als {cand}"
            if sid is None and node.get("legacy_id") and (node["legacy_id"] in live["status"]):
                sid, note = node["legacy_id"], "läuft noch mit altem Skript"
            if sid is None and node.get("type") == "shelly":
                cand = by_mac.get(bd.wifi_to_ble_mac(bd.normalize_mac(node.get("wifi_mac"))))
                if cand:
                    sid, note = cand, f"meldet sich als {cand}"
            ident = live["identities"].get(sid or "", {})
            st = live["state"].get(sid or "", {})
            cached_ip = ""
            if node.get("type") == "shelly" and not ident.get("ip"):
                cached_ip = shelly_cache.get(bd.normalize_mac(node.get("wifi_mac")), "")
            if sid:
                claimed.add(sid)
            devices[nid] = {
                "seen_as": sid, "note": note,
                "status": live["status"].get(sid or "", ""),
                "ip": ident.get("ip") or cached_ip, "version": ident.get("version") or "",
                "hostname": ident.get("hostname") or "", "ble_mac": ident.get("ble_mac") or "",
                "present": st.get("present"), "rssi": st.get("rssi"),
                "age": (now - st["at"]) if st.get("at") else None,
            }
        ignored = set(bd.load_cache().get("ignored", []))
        unknown = []
        for sid, ident in live["identities"].items():
            if sid in claimed or bd.normalize_mac(ident.get("ble_mac")) in ignored or sid in ignored:
                continue
            unknown.append({"id": sid, "type": ident.get("implementation") or "", "ip": ident.get("ip", ""),
                            "ble_mac": ident.get("ble_mac", ""), "version": ident.get("version", ""),
                            "name": ident.get("name") or ident.get("sensor_name") or "",
                            "status": live["status"].get(sid, "")})
        known_macs = {bd.normalize_mac(n.get("wifi_mac")) for n in nodes if n.get("wifi_mac")}
        scan = dict(self.scan)
        scan["found"] = [dict(d, known=d["mac"] in known_macs) for d in self.scan.get("found", [])
                         if d["mac"] not in ignored]
        return {
            "mqtt": {"connected": live["connected"], "error": live["error"]},
            "tracker": live["tracker"],
            "devices": devices,
            "unknown": unknown,
            "versions": read_repo_versions(),
            "jobs": self.jobs.list(),
            "busy": sorted(self.jobs.busy_nodes()),
            "pio": self.pio_available(),
            "paramiko": _has_module("paramiko"),
            "scan": scan,
            "rev": self.rev(),
            "sudo_remembered": sorted(self.sudo_passwords),
            "addon": self.addon,
            "local_tracker": self.local_tracker.status() if self.local_tracker else None,
            "ignored": len(ignored),
        }

    def set_ignored(self, key: str, ignore: bool = True):
        cache = bd.load_cache()
        ignored = set(cache.get("ignored", []))
        key = bd.normalize_mac(key) or str(key)
        if ignore:
            ignored.add(key)
        else:
            ignored.discard(key)
        cache["ignored"] = sorted(ignored)
        bd.save_cache(cache)

    def clear_ignored(self):
        cache = bd.load_cache()
        cache["ignored"] = []
        bd.save_cache(cache)

    # ---- Karte / Grundriss --------------------------------------------------
    def plan(self):
        snap = self.live.plan_snapshot()
        try:
            data = self.fleet_data()
        except bd.DeployError:
            data = {"node": [], "tracker": {}}
        nodes = {n.get("id"): n for n in data.get("node", [])}
        sensors = []
        for sid in sorted(set(snap["positions"]) | set(snap["enabled"])):
            pos = snap["positions"].get(sid) or {}
            node = nodes.get(sid, {})
            ident = snap["identities"].get(sid, {})
            sensors.append({
                "id": sid,
                "name": node.get("name") or ident.get("name") or sid,
                "type": node.get("type") or ident.get("implementation") or "",
                "x": pos.get("x_cm"), "y": pos.get("y_cm"),
                "height_cm": pos.get("height_cm"), "height_set": pos.get("height_set"),
                "floor_cm": pos.get("floor_cm"), "height_active": pos.get("height_active"),
                "configured": bool(pos.get("configured")),
                "enabled": snap["enabled"].get(sid, True),
                "in_fleet": sid in nodes,
            })
        tracker = data.get("tracker", {})
        return {
            "mqtt": {"connected": snap["connected"], "error": snap["error"]},
            "tracker_online": snap["tracker"].get("status") == "online",
            "sensors": sensors,
            "floorplan": snap["plan"]["floorplan"],
            "georef": snap["plan"]["georef"],
            "radio_map": snap["plan"]["radio_map"],
            "fleet_georef": {k: tracker.get(k) for k in ("origin_lat", "origin_lon", "origin_bearing_deg",
                                                         "reference", "reference_lat", "reference_lon")},
            "calibration": snap["plan"]["calibration"],
            "tracker_version": snap["tracker"].get("version", ""),
            "image": load_plan_image_meta(),
            "cal_plan": load_calibration_plan(),
        }

    def live_view(self):
        snap = self.live.plan_snapshot()
        now = time.time()
        live = snap["plan"]["live"]
        sensors = {}
        for sid in set(snap["positions"]) | set(snap["status"]):
            st = snap["state"].get(sid, {})
            sensors[sid] = {"status": snap["status"].get(sid, ""), "present": st.get("present"),
                            "rssi": st.get("rssi")}
        return {
            "live": live,
            "live_age": (now - snap["plan"]["live_at"]) if snap["plan"]["live_at"] else None,
            "gps": snap["plan"]["gps"],
            "tracker": snap["tracker"],
            "sensors": sensors,
            "radio_map": snap["plan"]["radio_map"],
            "calibration": snap["plan"]["calibration"],
        }

    def _require_tracker(self):
        snap = self.live.plan_snapshot()
        if not snap["connected"]:
            raise bd.DeployError("Keine Verbindung zum MQTT-Broker")
        if snap["tracker"].get("status") != "online":
            raise bd.DeployError("Der Tracker ist nicht online – Änderungen am Grundriss und an Positionen "
                                 "speichert er selbst, er muss dafür laufen")

    def save_positions(self, positions: dict):
        self._require_tracker()
        count = 0
        for sid, value in (positions or {}).items():
            if not bd.ID_RE.match(str(sid)):
                raise bd.DeployError(f"ungültige Sensor-ID {sid!r}")
            x, y = (float(v) for v in value)
            if not (math.isfinite(x) and math.isfinite(y)) or max(abs(x), abs(y)) > 1e6:
                raise bd.DeployError(f"ungültige Position für {sid}")
            self.live.publish(f"bluecat/config/sensors/{sid}/position_x/set", f"{x:.1f}")
            self.live.publish(f"bluecat/config/sensors/{sid}/position_y/set", f"{y:.1f}")
            count += 1
        return count

    def save_heights(self, heights: dict):
        """{sensor_id: {"height_cm": Zahl|None, "floor_cm": Zahl|None}} → Tracker."""
        self._require_tracker()
        if not isinstance(heights, dict):
            raise bd.DeployError("Höhen fehlen")
        count = 0
        for sid, value in heights.items():
            if not bd.ID_RE.match(str(sid)) or not isinstance(value, dict):
                raise bd.DeployError(f"ungültige Sensor-ID {sid!r}")
            for key, topic, low, high in (("height_cm", "height", 0.0, 600.0), ("floor_cm", "floor", -5000.0, 50000.0)):
                if key not in value:
                    continue
                raw = value[key]
                if raw in (None, ""):
                    text = ""
                else:
                    try:
                        number = float(raw)
                    except (TypeError, ValueError):
                        raise bd.DeployError(f"{sid}: {key} ist keine Zahl")
                    if not (math.isfinite(number) and low <= number <= high):
                        raise bd.DeployError(f"{sid}: {key} außerhalb {low:g}…{high:g}")
                    text = f"{number:.1f}"
                self.live.publish(f"bluecat/config/sensors/{sid}/{topic}/set", text)
            count += 1
        return count

    def tuning_command(self, body: dict):
        self._require_tracker()
        if not isinstance(body, dict) or not body:
            raise bd.DeployError("Feintuning leer")
        clean = {}
        for key, value in body.items():
            if key == "reset":
                if value is True or (isinstance(value, list) and all(isinstance(k, str) for k in value)):
                    clean[key] = value
                    continue
                raise bd.DeployError("reset ungültig")
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,40}", str(key)):
                raise bd.DeployError(f"ungültiger Parameter {key!r}")
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise bd.DeployError(f"{key}: keine Zahl")
            if not math.isfinite(number):
                raise bd.DeployError(f"{key}: keine Zahl")
            clean[key] = number
        self.live.publish("bluecat/config/tracker/tuning/set", clean)

    def tuning_view(self):
        snap = self.live.plan_snapshot()
        return {"tuning": snap["plan"].get("tuning"), "tracker_online": snap["tracker"].get("status") == "online",
                "tracker_version": snap["tracker"].get("version", ""), "engine": snap["tracker"].get("engine", ""),
                "connected": snap["connected"]}

    def save_floorplan(self, floorplan: dict):
        self._require_tracker()
        if not isinstance(floorplan, dict):
            raise bd.DeployError("Grundriss fehlt")
        walls, rooms = floorplan.get("walls") or [], floorplan.get("rooms") or []
        if not isinstance(walls, list) or not isinstance(rooms, list) or len(walls) > 2000 or len(rooms) > 200:
            raise bd.DeployError("Grundriss ungültig oder zu groß")
        self.live.publish("bluecat/config/floorplan/set", floorplan)

    def save_georef(self, body: dict):
        try:
            lat, lon = float(body["lat"]), float(body["lon"])
            bearing = float(body.get("bearing_deg", 0.0))
            extra = [float(body[k]) for k in ("reference_lat", "reference_lon") if body.get(k) not in (None, "")]
        except (KeyError, TypeError, ValueError):
            raise bd.DeployError("Kartenbezug unvollständig")
        if not all(math.isfinite(v) for v in [lat, lon, bearing] + extra):
            raise bd.DeployError("Kartenbezug enthält ungültige Zahlen")
        bearing %= 360.0
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise bd.DeployError("Koordinaten außerhalb des gültigen Bereichs")
        payload = {"lat": round(lat, 8), "lon": round(lon, 8), "bearing_deg": round(bearing, 3)}
        for key in ("reference", "reference_lat", "reference_lon"):
            if body.get(key) not in (None, ""):
                payload[key] = body[key]
        published = False
        try:
            self._require_tracker()
            self.live.publish("bluecat/config/tracker/georef/set", payload)
            published = True
        except bd.DeployError:
            pass
        # auch in fleet.toml, damit ein neuer Rollout denselben Bezug mitbringt
        with self.fleet_lock:
            data = self.fleet_data()
            tracker = data.setdefault("tracker", {})
            tracker.update({"origin_lat": payload["lat"], "origin_lon": payload["lon"],
                            "origin_bearing_deg": payload["bearing_deg"]})
            for key in ("reference", "reference_lat", "reference_lon"):
                if key in payload:
                    tracker[key] = payload[key]
            bd.write_fleet_data(self.fleet_path, sanitize_fleet(data))
        return {"published": published, "rev": self.rev()}

    def calibration_command(self, body: dict):
        cmd = str((body or {}).get("cmd", ""))
        if cmd not in CALIBRATION_COMMANDS:
            raise bd.DeployError(f"unbekannter Kalibrierbefehl {cmd!r}")
        self._require_tracker()
        payload = {k: v for k, v in body.items() if k in {"cmd", "x", "y", "duration_s", "id", "label", "per_sensor_n",
                                                          "sensors", "n", "walls"}}
        for key in ("x", "y", "duration_s"):
            if key in payload:
                value = float(payload[key])
                if not math.isfinite(value):
                    raise bd.DeployError("ungültige Zahl")
                payload[key] = value
        self.live.publish("bluecat/config/calibration/set", payload)

    _geocode_lock = threading.Lock()
    _geocode_last = 0.0

    def geocode(self, query: str):
        query = (query or "").strip()
        if len(query) < 3:
            return []
        with self._geocode_lock:  # Nominatim: höchstens 1 Anfrage pro Sekunde
            wait = 1.1 - (time.time() - App._geocode_last)
            if wait > 0:
                time.sleep(wait)
            App._geocode_last = time.time()
        url = NOMINATIM_URL + "?" + urlencode({"format": "jsonv2", "limit": 6, "q": query,
                                                "accept-language": "de"})
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                results = json.loads(resp.read().decode("utf-8"))
        except (OSError, ValueError) as error:
            raise bd.DeployError(f"Adresssuche nicht erreichbar: {error}")
        return [{"name": r.get("display_name", ""), "lat": float(r["lat"]), "lon": float(r["lon"])}
                for r in results if "lat" in r and "lon" in r]

    # ---- Shelly-Suche ----------------------------------------------------
    def start_scan(self):
        if self.scan["running"]:
            return
        self.scan.update({"running": True, "error": ""})

        def worker():
            try:
                fleet = bd.load_fleet(self.fleet_path)
                found = bd.scan_shellys(fleet)
                self.scan.update({"found": sorted(found, key=lambda d: tuple(int(x) for x in d["ip"].split("."))),
                                  "at": time.time()})
            except Exception as error:  # noqa: BLE001
                self.scan["error"] = str(error)
            finally:
                self.scan["running"] = False
        threading.Thread(target=worker, daemon=True).start()

    # ---- Tracker umziehen / Home Assistant ----------------------------------
    def _tracker_move_steps(self, fleet: "bd.Fleet", source: str, target: str, sudo_env):
        """Reihenfolge: alten Tracker anhalten (er speichert), Konfiguration holen, erst dann fleet.toml
        umstellen und den neuen starten – nie zwei gleichzeitig. Scheitert ein Schritt vor dem Umstellen,
        bleibt fleet.toml beim alten Ort."""
        addon = bd.TRACKER_ADDON
        current = str(fleet.tracker.get("node") or "")
        if not target:
            raise bd.DeployError("Kein Ziel für den Tracker angegeben")
        if source != current:
            raise bd.DeployError("fleet.toml wurde inzwischen geändert – bitte neu laden")
        if source == target:
            raise bd.DeployError("Der Tracker läuft bereits dort")
        if addon in (source, target) and self.local_tracker is None:
            raise bd.DeployError("In die App umziehen geht nur aus der Home-Assistant-App heraus")
        for nid in (source, target):
            if nid and nid != addon and fleet.node(nid).type != "pi":
                raise bd.DeployError(f"{nid} ist kein Pi")
        name = lambda nid: "Home Assistant (App)" if nid == addon else (fleet.node(nid).display if nid else "–")  # noqa: E731
        steps = []
        cfg_dir = None
        if source == addon:
            steps.append({"label": "Tracker in der App anhalten", "call": self._call_local_tracker("stop"),
                          "critical": True})
            cfg_dir = os.path.join(bd.TRACKER_HOME, "config")
        elif source:
            steps.append({"label": f"{name(source)}: Tracker anhalten", "args": ["tracker-stop", source],
                          "env": sudo_env(source), "critical": True})
            cfg_dir = (os.path.join(bd.TRACKER_HOME, "config") if target == addon
                       else os.path.join(bd.DATA_DIR, ".tracker_move", time.strftime("%Y%m%d-%H%M%S")))
            steps.append({"label": "Konfiguration holen", "args": ["tracker-config", "pull", source, "--dir", cfg_dir],
                          "critical": True})
        steps.append({"label": "fleet.toml umstellen", "call": self._set_tracker_node(target), "critical": True})
        if target == addon:
            steps.append({"label": "Tracker in der App starten", "call": self._call_local_tracker("restart")})
        else:
            if cfg_dir:
                steps.append({"label": "Konfiguration übertragen",
                              "args": ["tracker-config", "push", target, "--dir", cfg_dir], "critical": True})
            steps.append({"label": f"{name(target)}: Tracker installieren", "args": ["pi", target],
                          "env": sudo_env(target)})
        return steps, f"Tracker umziehen: {name(source)} → {name(target)}"

    def _set_tracker_node(self, target: str):
        def call(job):
            with self.fleet_lock:
                data = bd.read_fleet_data(self.fleet_path)
                data.setdefault("tracker", {})["node"] = target
                bd.write_fleet_data(self.fleet_path, data)
            job.log(f"OK [tracker] node = \"{target}\"", "ok")
        return call

    def _remember_ha(self, host: str, port: int, user: str):
        with self.fleet_lock:
            data = bd.read_fleet_data(self.fleet_path)
            ha = dict(data.get("homeassistant") or {})
            if ha.get("host") == host and ha.get("port") == port and ha.get("ssh_user") == user:
                return
            ha.update({"host": host, "port": port, "ssh_user": user})
            data["homeassistant"] = ha
            bd.write_fleet_data(self.fleet_path, data)

    # ---- Jobs ------------------------------------------------------------
    def start_job(self, body: dict):
        action = body.get("action", "")
        node_id = body.get("node") or None
        opts = body.get("options") or {}
        secrets_in = body.get("secrets") or {}
        fleet = bd.load_fleet(self.fleet_path)
        node = fleet.node(node_id) if node_id else None
        label = node.display if node else ""

        def sudo_env(nid):
            pw = secrets_in.get("sudo_password") or (secrets_in.get("sudo_passwords") or {}).get(nid)
            if pw:
                if opts.get("remember", action == "update_all"):
                    self.sudo_passwords[nid] = pw
            else:
                pw = self.sudo_passwords.get(nid)
            return {"BLUECAT_SUDO_PASSWORD": pw} if pw else {}

        if action == "pi_install":
            steps = [{"label": "Installieren", "args": ["pi", node_id], "env": sudo_env(node_id)}]
            title = f"{label}: installieren / aktualisieren"
        elif action == "pi_rollback":
            steps = [{"label": "Rollback", "args": ["pi", node_id, "--rollback"], "env": sudo_env(node_id)}]
            title = f"{label}: zur alten Installation zurück"
        elif action == "ssh_setup":
            env = {"BLUECAT_SSH_PASSWORD": secrets_in["ssh_password"]} if secrets_in.get("ssh_password") else {}
            steps = [{"label": "SSH-Zugang", "args": ["ssh-setup", node_id], "env": env}]
            title = f"{label}: SSH-Zugang einrichten"
        elif action == "import_old":
            steps = [{"label": "Alte Einstellungen", "args": ["import-old", node_id, "--json"], "parse_json": True}]
            title = f"{label}: alte Einstellungen lesen"
        elif action == "shelly_install":
            args = ["shelly", node_id] + (["--fix-settings"] if opts.get("fix_settings", True) else [])
            steps = [{"label": "Shelly", "args": args}]
            title = f"{label}: einrichten / aktualisieren"
        elif action == "esp_usb":
            args = ["esp", "usb"] + ([node_id] if node_id else [])
            if opts.get("port"):
                args += ["--port", str(opts["port"])]
            steps = [{"label": "USB-Flash", "args": args}]
            title = f"{label or 'ESP32'}: per USB flashen"
        elif action == "esp_ota":
            steps = [{"label": "OTA", "args": ["esp", "ota", node_id or "all"]}]
            title = f"{label or 'Alle ESP32'}: per WLAN aktualisieren"
        elif action == "esp_provision":
            steps = [{"label": "Provisionieren", "args": ["esp", "provision"]}]
            title = "ESP32: Sensor-IDs zuweisen"
        elif action == "remove":
            args = ["remove", node_id] + (["--skip-device"] if opts.get("skip_device") else [])
            steps = [{"label": "Entfernen", "args": args,
                      "env": sudo_env(node_id) if node and node.type == "pi" else {}}]
            title = f"{label}: entfernen"
        elif action == "ha_cleanup":
            args = ["ha-cleanup"] + (["--yes"] if opts.get("apply") else ["--dry-run"])
            steps = [{"label": "HA", "args": args}]
            title = "Home Assistant aufräumen" + ("" if opts.get("apply") else " (Vorschau)")
        elif action == "check":
            steps = [{"label": "Prüfen", "args": ["check"]}]
            title = "Konfiguration prüfen"
        elif action == "status":
            steps = [{"label": "Status", "args": ["status"]}]
            title = "Status aller Knoten"
        elif action == "tracker_move":
            steps, title = self._tracker_move_steps(fleet, str(opts.get("from") or ""), str(opts.get("to") or ""),
                                                    sudo_env)
            node_id = None
        elif action == "tracker_restart":
            steps = [{"label": "Tracker", "call": self._call_local_tracker("restart")}]
            title = "Tracker in der App neu starten"
        elif action == "ha_addon":
            ha = opts
            host = str(ha.get("host") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9.:_-]{1,253}", host):
                raise bd.DeployError("Adresse von Home Assistant fehlt")
            args = ["ha-addon", "--host", host, "--port", str(int(ha.get("port") or 22)),
                    "--user", str(ha.get("user") or "root")]
            if ha.get("migrate"):
                args.append("--migrate")
            env = {"BLUECAT_HA_PASSWORD": secrets_in["ha_password"]} if secrets_in.get("ha_password") else {}
            steps = [{"label": "Home Assistant", "args": args, "env": env}]
            title = "TriLola-App auf Home Assistant installieren"
            self._remember_ha(host, int(ha.get("port") or 22), str(ha.get("user") or "root"))
        elif action == "install_pio":
            steps = [{"label": "PlatformIO", "argv": [sys.executable, "-m", "pip", "install",
                                                      "--disable-pip-version-check", "platformio"]}]
            title = "ESP32-Werkzeug (PlatformIO) installieren"
            self._pio_cache = (0.0, None)
        elif action == "update_all":
            steps = []
            for n in fleet.nodes:
                if n.type == "pi":
                    steps.append({"label": f"Pi {n.display}", "args": ["pi", n.id], "env": sudo_env(n.id)})
            if any(n.type == "shelly" for n in fleet.nodes):
                steps.append({"label": "Shellys", "args": ["shelly", "all"] +
                              (["--fix-settings"] if opts.get("fix_settings", True) else [])})
            if any(n.type == "esp32" for n in fleet.nodes) and opts.get("esp", True):
                steps.append({"label": "ESP32 per WLAN", "args": ["esp", "ota", "all"]})
            if fleet.tracker_in_addon and self.local_tracker is not None:
                steps.append({"label": "Tracker (App)", "call": self._call_local_tracker("restart")})
            if not steps:
                raise bd.DeployError("Noch keine Geräte angelegt")
            title = "Alle Geräte aktualisieren"
            node_id = None
        else:
            raise bd.DeployError(f"unbekannte Aktion {action!r}")
        if node_id and node_id in self.jobs.busy_nodes():
            raise bd.DeployError(f"Für {label} läuft bereits eine Aktion")
        job = Job(title, steps, node=node_id, kind=action)
        return self.jobs.submit(job)


def load_token(path: str) -> str:
    """Zugangsschlüssel bleibt über Neustarts gleich – offene Tabs funktionieren nach einem Neustart weiter."""
    try:
        with open(path, encoding="utf-8") as handle:
            token = handle.read().strip()
        if len(token) >= 24:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token)
    except OSError:
        pass
    return token


def _has_module(name):
    import importlib.util
    return importlib.util.find_spec(name) is not None


def remember_previous_ota_password(old: dict, new: dict, keep=3):
    """Neues OTA-Passwort: das alte merken – die ESP32 kennen bis zum nächsten Update nur das alte."""
    old_pw = str(((old or {}).get("esp32") or {}).get("ota_password") or "")
    esp = new.setdefault("esp32", {})
    new_pw = str(esp.get("ota_password") or "")
    previous = esp.get("ota_password_previous") or ((old or {}).get("esp32") or {}).get("ota_password_previous") or []
    if isinstance(previous, str):
        previous = [previous]
    previous = [p for p in previous if isinstance(p, str) and p and p != new_pw]
    if old_pw and old_pw != new_pw:
        previous = [old_pw] + [p for p in previous if p != old_pw]
    if previous:
        esp["ota_password_previous"] = previous[:keep]
    else:
        esp.pop("ota_password_previous", None)


def sanitize_fleet(data: dict) -> dict:
    """Werte aus der Oberfläche in saubere TOML-Typen bringen."""
    data = copy.deepcopy(data or {})
    out = {}
    for section, keys, _ in bd.SETTINGS_LAYOUT:
        values = dict(data.get(section) or {})
        clean = {}
        for key, value in values.items():
            if isinstance(value, str):
                value = value.strip() if "password" not in key else value
            clean[key] = value
        out[section] = clean
    mqtt = out["mqtt"]
    try:
        mqtt["port"] = int(mqtt.get("port") or 1883)
    except (TypeError, ValueError):
        mqtt["port"] = 1883
    tracker = out["tracker"]
    for key in ("origin_lat", "origin_lon", "origin_bearing_deg", "reference_lat", "reference_lon"):
        value = tracker.get(key)
        if value in (None, ""):
            if key == "origin_bearing_deg":
                tracker[key] = 0.0
            else:
                tracker.pop(key, None)
            continue
        try:
            tracker[key] = float(str(value).replace(",", "."))
        except ValueError:
            pass  # validate_fleet meldet das
    if tracker.get("target_mac"):
        tracker["target_mac"] = bd.normalize_mac(tracker["target_mac"]) or tracker["target_mac"]
    for key, value in data.items():
        if key not in out and key != "node":
            out[key] = value
    nodes = []
    for raw in data.get("node") or []:
        node = {}
        for key, value in raw.items():
            if key.startswith("_"):
                continue
            if isinstance(value, str):
                value = value.strip()
            node[key] = value
        for key in ("ble_mac", "wifi_mac"):
            if node.get(key):
                node[key] = bd.normalize_mac(node[key]) or node[key]
        if "ssh_port" in node:
            try:
                node["ssh_port"] = int(node["ssh_port"] or 22)
            except (TypeError, ValueError):
                node["ssh_port"] = 22
        if node.get("type") != "pi":
            node.pop("sensor", None)
            node.pop("ssh_user", None)
            node.pop("ssh_port", None)
        nodes.append(node)
    out["node"] = nodes
    return out


# ---------------------------------------------------------------------------
# Grundriss-Bild
# ---------------------------------------------------------------------------
def image_kind(data: bytes) -> Optional[str]:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def load_plan_image_meta() -> Optional[dict]:
    try:
        with open(os.path.join(PLAN_DIR, "plan_image.json"), encoding="utf-8") as handle:
            meta = json.load(handle)
        if os.path.exists(os.path.join(PLAN_DIR, meta["file"])):
            return meta
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _placement(body: dict, base: Optional[dict] = None) -> dict:
    meta = dict(base or {})
    try:
        if "center" in body:
            cx, cy = (float(v) for v in body["center"])
            meta["center"] = [round(cx, 1), round(cy, 1)]
        if "width_cm" in body:
            meta["width_cm"] = round(float(body["width_cm"]), 1)
        if "rotation_deg" in body:
            meta["rotation_deg"] = round(float(body["rotation_deg"]) % 360.0, 3)
        if "opacity" in body:
            meta["opacity"] = round(min(max(float(body["opacity"]), 0.05), 1.0), 2)
        if "visible" in body:
            meta["visible"] = bool(body["visible"])
    except (TypeError, ValueError):
        raise bd.DeployError("ungültige Bildlage")
    numbers = meta.get("center", [0, 0]) + [meta.get("width_cm", 1000), meta.get("rotation_deg", 0)]
    if not all(math.isfinite(v) for v in numbers) or not (10 <= meta.get("width_cm", 1000) <= 1e5):
        raise bd.DeployError("ungültige Bildlage")
    return meta


def save_plan_image(body: dict) -> dict:
    import base64
    raw = str(body.get("data") or "")
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    try:
        data = base64.b64decode(raw, validate=True)
    except ValueError:
        raise bd.DeployError("Bilddaten unlesbar")
    if len(data) > PLAN_IMAGE_MAX_BYTES:
        raise bd.DeployError("Bild zu groß (max. 20 MB)")
    kind = image_kind(data)
    if kind is None:
        raise bd.DeployError("Nur PNG, JPG, WebP oder GIF")
    try:
        width_px, height_px = int(body["width_px"]), int(body["height_px"])
    except (KeyError, TypeError, ValueError):
        raise bd.DeployError("Bildgröße fehlt")
    if not (1 <= width_px <= 20000 and 1 <= height_px <= 20000):
        raise bd.DeployError("Bildgröße ungültig")
    os.makedirs(PLAN_DIR, exist_ok=True)
    old = load_plan_image_meta()
    name = f"plan_image_{int(time.time())}.{kind}"
    with open(os.path.join(PLAN_DIR, name), "wb") as handle:
        handle.write(data)
    if old and old.get("file") != name:
        try:
            os.remove(os.path.join(PLAN_DIR, old["file"]))
        except OSError:
            pass
    meta = _placement(body, {"center": [0.0, 0.0], "width_cm": 1500.0, "rotation_deg": 0.0, "opacity": 0.6,
                             "visible": True})
    meta.update({"file": name, "width_px": width_px, "height_px": height_px,
                 "original_name": str(body.get("name") or "")[:120], "updated": time.time()})
    _write_plan_meta(meta)
    return meta


def update_plan_image(body: dict) -> dict:
    meta = load_plan_image_meta()
    if meta is None:
        raise bd.DeployError("Kein Grundriss-Bild hochgeladen")
    meta = _placement(body, meta)
    meta["updated"] = time.time()
    _write_plan_meta(meta)
    return meta


def delete_plan_image():
    meta = load_plan_image_meta()
    for name in ([meta["file"]] if meta else []) + ["plan_image.json"]:
        try:
            os.remove(os.path.join(PLAN_DIR, name))
        except OSError:
            pass


def load_calibration_plan() -> list:
    try:
        with open(os.path.join(PLAN_DIR, "calibration_plan.json"), encoding="utf-8") as handle:
            points = json.load(handle)
        return points if isinstance(points, list) else []
    except (OSError, ValueError):
        return []


def save_calibration_plan(points) -> list:
    if not isinstance(points, list) or len(points) > 60:
        raise bd.DeployError("Kalibrierplan ungültig")
    clean = []
    for p in points:
        try:
            x, y = float(p["x"]), float(p["y"])
        except (KeyError, TypeError, ValueError):
            raise bd.DeployError("Kalibrierplan ungültig")
        if not (math.isfinite(x) and math.isfinite(y)):
            raise bd.DeployError("Kalibrierplan ungültig")
        pid = str(p.get("id") or "")[:32]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", pid):
            raise bd.DeployError("Kalibrierplan: ungültige Punkt-ID")
        clean.append({"id": pid, "x": round(x, 1), "y": round(y, 1), "label": str(p.get("label") or "")[:40]})
    os.makedirs(PLAN_DIR, exist_ok=True)
    tmp = os.path.join(PLAN_DIR, "calibration_plan.json.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(clean, handle, indent=1)
    os.replace(tmp, os.path.join(PLAN_DIR, "calibration_plan.json"))
    return clean


def _write_plan_meta(meta):
    tmp = os.path.join(PLAN_DIR, "plan_image.json.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=1)
    os.replace(tmp, os.path.join(PLAN_DIR, "plan_image.json"))


def fetch_tile(layer: str, z: int, x: int, y: int):
    """Kachel aus dem Cache oder vom Kartendienst; None, wenn nicht verfügbar."""
    if layer not in TILE_SOURCES:
        return None
    template, max_zoom = TILE_SOURCES[layer]
    if not (0 <= z <= max_zoom and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        return None
    path = os.path.join(TILE_CACHE_DIR, layer, str(z), str(x), f"{y}.img")
    try:
        if time.time() - os.path.getmtime(path) < TILE_MAX_AGE_SEC:
            with open(path, "rb") as handle:
                return handle.read()
    except OSError:
        pass
    req = urllib.request.Request(template.format(z=z, x=x, y=y), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
    except (OSError, ValueError):
        # offline: alte Kachel ist besser als keine
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except OSError:
            return None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "wb") as handle:
            handle.write(data)
        os.replace(path + ".tmp", path)
    except OSError:
        pass
    return data


def list_serial_ports():
    try:
        from serial.tools import list_ports  # type: ignore
    except ModuleNotFoundError:
        return {"ports": [], "error": "pyserial fehlt (pip install pyserial)"}
    likely_vids = {0x10C4: "CP210x", 0x1A86: "CH340", 0x303A: "Espressif", 0x0403: "FTDI"}
    ports = []
    for p in list_ports.comports():
        chip = likely_vids.get(p.vid or -1, "")
        ports.append({"device": p.device, "description": p.description or "", "chip": chip,
                      "likely_esp": bool(chip)})
    ports.sort(key=lambda p: (not p["likely_esp"], p["device"]))
    return {"ports": ports, "error": ""}


def test_mqtt(cfg: dict):
    try:
        import paho.mqtt.client as mqtt
    except ModuleNotFoundError:
        return {"ok": False, "error": "paho-mqtt fehlt"}
    result = {"ok": False, "error": "keine Antwort"}
    done = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            result.update(ok=False, error=f"Broker lehnt ab: {reason_code}")
        else:
            result.update(ok=True, error="")
        done.set()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"bluecat_gui_test_{secrets.token_hex(3)}")
    if cfg.get("user"):
        client.username_pw_set(str(cfg["user"]), str(cfg.get("password") or "") or None)
    client.on_connect = on_connect
    try:
        client.connect(str(cfg.get("host") or ""), int(cfg.get("port") or 1883), 10)
    except (OSError, ValueError) as error:
        return {"ok": False, "error": f"nicht erreichbar: {error}"}
    client.loop_start()
    done.wait(5)
    client.loop_stop()
    try:
        client.disconnect()
    except Exception:  # noqa: BLE001
        pass
    return result


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def make_handler(app: App, port: int, ingress: bool = False):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "BluecatGUI/1"

        def log_message(self, fmt, *args):  # ruhig
            pass

        def _send(self, code, body, content_type="application/json; charset=utf-8", cache=False):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self, api=True, same_site=False):
            if ingress:
                # Nur der HA-Supervisor darf durch; er hat den Nutzer bereits angemeldet.
                if self.client_address[0] not in INGRESS_PEERS:
                    self._send(403, {"error": "nur über Home Assistant erreichbar"})
                    return False
                if same_site and self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
                    self._send(403, {"error": "nur für die Rollout-Oberfläche"})
                    return False
                if self.command == "POST" and (
                        self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none")
                        or not self.headers.get("Content-Type", "").startswith("application/json")):
                    self._send(403, {"error": "nur aus der TriLola-Oberfläche"})
                    return False
                return True
            if self.headers.get("Host", "") not in allowed_hosts:
                self._send(403, {"error": "falscher Host"})
                return False
            # Kacheln/Bibliothek nur für die eigene Seite (keine fremde Webseite, die über uns Kacheln lädt)
            if same_site and self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
                self._send(403, {"error": "nur für die Rollout-Oberfläche"})
                return False
            if api and not secrets.compare_digest(self.headers.get("X-Bluecat-Token", ""), app.token):
                self._send(401, {"error": "Sitzung abgelaufen – Oberfläche neu starten"})
                return False
            return True

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > 30_000_000:
                raise ValueError("zu groß")
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                if not self._guard(api=False):
                    return
                with open(UI_FILE, "rb") as handle:
                    page = handle.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                 "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                                 "connect-src 'self'; frame-ancestors " + ("'self'" if ingress else "'none'"))  # HA bettet per iframe ein
                self.end_headers()
                self.wfile.write(page)
                return
            if url.path.startswith("/vendor/"):
                name = url.path[len("/vendor/"):]
                if not self._guard(api=False, same_site=True) or name not in VENDOR_FILES:
                    if name not in VENDOR_FILES:
                        self._send(404, {"error": "nicht gefunden"})
                    return
                with open(os.path.join(VENDOR_DIR, name), "rb") as handle:
                    self._send(200, handle.read(), VENDOR_FILES[name], cache=True)
                return
            if url.path == "/plan-image":
                if not self._guard(api=False, same_site=True):
                    return
                meta = load_plan_image_meta()
                if meta is None:
                    self._send(404, b"", "text/plain")
                    return
                with open(os.path.join(PLAN_DIR, meta["file"]), "rb") as handle:
                    data = handle.read()
                self._send(200, data, PLAN_IMAGE_TYPES.get(image_kind(data) or "", "application/octet-stream"))
                return
            if url.path.startswith("/tiles/"):
                if not self._guard(api=False, same_site=True):
                    return
                match = re.fullmatch(r"/tiles/([a-z]+)/(\d+)/(\d+)/(\d+)\.png", url.path)
                data = fetch_tile(match.group(1), *(int(g) for g in match.groups()[1:])) if match else None
                if data is None:
                    self._send(404, b"", "text/plain")
                else:
                    kind = "image/jpeg" if data[:2] == b"\xff\xd8" else "image/png"
                    self._send(200, data, kind, cache=True)
                return
            if not url.path.startswith("/api/") or not self._guard():
                if not url.path.startswith("/api/"):
                    self._send(404, {"error": "nicht gefunden"})
                return
            try:
                if url.path == "/api/fleet":
                    self._send(200, {"data": app.fleet_data(), "rev": app.rev(), "problems": app.problems(),
                                     "path": app.fleet_path})
                elif url.path == "/api/state":
                    self._send(200, app.state())
                elif url.path == "/api/ports":
                    self._send(200, list_serial_ports())
                elif url.path == "/api/plan":
                    self._send(200, app.plan())
                elif url.path == "/api/live":
                    self._send(200, app.live_view())
                elif url.path == "/api/tuning":
                    self._send(200, app.tuning_view())
                elif url.path == "/api/geocode":
                    self._send(200, {"results": app.geocode((parse_qs(url.query).get("q") or [""])[0])})
                elif url.path.startswith("/api/jobs/"):
                    job = app.jobs.jobs.get(url.path.rsplit("/", 1)[-1])
                    if job is None:
                        self._send(404, {"error": "Job unbekannt"})
                        return
                    start = int((parse_qs(url.query).get("from") or ["0"])[0])
                    self._send(200, dict(job.summary(), lines=job.lines[start:], next=len(job.lines)))
                else:
                    self._send(404, {"error": "nicht gefunden"})
            except bd.DeployError as error:
                self._send(400, {"error": str(error)})
            except Exception as error:  # noqa: BLE001
                traceback.print_exc()
                self._send(500, {"error": str(error)})

        def do_PUT(self):  # noqa: N802
            self.do_POST()

        def do_POST(self):  # noqa: N802
            if not self._guard():
                return
            url = urlparse(self.path)
            try:
                body = self._body()
                if url.path == "/api/fleet":
                    code, reply = app.save_fleet(body.get("data") or {}, body.get("rev"))
                    self._send(code, reply)
                elif url.path == "/api/jobs":
                    job = app.start_job(body)
                    self._send(200, job.summary())
                elif url.path.startswith("/api/jobs/") and url.path.endswith("/cancel"):
                    self._send(200, {"ok": app.jobs.cancel(url.path.split("/")[3])})
                elif url.path == "/api/scan":
                    app.start_scan()
                    self._send(200, {"ok": True})
                elif url.path == "/api/mqtt-test":
                    self._send(200, test_mqtt(body.get("mqtt") or {}))
                elif url.path == "/api/plan/positions":
                    self._send(200, {"saved": app.save_positions(body.get("positions") or {})})
                elif url.path == "/api/plan/floorplan":
                    app.save_floorplan(body.get("floorplan"))
                    self._send(200, {"ok": True})
                elif url.path == "/api/plan/georef":
                    self._send(200, app.save_georef(body))
                elif url.path == "/api/plan/image":
                    self._send(200, {"image": save_plan_image(body)})
                elif url.path == "/api/plan/image-placement":
                    self._send(200, {"image": update_plan_image(body)})
                elif url.path == "/api/plan/calibration-plan":
                    self._send(200, {"points": save_calibration_plan(body.get("points"))})
                elif url.path == "/api/plan/image-delete":
                    delete_plan_image()
                    self._send(200, {"ok": True})
                elif url.path == "/api/plan/heights":
                    self._send(200, {"count": app.save_heights(body.get("heights"))})
                elif url.path == "/api/tuning":
                    app.tuning_command(body)
                    self._send(200, {"ok": True})
                elif url.path == "/api/calibration":
                    app.calibration_command(body)
                    self._send(200, {"ok": True})
                elif url.path == "/api/ignore":
                    app.set_ignored(str(body.get("key") or ""), bool(body.get("ignore", True)))
                    self._send(200, {"ok": True})
                elif url.path == "/api/unignore-all":
                    app.clear_ignored()
                    self._send(200, {"ok": True})
                elif url.path == "/api/forget-sudo":
                    app.sudo_passwords.clear()
                    self._send(200, {"ok": True})
                else:
                    self._send(404, {"error": "nicht gefunden"})
            except bd.DeployError as error:
                self._send(400, {"error": str(error)})
            except (ValueError, KeyError) as error:
                self._send(400, {"error": f"ungültige Anfrage: {error}"})
            except Exception as error:  # noqa: BLE001
                traceback.print_exc()
                self._send(500, {"error": str(error)})

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bluecat/TriLola – Rollout-Oberfläche")
    parser.add_argument("--fleet", default=bd.DEFAULT_FLEET)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--ingress", action="store_true",
                        help="Home-Assistant-Add-on: auf allen Adressen lauschen, nur den Supervisor zulassen")
    args = parser.parse_args(argv)

    app = App(args.fleet)
    app.ingress = args.ingress
    if args.ingress:
        server = ThreadingHTTPServer(("0.0.0.0", args.port), None)
        server.RequestHandlerClass = make_handler(app, args.port, ingress=True)
        server.daemon_threads = True
        print(f"TriLola-Add-on: Oberfläche auf Port {args.port} (nur über Home Assistant)")
        # Als PID 1 ignoriert Python SIGTERM ohne Handler – sauber beenden (Tracker speichert dabei)
        signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
        app.start_background()
        try:
            server.serve_forever(poll_interval=0.3)
        finally:
            app.stop_background()
            server.server_close()
        return 0
    server = None
    for port in [args.port] + list(range(args.port + 1, args.port + 20)) + [0]:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), None)
            break
        except OSError:
            continue
    assert server is not None
    port = server.server_address[1]
    server.RequestHandlerClass = make_handler(app, port)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{port}/#t={app.token}"
    print("Bluecat Rollout läuft. Oberfläche:")
    print(f"  {url}")
    print("Beenden mit Strg+C (bzw. dieses Fenster schließen).")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
