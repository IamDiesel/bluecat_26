"""Nachgebauter Shelly Gen2 (HTTP-RPC mit Digest SHA-256) für Tests."""
import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeShelly:
    def __init__(self, mac="02000010200C", password="", name="Licht", host="127.0.0.1", port=0,
                 ble=True, mqtt_server="192.168.10.44:1883", eco=False, old_script=True):
        self.mac = mac
        self.password = password
        self.name = name
        self.kvs = {}
        self.scripts = {}
        self.calls = []
        self.config = {"ble": {"enable": ble}, "mqtt": {"enable": True, "server": mqtt_server},
                       "sys": {"device": {"eco_mode": eco, "name": name}}}
        self.rebooted = False
        if old_script:
            self.scripts[1] = {"id": 1, "name": "trilola_wz", "enable": True, "running": True,
                               "code": "// Bluecat BLE-Sensor für Shelly (Hocheffiziente Architektur)\nlet x = 1;"}
        self.nonce = "abc123"
        handler = self._make_handler()
        self.server = ThreadingHTTPServer((host, port), handler)
        self.host = f"{host}:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()

    # ------------------------------------------------------------------
    def _check_auth(self, header):
        if not self.password:
            return True
        if not header or not header.startswith("Digest "):
            return False
        p = dict(re.findall(r'(\w+)="?([^",]+)"?', header))
        h = lambda s: hashlib.sha256(s.encode()).hexdigest()
        ha1 = h(f"admin:shellyplus1-{self.mac.lower()}:{self.password}")
        ha2 = h(f"POST:{p.get('uri')}")
        expected = h(f"{ha1}:{self.nonce}:{p.get('nc')}:{p.get('cnonce')}:{p.get('qop')}:{ha2}")
        return p.get("response") == expected

    def rpc(self, method, params):
        self.calls.append((method, params))
        s = self.scripts
        if method == "Shelly.GetDeviceInfo":
            return {"id": f"shellyplus1-{self.mac.lower()}", "mac": self.mac, "gen": 2, "ver": "2.0.1",
                    "model": "SNSW-001X16EU", "name": self.name}
        if method == "KVS.Set":
            self.kvs[params["key"]] = params["value"]
            return {"etag": "x"}
        if method == "Script.List":
            return {"scripts": [{k: v for k, v in sc.items() if k != "code"} for sc in s.values()]}
        if method == "Script.GetCode":
            code = s[params["id"]]["code"]
            off, ln = params.get("offset", 0), params.get("len", len(code))
            return {"data": code[off:off + ln], "left": max(len(code) - off - ln, 0)}
        if method == "Script.Create":
            new_id = max(s or {0: 0}) + 1
            s[new_id] = {"id": new_id, "name": params.get("name", ""), "enable": False, "running": False, "code": ""}
            return {"id": new_id}
        if method == "Script.Stop":
            s[params["id"]]["running"] = False
            return {"was_running": True}
        if method == "Script.Start":
            s[params["id"]]["running"] = True
            return {"was_running": False}
        if method == "Script.PutCode":
            if len(params["code"].encode("utf-8")) > 2048:
                raise KeyError("code zu lang")
            sc = s[params["id"]]
            sc["code"] = (sc["code"] if params.get("append") else "") + params["code"]
            return {"len": len(sc["code"])}
        if method == "Script.SetConfig":
            s[params["id"]].update(params["config"])
            return {"restart_required": False}
        if method == "Script.GetStatus":
            return {"id": params["id"], "running": s[params["id"]]["running"], "errors": []}
        if method == "BLE.GetConfig":
            return dict(self.config["ble"])
        if method == "MQTT.GetConfig":
            return dict(self.config["mqtt"])
        if method == "Sys.GetConfig":
            return {"device": dict(self.config["sys"]["device"])}
        if method == "BLE.SetConfig":
            self.config["ble"].update(params["config"])
            return {"restart_required": True}
        if method == "MQTT.SetConfig":
            self.config["mqtt"].update(params["config"])
            return {"restart_required": True}
        if method == "Sys.SetConfig":
            self.config["sys"]["device"].update(params["config"].get("device", {}))
            return {"restart_required": False}
        if method == "Shelly.Reboot":
            self.rebooted = True
            return None
        raise KeyError(method)

    def _make_handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body, headers=None):
                data = json.dumps(body).encode()
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/shelly":
                    self._send(200, {"id": f"shellyplus1-{fake.mac.lower()}", "mac": fake.mac, "gen": 2,
                                     "model": "SNSW-001X16EU", "ver": "2.0.1", "name": fake.name,
                                     "auth_en": bool(fake.password)})
                else:
                    self._send(404, {})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) or b"{}"
                body = json.loads(raw)
                # wie mjson in der echten Firmware: nur \u00XX-Escapes, sonst "bad argument"
                if re.search(rb"\\u(?!00)[0-9a-fA-F]{4}", raw):
                    arg = "code" if body.get("method") == "Script.PutCode" else "params"
                    self._send(200, {"id": body.get("id"), "error": {"code": -103,
                               "message": f"Missing or bad argument '{arg}'!"}})
                    return
                if not fake._check_auth(self.headers.get("Authorization")):
                    self._send(401, {}, {"WWW-Authenticate":
                                         f'Digest qop="auth", realm="shellyplus1-{fake.mac.lower()}", '
                                         f'nonce="{fake.nonce}", algorithm=SHA-256'})
                    return
                try:
                    result = fake.rpc(body["method"], body.get("params") or {})
                    self._send(200, {"id": body.get("id"), "result": result})
                except KeyError as e:
                    self._send(200, {"id": body.get("id"), "error": {"code": -114, "message": f"unbekannt {e}"}})

        return Handler
