#!/usr/bin/env bash
# TriLola Rollout – startet die Oberfläche im Browser (Linux/macOS).
set -euo pipefail
cd "$(dirname "$0")/deploy"
VENV="$PWD/.venv"
if [ ! -x "$VENV/bin/python" ]; then
    echo "Richte die Rollout-Umgebung ein (einmalig) ..."
    python3 -m venv "$VENV"
fi
if ! cmp -s requirements-gui.txt "$VENV/requirements.stamp"; then
    echo "Installiere benötigte Pakete ..."
    if "$VENV/bin/python" -m pip install -q --disable-pip-version-check -r requirements-gui.txt; then
        cp requirements-gui.txt "$VENV/requirements.stamp"
    else
        echo "Warnung: Pakete konnten nicht installiert werden – versuche trotzdem zu starten."
    fi
fi
exec "$VENV/bin/python" bluecat_gui.py "$@"
