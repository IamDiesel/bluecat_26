#!/usr/bin/env bash
# TriLola als Home-Assistant-App im Devcontainer bauen, starten, stoppen, Protokoll zeigen.
#   bash deploy/devcontainer/trilola_app.sh start|logs|status|stop
# Wird von den VS-Code-Tasks aufgerufen (deploy/devcontainer/tasks.json).
set -u
cd "$(dirname "$0")/../.." || exit 1
SLUG=local_trilola

wait_supervisor() {
  for _ in $(seq 1 60); do
    ha supervisor info >/dev/null 2>&1 && return 0
    echo "Warte auf den Supervisor … (läuft Task „1. Home Assistant starten“?)"
    sleep 5
  done
  echo "Supervisor antwortet nicht." >&2
  exit 1
}

status() {
  echo "=== Status $(date '+%H:%M:%S') ==="
  echo "- Home Assistant Core:"; ha core info 2>&1 | grep -E '^(state|version):' | sed 's/^/    /'
  echo "- TriLola:"; ha apps info "$SLUG" 2>&1 | grep -E '^(state|version):' | sed 's/^/    /'
  for port in 80 8123 4357; do
    python3 - "$port" <<'PY'
import http.client, sys
port = int(sys.argv[1])
for path in ("/", "/onboarding.html", "/auth/providers"):
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", path, headers={"Host": f"localhost:{port}"})
        r = c.getresponse()
        loc = r.getheader("Location")
        print(f"- http://127.0.0.1:{port}{path} → {r.status}" + (f" → Location: {loc}" if loc else ""))
    except Exception as e:
        print(f"- http://127.0.0.1:{port}{path} → {type(e).__name__}: {e}")
PY
  done
  echo "- lauschende Ports:"; (ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -E ':(80|8123|4357) ' | sed 's/^/    /'
  echo "=================="
}

case "${1:-start}" in
  start)
    python3 deploy/bluecat_deploy.py ha-addon --stage || exit 1
    wait_supervisor
    # Beim ersten Start lädt der Supervisor alle Stores – das dauert länger als das ha-Kommando wartet.
    for try in 1 2 3 4; do
      ha store reload >/dev/null 2>&1 && break
      echo "Store wird noch neu geladen (Versuch $try) …"
      sleep 15
    done
    for _ in $(seq 1 36); do
      ha apps info "$SLUG" >/dev/null 2>&1 && break
      echo "Warte, bis Home Assistant die App kennt …"
      sleep 5
    done
    if ha apps info "$SLUG" --raw-json 2>/dev/null | grep -q '"version": *"[0-9]'; then
      echo "App ist installiert → neu bauen"
      ha apps rebuild --force "$SLUG" || exit 1
    else
      echo "App wird installiert (baut das Image, beim ersten Mal einige Minuten) …"
      ha apps install "$SLUG" || exit 1
    fi
    ha apps start "$SLUG" || exit 1
    echo "Läuft. Home Assistant: VS Code → Reiter „Ports“ → Zeile „Home Assistant (80)“ → Adresse im Browser öffnen → Einstellungen → Apps → TriLola"
    exec ha apps logs -f "$SLUG"
    ;;
  logs) status; exec ha apps logs -f "$SLUG" ;;
  status) status ;;
  stop) ha apps stop "$SLUG" ;;
  *) echo "Aufruf: $0 start|logs|status|stop" >&2; exit 2 ;;
esac
