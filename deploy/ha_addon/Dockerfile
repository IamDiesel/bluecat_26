# Debian (glibc): die ESP32-Werkzeuge von PlatformIO laufen nicht unter Alpine/musl
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 LANG=C.UTF-8

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY app/deploy/requirements-gui.txt app/bt_tracker/requirements-tracker.txt /tmp/
RUN pip install -r /tmp/requirements-gui.txt -r /tmp/requirements-tracker.txt platformio

COPY app /opt/trilola
COPY run.sh /run.sh
# Windows-Checkout (CRLF) darf das Startskript nicht kaputt machen
RUN sed -i 's/\r$//' /run.sh && chmod a+x /run.sh

CMD [ "/run.sh" ]
