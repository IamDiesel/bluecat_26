
---

# TriLola - Advanced BLE Indoor Positioning System 🐈

TriLola ist ein hochpräzises, stochastisches Indoor-Tracking-System, das auf Bluetooth Low Energy (BLE) und MQTT basiert. Es wurde entwickelt, um ein dynamisches Ziel (z. B. ein Haustier-Halsband) über eine heterogene Sensorlandschaft (Unix, Shelly, ESP32) im Raum zu lokalisieren.

Das System nutzt eine modulare OOP-Architektur, Voxel-basierte Funkwiderstandsdetektion (Tomographie/SLAM) und verschmilzt einen kinematischen AMCL-Partikelfilter mit einem Interacting Multiple Model Constrained Kalman Filter (IMM-CKF). Verrauschte RSSI-Signale werden so in flüssige, stetige Raumkoordinaten übersetzt. Die Integration in Home Assistant erfolgt vollautomatisch via MQTT Auto-Discovery.

---

## 🏗 Architektur-Übersicht

Das System besteht aus zwei Hauptkomponenten:

1. **Zentraler Tracker (`trilola_tracker.py` / `engine.py`):** Das Gehirn des Systems. Empfängt alle MQTT-Daten via `PayloadParser`, verwaltet die Filter in der `TrackingEngine`, berechnet Echtzeit-Hardware-Offsets im `RadioEnvironmentModel` und führt die stochastische Positionsbestimmung durch.
2. **Dezentrale Sensoren (Nodes):** Passive BLE-Scanner, die das Ziel und sich gegenseitig (Mesh) überwachen und die Signalstärken (RSSI) per MQTT an den Tracker leiten.

---

## 🚀 1. Inbetriebnahme: Zentrale (Unix Tracker)

Der Tracker läuft idealerweise als systemd-Service auf einem zentralen Raspberry Pi oder Linux-Server.

**Voraussetzungen:**

* Python 3.9+
* Laufender MQTT-Broker (z. B. Mosquitto)
* Home Assistant (für Auto-Discovery und Steuerung)

**Schritte:**

1. Repository klonen und Abhängigkeiten installieren:

```bash
pip install paho-mqtt>=2.0 numpy scipy

```

2. Konfigurationsdatei prüfen: Kopiere die Vorlage zu `secrets_tri.py` und trage dort die korrekten MQTT-Zugangsdaten ein.
3. Systemd-Service einrichten (z. B. unter `/etc/systemd/system/trilola.service`):

```ini
[Unit]
Description=TriLola Central Tracker
After=network.target

[Service]
ExecStart=/usr/bin/python3 /pfad/zu/trilola_tracker.py
Restart=always
User=pi

[Install]
WantedBy=multi-user.target

```

4. Service starten: `sudo systemctl enable --now trilola.service`

---

## 📡 2. Inbetriebnahme: Sensoren (Nodes)

Das System unterstützt eine komplett heterogene Hardware-Landschaft. Jeder Sensor meldet sich bei Aktivierung vollautomatisch per MQTT am Tracker an. Ein Neustart des Trackers ist nicht erforderlich.

### A. Unix Sensor (z. B. Raspberry Pi Zero)

Nutzt den internen Bluetooth-Stack (`bluez`) des Betriebssystems.

* **Wichtig:** Im Betriebssystem muss der Bluetooth Experimental Mode aktiviert sein! Ergänze dazu in der Bluetooth-Service-Datei den Startbefehl um das Flag: `ExecStart=/usr/lib/bluetooth/bluetoothd -E`
* **Python Abhängigkeiten:** `pip install bleak>=0.22 paho-mqtt>=2.0 dbus-next>=0.2.3`

### B. Shelly Sensor (mJS)

Hocheffiziente, event-basierte Scanner-Architektur mit O(1) MAC-Lookup und Median-Batching.

* **Voraussetzung:** Shelly-Firmware **>= v2.0**!
* **Wichtig:** Deaktiviere zwingend den **Eco Mode** in den Shelly-Einstellungen.

### C. ESP32 Sensor (PlatformIO / C++)

Nutzt den ressourcenschonenden NimBLE-Stack.

* **Wichtig für die `platformio.ini`:** Um Bootloops zu verhindern, muss die Plattform-Version gepinnt werden:

```ini
[env:esp32dev]
platform = espressif32 @ ~6.5.0
board = esp32dev
framework = arduino
lib_deps =
    h2zero/NimBLE-Arduino @ ^1.4.1
    knolleary/PubSubClient @ ^2.8
    bblanchon/ArduinoJson @ ^7.0.4

```

---

## 📐 3. Kalibrierung (`calibrate_sensors.py`)

BLE-Signale variieren je nach Hardware und Umgebung stark. Die Kalibrierung ermittelt die Konstanten für das Log-Distanz-Modell:

$$RSSI(d)=tx\_power-10\cdot n\_factor\cdot\log_{10}(d)$$

* **`tx_power`:** Der gemessene RSSI-Wert bei exakt 1 Meter Entfernung.
* **`n_factor`:** Der Dämpfungsfaktor (Path Loss).
* **`q_variance`:** Die Prozessrauschkovarianz für den Kalman-Filter (bestimmt die Agilität/Trägheit im 1D-Filter).

---

## 🧠 4. Filter- & Tracking-Architektur (5 Stufen)

Die Übersetzung der chaotischen RSSI-Werte in stetige Raumkoordinaten durchläuft fünf hochspezialisierte mathematische Stufen.

### Stufe 1: Echtzeit-Hardware-Kalibrierung (Mesh-Offset)

Das Mesh-Netzwerk vergleicht permanent die inter-Sensor-Kommunikation mit einer gelernten Baseline. Fällt die Sendeleistung eines Sensors temporär ab (z.B. durch Spannungsschwankungen), wird dieser Offset ($\Delta A_l$) in Echtzeit auf den Messwert des Halsbandes angewendet.

$$\Delta A_l=\text{median}(R_{Ml}-\overline{R_{Ml}})$$

$$RSSI_{korr}=RSSI_{roh}-\Delta A_l$$

```python
# In RadioEnvironmentModel.get_hardware_offset()
current_median = float(np.median(np.asarray(recent, dtype=float)))
offset = current_median - baseline.baseline_rssi
offsets.append(offset)
return float(np.median(offsets))

# In TrackingEngine.process_tick()
hardware_offset = self.radio_env.get_hardware_offset(name, now)
node.rssi -= hardware_offset

```

### Stufe 2: Signalvorverarbeitung & 1D-Kalman-Filterung

Der Hardware-korrigierte RSSI-Wert wird lokal pro Sensor entrauscht.

* **DBSCAN (Robust Outlier Rejection):** Ausreißer werden über die Median Absolute Deviation ($MAD$) geblockt.

$$MAD=\text{median}(\vert{}X_i-\text{median}(X)\vert{})$$


$$\sigma=\max(1.4826\cdot MAD,0.5)$$


$$T=\max(\epsilon,3\sigma)$$



```python
median = float(np.median(data))
mad = float(np.median(np.abs(data - median)))
robust_sigma = max(1.4826 * mad, 0.5)
threshold = max(self.eps, 3.0 * robust_sigma)
inliers = data[np.abs(data - median) <= threshold]

```

* **1D-Kalman-Filter & Innovation Gate:** Modelliert die Trägheit des Funksignals ($x=[RSSI,\Delta RSSI]^T$). Weicht ein Messwert massiv ab, bläst das Gate die Messvarianz ($R_k$) auf, um Teleportationen zu verhindern.

$$F=\begin{bmatrix}1&\Delta t\\0&1\end{bmatrix}$$


$$Q=q_{var}\cdot\begin{bmatrix}\frac{\Delta t^4}{4}&\frac{\Delta t^3}{2}\\\frac{\Delta t^3}{2}&\Delta t^2\end{bmatrix}$$


$$\text{inflation}=\min\left(\left(\frac{\text{innovation}}{\sigma_{gate}}\right)^3,500.0\right)$$



```python
x_pred = F @ self.x
P_pred = F @ self.P @ F.T + Q
innovation = z - self.H @ x_pred
inflation = min((normalized_innovation / self.innovation_gate_sigma) ** 3, 500.0)
R_k = R_k * inflation

```

### Stufe 3: Voxel-Tomographie & Mesh-Netzwerk (`RadioGridMap`)

Die `RadioGridMap` lernt aktiv Wände und physikalische Hindernisse (in dB/m) in einem 50x50 cm Raster.

* **SIRT-Algorithmus (Statisches Mesh):** Die Abweichung zwischen idealem und realem inter-Sensor RSSI-Wert wird iterativ auf die Voxel zwischen den Sensoren verteilt.

$$A_{target}=\max(RSSI_{ideal}-RSSI_{baseline},0)$$


$$\Delta V_i=\frac{E\cdot\text{relaxation}}{d}$$



```python
target_attenuation = max(ideal_rssi - baseline.baseline_rssi, 0.0)
current_attenuation = np.mean(self.grid[idx_x, idx_y]) * dist_m
error = target_attenuation - current_attenuation
correction = (error * relaxation) / (dist_m + 1e-6)
self.grid[idx_x, idx_y] += correction

```

* **Dynamisches SLAM:** Lolas Bewegungen ziehen Radarstrahlen durch den Raum. Exponentieller gleitender Mittelwert (EMA) kartografiert Möbel und Dämpfungen.

```python
measured_db_per_m = measured_attenuation / dist_m
self.grid[idx_x, idx_y] = (1 - alpha) * self.grid[idx_x, idx_y] + alpha * measured_db_per_m

```

### Stufe 4: Kinematischer AMCL Partikelfilter (`ParticleFilter`)

Löst das nicht-lineare Geometrieproblem. Er fungiert als "versteckte Beobachtung" für Stufe 5 und gibt einen unscharfen Punkt $\mu_k$ mit gemessener Ungenauigkeit $\Sigma_k$ aus.

* **Tomographie-Gewichtung & Huber Loss:** Die erwartete Distanz ($d_{app}$) eines Partikels wird durch die `RadioGridMap` künstlich verlängert, wenn eine "Wand" im Weg ist. Die Wahrscheinlichkeit ($w_i$) wird robust über eine Soft-L1-Funktion berechnet.

$$d_{app}=d_{true}\cdot 10^{\frac{A_{dB}}{10\cdot n\_factor}}$$


$$L=2\left(\sqrt{1+\left(\frac{d_{app}-z}{\sigma}\right)^2}-1\right)$$


$$w_i\propto\exp\left(-0.5\sum L\right)$$



```python
distance_multiplier = 10.0 ** (attenuation_db / (10.0 * n_factors_arr))
predicted_distances = predicted_distances * distance_multiplier

residuals = predicted_distances - distances[None, :]
normalized_residuals = residuals / sigmas[None, :]
loss = 2.0 * (np.sqrt(1.0 + normalized_residuals**2) - 1.0)
log_likelihood = -0.5 * np.sum(loss, axis=1)

```

### Stufe 5: IMM Constrained Kalman Filter (`IMMCKFilter`)

Als finale Kontrollinstanz löst der IMM-CKF den Zielkonflikt zwischen Rauschen (Jitter) und Verzögerung (Lag). Das System rechnet zwei physikalische 4D-Modelle ($x=[X, Y, \dot{X}, \dot{Y}]^T$) parallel: Modell 1 erwartet Stillstand ($Q\approx 0$), Modell 2 erwartet Bewegung ($Q\gg 0$). Die Ergebnisse des Partikelfilters fungieren als Messmatrix.

* **Wahrscheinlichkeits-Update (Likelihood):** Das System ermittelt bei jedem Tick, welches Modell besser zur neuen Partikelwolke passt, und mischt die Ergebnisse stochastisch.

$$\Lambda_i=\frac{1}{\sqrt{(2\pi)^2\det(S)}}\exp\left(-\frac{1}{2}y_{res}^T S^{-1}y_{res}\right)$$


$$\mu_i=\frac{\bar{c}_i\Lambda_i}{\sum\mu}$$


$$x_{final}=\sum\mu_i\cdot x_i$$



```python
# Likelihood Berechnung
det_S = max(np.linalg.det(S), 1e-9)
inv_S = np.linalg.inv(S)
exponent = -0.5 * np.dot(y_res.T, np.dot(inv_S, y_res))
likelihoods[i] = (1.0 / np.sqrt((2.0 * np.pi)**2 * det_S)) * np.exp(exponent)

# Probability Update & Combination
self.mu = c_bar * likelihoods
self.mu /= np.sum(self.mu)

x_comb = np.zeros(4)
for i in range(2):
    x_comb += self.mu[i] * self.x[i]

```