# TriLola – Modell & Parameter

<img src="images/lola-icon.png" width="72" align="right" alt="Lola">

Wie TriLola aus Signalstärken eine Position macht – und was jede Stellschraube im
[Filter-Feintuning](BEDIENUNG.md#filter-feintuning) mathematisch bewirkt. Beschrieben ist das
Standardmodell **`pf`** (Partikelfilter); das ältere Modell `legacy` steht kurz am Ende.

**Inhalt**

0. [Die Idee in fünf Sätzen](#0-die-idee-in-fünf-sätzen)
1. [Zustand und Ablauf](#1-zustand-und-ablauf)
2. [Geometrie: schräger Abstand](#2-geometrie-schräger-abstand)
3. [Messmodell](#3-messmodell)
4. [Bewegungsmodell](#4-bewegungsmodell)
5. [Resampling und Wiederfinden](#5-resampling-und-wiederfinden)
6. [Schätzung und Ausgabe](#6-schätzung-und-ausgabe)
7. [Mesh: Drift und Funkqualität](#7-mesh-drift-und-funkqualität)
8. [Funkkarte und Kalibrierung](#8-funkkarte-und-kalibrierung)
9. [Parameterübersicht](#9-parameterübersicht)
10. [Das Modell `legacy`](#10-das-modell-legacy)

---

## 0. Die Idee in fünf Sätzen

Je weiter das Halsband von einem Sensor entfernt ist, desto schwächer kommt sein Signal an – aber
stark verrauscht, durch Wände, Möbel, Körper und Reflexionen. Statt aus jedem Messwert eine
Entfernung zu „rechnen“, hält TriLola **viele Hypothesen** gleichzeitig: 1500 Partikel, jeder eine
mögliche Position von Lola mit Geschwindigkeit und dem Zustand „ruht“ oder „läuft“. Zwischen zwei
Messungen bewegen sich die Partikel so, wie eine Katze sich bewegen könnte (nicht durch Wände).
Trifft eine Messung ein, bekommt jeder Partikel ein Gewicht danach, wie gut die Messung zu seiner
Position passt. Der gewichtete Schwerpunkt ist Lolas Position, die Streuung der Wolke ihre Genauigkeit.

---

## 1. Zustand und Ablauf

Jeder Partikel $`i = 1 \dots N`$ trägt den Zustand

```math
\mathbf{s}_i = (x_i,\ y_i,\ v_{x,i},\ v_{y,i},\ m_i), \qquad m_i \in \{\text{Ruhe},\ \text{Bewegung}\}
```

und ein Gewicht $`w_i`$ mit $`\sum_i w_i = 1`$. Positionen sind lokale Koordinaten in cm (die
Oberfläche zeigt sie relativ zum Referenzgerät).

Jede Sensor-Nachricht wird **genau einmal und in Zeitreihenfolge** verarbeitet – keine Messung
zählt doppelt, und es gibt keine „Momentaufnahmen“ mit unterschiedlich alten Werten:

1. **Vorhersage** – alle Partikel bis zum Zeitpunkt der Nachricht bewegen ([§4](#4-bewegungsmodell)).
2. **Messung** – Gewichte mit der Likelihood der Nachricht multiplizieren ([§3](#3-messmodell)):
   $`w_i \leftarrow w_i \cdot p(z \mid \mathbf{s}_i)^{\beta}`$, dann normieren.
3. **Resampling** – bei Bedarf neu ziehen und ggf. frische Partikel einstreuen ([§5](#5-resampling-und-wiederfinden)).
4. **Schätzung** – Position, Raum, Genauigkeit, Bewegung ([§6](#6-schätzung-und-ausgabe)).

**Start:** Die erste Sichtung verteilt alle Partikel gleichmäßig über die gezeichneten Räume
(ohne Grundriss: über das Rechteck um alle Sensoren plus Rand); 20 % beginnen „in Bewegung“.
Aus reinen „nicht gesehen“-Meldungen wird nie gestartet.

---

## 2. Geometrie: schräger Abstand

Sensorantenne und Halsband liegen auf verschiedenen Höhen. Für Sensor $`j`$ an der Stelle
$`\mathbf{p}_j`$ mit Antennenhöhe $`h_j`$ und das Halsband auf Höhe $`h_T`$ gilt

```math
d_j(\mathbf{x}) = \sqrt{\lVert \mathbf{x} - \mathbf{p}_j \rVert^2 + (h_j - h_T)^2}
```

* $`h_j`$ = **Höhe über Fußboden** des Geräts (Standard Shelly 105 cm, sonst 100 cm). Steht es auf
  einem anderen Stockwerk, kommt der Höhenunterschied der Fußböden dazu:
  $`h_j = h_{\text{Gerät}} + (f_{\text{Gerät}} - f_{\text{Wohnung}})`$.
* $`h_T`$ = **Halsbandhöhe über dem Fußboden** (`TAG_HEIGHT_CM`, Standard 25 cm).
* Wirkung: Direkt unter einem Sensor auf 1,05 m ist Lola nicht 0 m, sondern 0,8 m entfernt.
  Ohne diese Korrektur hält das Modell sie in Sensornähe für „zu weit weg“.
* Sensoren, deren Kalibrierung noch ohne Höhen berechnet wurde, rechnen eben ($`h_j = h_T`$), bis sie
  neu kalibriert sind – sonst passten Kalibrierwerte und Geometrie nicht zusammen.

---

## 3. Messmodell

Ein Sensor meldet alle 2 s den **Median** $`z`$ der RSSI-Werte (dBm), die er in diesem Fenster vom
Halsband empfangen hat, und deren Anzahl $`k`$ – oder „nicht gesehen“.

### 3.1 Erwarteter Pegel

Das Log-Distanz-Modell mit Wänden und Empfängerdrift:

```math
\mu_j(\mathbf{x}) = P_{0,j} - 10\, n_j \log_{10}\!\frac{\max(d_j(\mathbf{x}),\ 0{,}3\,\text{m})}{1\,\text{m}} - W_j(\mathbf{x}) + b_j
```

| Symbol | Bedeutung | Herkunft |
|---|---|---|
| $`P_{0,j}`$ | Pegel in 1 m Abstand (`tx_power`) | Kalibrierung je Sensor |
| $`n_j`$ | Pfadverlust-Exponent (`n_factor`); 2 = Freiraum, 2,5–3,5 = Wohnung | Kalibrierung |
| $`W_j(\mathbf{x})`$ | Summe der Dämpfungen aller Wände zwischen $`\mathbf{x}`$ und Sensor (dB) | Grundriss, skaliert durch die Kalibrierung |
| $`b_j`$ | Empfängerdrift des Sensors (dB) | Mesh, [§7](#7-mesh-drift-und-funkqualität) |

### 3.2 Streuung

Zwei Rauschquellen addieren sich:

```math
\sigma_j^2 = \sigma_{\text{sh},j}^2 + c_k\, \frac{\sigma_{\text{f},j}^2(\mu)}{k}, \qquad
c_k = \begin{cases} 1{,}57 & k > 1 \\ 1 & k = 1 \end{cases}, \qquad k \le 4
```

* **Shadowing** $`\sigma_{\text{sh}}`$ (`sigma_db`): ortsfeste Abweichung vom Modell – Möbel, Körper,
  Reflexionen. Mittelt sich *nicht* weg, auch nicht über viele Messungen am selben Ort.
* **Fading** $`\sigma_{\text{f}}^2`$: schnelles Zittern einzelner Werte. Seine Varianz steigt linear
  mit sinkendem erwarteten Pegel – von `r_min` bei $`\mu = P_0`$ (nah) bis `r_max` bei −110 dBm
  (`rssi_limit`, fern), mindestens 0,25 dB². Der Median aus $`k`$ Werten hat etwa die Varianz
  $`1{,}57\,\sigma_{\text{f}}^2/k`$.
* **Funkqualität** $`q_j \in [0{,}1;\ 1]`$ aus dem Mesh ([§7](#7-mesh-drift-und-funkqualität))
  weitet die Streuung auf: $`\sigma_j \leftarrow \sigma_j / \sqrt{q_j}`$. Ein gestörter Sensor zählt weniger.

### 3.3 Robuste Likelihood: „gesehen“

Statt einer Normalverteilung nutzt TriLola eine **Student-t-Verteilung** mit $`\nu`$ Freiheitsgraden
(*Ausreißer-Toleranz*):

```math
\log p(z \mid \mathbf{x}) = -\frac{\nu + 1}{2}\, \log\!\left(1 + \frac{r^2}{\nu}\right) - \log \sigma_j,
\qquad r = \frac{z - \mu_j(\mathbf{x})}{\sigma_j}
```

Kleines $`\nu`$ hat „schwere Ränder“: Ein einzelner verrückter Wert (Reflexion, Körper im Weg) kostet
einen Partikel wenig. Großes $`\nu`$ nähert die Normalverteilung an, bei der jeder Wert voll zählt.

### 3.4 „Nicht gesehen“ ist auch eine Information

Meldet ein Sensor „nicht gesehen“, ist Lola vermutlich weit weg – aber nicht sicher, denn auch
nahe Sensoren verpassen das Halsband manchmal. Mit der Empfangsschwelle $`F_j`$
(`detection_floor`, Standard −97 dBm) und der Normalverteilungsfunktion $`\Phi`$:

```math
P(\text{nicht gesehen} \mid \mathbf{x}) = p_0 + (1 - p_0)\; \Phi\!\left(\frac{F_j - \mu_j(\mathbf{x})}{\sigma_{\text{miss},j}}\right),
\qquad \sigma_{\text{miss},j} = \sqrt{\sigma_{\text{sh},j}^2 + 9\,\text{dB}^2}\,/\sqrt{q_j}
```

$`p_0`$ ist die Grundwahrscheinlichkeit, das Halsband trotz Nähe zu verpassen
(*„Nicht gesehen“ trotz Nähe*). Große $`p_0`$ machen „nicht gesehen“ fast bedeutungslos.

### 3.5 Gedächtnis je Sensor

Zwei Meldungen desselben Sensors kurz hintereinander sind nicht unabhängig – das Shadowing am
selben Ort ist dasselbe. Ohne Korrektur würde ein Sensor, der oft meldet, die Position „überstimmen“.
Deshalb wird die Likelihood abgeschwächt (getempert):

```math
p(z \mid \mathbf{x})^{\beta}, \qquad \beta = \min\!\left(1,\ \max\!\left(0{,}2,\ \frac{\Delta t}{\tau_c}\right)\right)
```

$`\Delta t`$ ist die Zeit seit der letzten Meldung *dieses* Sensors, $`\tau_c`$ das *Gedächtnis je Sensor*.
$`\tau_c = 0`$ schaltet das ab ($`\beta = 1`$).

---

## 4. Bewegungsmodell

Zwischen zwei Messungen wird in Schritten von etwa 1 s vorhergesagt (höchstens 10 Schritte;
nach längeren Pausen werden die Schritte entsprechend länger).

**Ruhe ↔ Bewegung** (Jump-Markov): In jedem Schritt wechselt ein Partikel mit

```math
P(\text{Ruhe} \to \text{Bewegung}) = 1 - e^{-h/T_R}, \qquad
P(\text{Bewegung} \to \text{Ruhe}) = 1 - e^{-h/T_M}
```

$`T_R`$ = *Mittlere Ruhedauer*, $`T_M`$ = *Mittlere Laufdauer*. Wer loslaufen will, bekommt eine
zufällige Geschwindigkeit $`\mathbf{v} \sim \mathcal{N}(0, \sigma_v^2 \mathbf{I})`$; in Ruhe ist $`\mathbf{v} = 0`$.

**Geschwindigkeit** (Ornstein-Uhlenbeck-Prozess): Laufende Partikel behalten ihre Richtung eine
Weile und ändern sie dann allmählich:

```math
\mathbf{v} \leftarrow e^{-h/\tau}\, \mathbf{v} + \sigma_v \sqrt{1 - e^{-2h/\tau}}\; \xi,
\qquad \lVert \mathbf{v} \rVert \le v_{\max}
```

$`\sigma_v`$ = *Typische Laufgeschwindigkeit* (Streuung der Geschwindigkeit im Dauerlauf),
$`\tau`$ = *Richtungsbeständigkeit*, $`v_{\max}`$ = *Höchstgeschwindigkeit*, $`\xi`$ standardnormal.

**Position:**

```math
\mathbf{x} \leftarrow \mathbf{x} + \mathbf{v}\, h + D\sqrt{h}\; \xi, \qquad
D = \begin{cases} D_R & \text{Ruhe} \\ \sqrt{D_R^2 + D_M^2} & \text{Bewegung} \end{cases}
```

$`D_R`$ = *Unruhe in Ruhe*, $`D_M`$ = *Zusätzliche Unruhe in Bewegung* (beide in cm/√s). Die Unruhe
lässt die Wolke langsam „atmen“, damit sie sich neuen Messungen anpassen kann.

**Wände:** Kreuzt ein Schritt eine Wand, durch die „die Katze nicht durchkommt“, bleibt der Partikel
stehen und prallt ab ($`\mathbf{v} \leftarrow -0{,}3\,\mathbf{v}`$). Türen und durchlässige Wände sperren nicht.
Alle Partikel bleiben im Rechteck um den Grundriss (+50 cm).

---

## 5. Resampling und Wiederfinden

**Resampling:** Sammelt sich das Gewicht auf wenige Partikel – die effektive Partikelzahl
$`N_{\text{eff}} = 1/\sum_i w_i^2`$ fällt unter $`N/2`$ –, wird systematisch neu gezogen: gute Partikel
vervielfältigen sich, schlechte verschwinden. Jeder neue Partikel wird um ~3 cm verrauscht, damit
keine Kopien übereinanderliegen.

**Wiederfinden** (adaptiv, nach dem AMCL-Prinzip): TriLola beobachtet, wie gut die Messungen im
Mittel zur Wolke passen – einmal langsam, einmal schnell gemittelt:

```math
\ell = \log p(z \mid z_{1:t-1}), \qquad
s_{\text{slow}} \mathrel{+}= \alpha_{\text{slow}}(\ell - s_{\text{slow}}), \qquad
s_{\text{fast}} \mathrel{+}= \alpha_{\text{fast}}(\ell - s_{\text{fast}})
```

Passen die jüngsten Messungen deutlich schlechter als üblich ($`s_{\text{fast}} < s_{\text{slow}}`$),
ist die Wolke vermutlich am falschen Ort. Dann wird ein Anteil

```math
\rho = \min\!\left(\max\!\left(0,\ 1 - e^{\,s_{\text{fast}} - s_{\text{slow}}}\right),\ \rho_{\max}\right)
```

der Partikel ersetzt, sobald er über 2 % liegt ($`\rho_{\max}`$ = *Wiederfinden: max. Anteil neuer
Partikel*). Kam gerade eine Sichtung, landet die Hälfte auf einem Ring um den meldenden Sensor – der
Radius aus dem umgekehrten Pegelmodell, $`10^{(P_0 - z)/(10n)}`$ m, auf den Boden projiziert – und die
andere Hälfte gleichmäßig in den Räumen; sonst alle gleichmäßig in den Räumen.

**Neustart nach Pause:** Kommt nach mehr als *Neustart nach Pause* Sekunden ohne Sichtung wieder
eine, startet der Filter neu wie in [§1](#1-zustand-und-ablauf) – Lola kann in der Zwischenzeit überall sein.

---

## 6. Schätzung und Ausgabe

**Position** ist der gewichtete Schwerpunkt $`\hat{\mathbf{x}} = \sum_i w_i \mathbf{x}_i`$.

**Raum:** $`P(\text{Raum } R) = \sum_{i:\ \mathbf{x}_i \in R} w_i`$. Der wahrscheinlichste Raum ist
„Lola Raum“. Liegt er über 50 %, der Schwerpunkt aber außerhalb (z. B. mitten in einer Wand
zwischen zwei Wolken), wird nur über die Partikel in diesem Raum gemittelt.

**Genauigkeit:** $`\sqrt{\mathrm{tr}\,\Sigma}`$ der gewichteten Partikel-Kovarianz, mindestens 30 cm.

**Bewegung im Filter:** $`P(\text{Bewegung}) = \sum_{i:\ m_i = \text{Bewegung}} w_i`$.

**Glättung in Ruhe** (optional, $`\tau_{\text{out}}`$ = *Glättung der Anzeige in Ruhe*): Ist
$`P(\text{Bewegung}) < 0{,}3`$, folgt die angezeigte Position exponentiell geglättet:
$`\hat{\mathbf{x}} \leftarrow \hat{\mathbf{x}}_{\text{alt}} + (1 - e^{-\Delta t/\tau_{\text{out}}})(\hat{\mathbf{x}} - \hat{\mathbf{x}}_{\text{alt}})`$.

**Wann gilt Lola als „weg“?** Zwei Bedingungen, beide nur für die Ausgabe (der Filter läuft weiter):

1. **Kein aktueller Messwert:** Ein Sensor zählt als „sieht sie“, wenn seine letzte Meldung eine
   Sichtung war und höchstens *Messwert gilt als aktuell* Sekunden alt ist. Sieht sie keiner,
   ist sie außer Reichweite. Sensoren melden „nicht gesehen“ selbst nach 10 s ohne Empfang.
2. **Nur noch schwache Sichtungen:** Hat seit *„Weg“ melden nach* Sekunden kein Sensor sie mit
   mindestens *Mindestsignal für „zu Hause“* gesehen, gilt sie ebenfalls als weg – auch wenn ein
   Sensor sie z. B. draußen vor dem Fenster noch schwach hört. Standard −100 dBm: jede Sichtung zählt.

Dann meldet Home Assistant `not_home` und „außer Reichweite“.

**„Lola in Bewegung“** (der HA-Sensor): an, wenn die angezeigte Position sich im Fenster von 4 s
schneller als *„In Bewegung“ ab* bewegt; bleibt danach 5 s an.

**Meldungen an Home Assistant** gehen höchstens alle *Home Assistant: höchstens alle* Sekunden
raus, und nur wenn sich die Position um mindestens *erst ab Bewegung von* cm (oder die Genauigkeit)
geändert hat – spätestens aber jede Minute. Die Karte der Oberfläche bekommt davon unabhängig
etwa einmal pro Sekunde Live-Daten.

---

## 7. Mesh: Drift und Funkqualität

Die Sensoren senden selbst kleine Bluetooth-Signale und hören sich gegenseitig. Für jede gerichtete
Strecke „Empfänger $`r`$ ← Sender $`t`$“ lernt der Tracker einmalig einen **Normalwert** $`B_{rt}`$
(aus 30 Messungen). Später zerlegt er die Abweichung aller Strecken gemeinsam in einen Sender- und
einen Empfängeranteil:

```math
R_{rt} - B_{rt} = a_t + b_r + \varepsilon_{rt}
```

gelöst als robuste Ridge-Regression (Huber-Gewichte ab 4 dB, Ridge 0,5). Ein Gleichtakt – alle
Empfänger scheinbar gleichzeitig schlechter – wird entfernt, weil er physikalisch nicht von einer
Umgebungsänderung zu unterscheiden ist. Ergebnisse:

* **Empfängerdrift $`b_r`$** (höchstens ±8 dB, nur mit mindestens zwei Strecken): Hört ein Sensor
  plötzlich 3 dB schlechter (Gehäuse bewegt, Firmware, Temperatur), korrigiert $`b_r`$ seine
  Halsband-Messungen. Für das Halsband zählt nur die Empfängerseite – es sendet, der Sensor empfängt.
* **Funkqualität $`q`$:** Was die Drift nicht erklärt (eine Person steht im Funkweg), senkt die
  Qualität der beteiligten Sensoren: $`q = \mathrm{Median}\big(e^{-z^2/2}\big)`$ mit
  $`z = \varepsilon / \max(2\sigma_{\text{Strecke}},\ 5\,\text{dB})`$, begrenzt auf $`[0{,}1;\ 1]`$.
* **Plausibilität:** Driften mehr als die Hälfte der Sensoren (mindestens drei) um über 4 dB, passt
  eher der Normalwert nicht mehr (Umbau, neue Firmware). Dann wird nichts korrigiert, und
  **Mesh-Baseline neu lernen** ist fällig.

---

## 8. Funkkarte und Kalibrierung

### 8.1 Funkkarte (Radio Tomographic Imaging)

Der Bereich der Sensoren (plus 1,5 m Rand) wird in Zellen von 50 cm zerlegt – bei großen Flächen
gröber, höchstens 900 Zellen. Jede Strecke „sieht“ die Zellen in einer schmalen
Ellipse um ihre Verbindungslinie; die Matrix $`\mathbf{W}`$ verteilt die Streckenlänge (m) gleichmäßig
auf diese Zellen. Gesucht ist die Dämpfung $`\mathbf{x}`$ je Zelle in dB/m:

```math
\min_{0\, \le\, \mathbf{x}\, \le\, 20}\ \lVert \mathbf{W}\mathbf{x} - \mathbf{y} \rVert^2 + 0{,}02\,\lVert \mathbf{x} \rVert^2 + 0{,}15\,\lVert \mathbf{D}\mathbf{x} \rVert^2
```

$`\mathbf{D}`$ bildet Differenzen benachbarter Zellen – die Karte bleibt glatt.

* **Veränderungen:** $`\mathbf{y}`$ = aktuelle Abweichung jeder Strecke vom Normalwert, bereinigt um
  $`a_t + b_r`$; neu gerechnet alle 10 s. Steigt eine Zelle, holt die Karte mindestens die Hälfte des
  Sprungs je Durchgang auf; fällt sie, klingt sie mit $`1 - e^{-\Delta t/\tau_{\text{RTI}}}`$ ab
  ($`\tau_{\text{RTI}}`$ = *Funkkarte: Nachleuchten von Veränderungen*).
* **Wände & Hindernisse:** $`\mathbf{y}`$ = wie viel mehr jede Strecke dauerhaft verliert als ein
  Modell ohne Hindernisse. Ohne gezeichnete Wände ist das das Freiraummodell
  $`B_{rt} = A_t + G_r - 10\,n_0 \log_{10} d_{rt}`$ mit $`n_0 \approx 2`$. Mit Wänden werden
  Gewinne und Exponent aus dem Modell *mit* Wänden geschätzt ([§8.3](#83-autokalibrierung-aus-dem-mesh)),
  und die Karte zeigt Wand- plus Restdämpfung (etwas stärker geglättet: 0,3 statt 0,15).

### 8.2 Kalibrierung mit dem Halsband

Alle Messpunkte $`k`$ aller Sensoren $`j`$ werden gemeinsam gefittet:

```math
\bar z_{jk} = P_{0,j} - 10\, n \log_{10} d_{jk} - s \cdot A_{jk} + \varepsilon_{jk}
```

* $`\bar z_{jk}`$ = robuster Mittelwert am Punkt (Ausreißer über 3 MAD verworfen), $`d_{jk}`$ = schräger
  Abstand ([§2](#2-geometrie-schräger-abstand)), $`A_{jk}`$ = Wanddämpfung laut Grundriss,
  $`s`$ = Korrekturfaktor für alle Wanddämpfungen (0–3).
* Robust gelöst (Soft-L1-Verlust, Skala 3 dB) mit $`P_0 \in [-110;\ -20]`$ dBm, $`n \in [1;\ 5]`$.
* Ergebnis je Sensor: `tx_power` = $`P_{0,j}`$, `n_factor` = $`n`$, `sigma_db` = Streuung der Reste
  (bei wenigen Punkten zur Gesamtstreuung hin gezogen, mindestens 2 dB), `r_min`/`r_max` = Varianz
  der Einzelwerte nah (≤ 2 m) bzw. fern (≥ 4 m).

### 8.3 Autokalibrierung aus dem Mesh

Dasselbe Modell auf den Mesh-Normalwerten, mit freiem Exponenten und je einer Dämpfung $`L_w`$
pro gezeichneter Wand:

```math
B_{rt} = A_t + G_r - 10\, n \log_{10} d_{rt} - \sum_w c_{rtw} L_w + \varepsilon
```

$`c_{rtw} = 1`$, wenn die Strecke die Wand kreuzt. $`G_r`$ ist die relative Empfangsstärke jedes
Sensors und wird zum Vorschlag für `tx_power`. Das absolute Niveau kann das Mesh nicht sehen (das
Halsband ist ein anderer Sender); es wird an mindestens zwei bereits mit dem Halsband kalibrierten
Sensoren ausgerichtet, sonst am Median der bisherigen Werte.
Schwach bestimmte Größen halten Gauß-Priors nahe an plausiblen Startwerten.

---

## 9. Parameterübersicht

**Legende:** *In der App* = Bezeichnung unter *Einstellungen → Filter-Feintuning*.
Schlüssel = Name in `config/tuning.json`, per MQTT und in `secrets_tri.py`.
↑ = was passiert, wenn du den Wert erhöhst; ↓ = wenn du ihn senkst.

### 9.1 Bewegung

| In der App | Schlüssel · Symbol | Standard (Bereich) | Wirkung im Modell | ↑ größer · ↓ kleiner |
|---|---|---|---|---|
| **Typische Laufgeschwindigkeit** | `PF_MOVE_SPEED_CM_S` · $`\sigma_v`$ | 90 cm/s (20–300) | Streuung der Geschwindigkeit laufender Partikel ([§4](#4-bewegungsmodell)) | ↑ folgt schnellen Sprints und Raumwechseln besser, springt aber leichter · ↓ ruhigere Bahnen, hinkt beim Rennen hinterher |
| **Richtungsbeständigkeit** | `PF_MOVE_TAU_SEC` · $`\tau`$ | 2 s (0,5–10) | wie lange die Laufrichtung erhalten bleibt | ↑ glatte, zielstrebige Bahnen, träge bei Haken · ↓ zickzack, reagiert schnell auf Richtungswechsel |
| **Unruhe in Ruhe** | `PF_REST_DIFFUSION_CM` · $`D_R`$ | 4 cm/√s (0–20) | zufälliges Wandern ruhender Partikel | ↑ passt sich neuen Messungen schneller an, Anzeige zittert mehr · ↓ ruhige Anzeige beim Schlafen, übersieht kleine echte Ortswechsel |
| **Zusätzliche Unruhe in Bewegung** | `PF_MOVE_DIFFUSION_CM` · $`D_M`$ | 15 cm/√s (0–60) | zufällige Abweichung zusätzlich zur Laufbewegung | ↑ robuster bei unvorhersehbaren Wegen, ungenauer · ↓ strenger an der Laufrichtung |
| **Mittlere Ruhedauer (Modell)** | `PF_MEAN_REST_SEC` · $`T_R`$ | 10 s (2–300) | mittlere Zeit, bis ein ruhender Partikel losläuft | ↑ bleibt eher liegen, bemerkt Aufbrechen später · ↓ rechnet ständig mit Aufbruch, Wolke weiter |
| **Mittlere Laufdauer (Modell)** | `PF_MEAN_MOVE_SEC` · $`T_M`$ | 10 s (2–120) | mittlere Dauer einer Laufphase | ↑ lange Wege möglich, beruhigt sich langsamer · ↓ kurze Wege, zur Ruhe kommt die Wolke schneller |
| **Höchstgeschwindigkeit** | `MAX_POSITION_SPEED_CM_S` · $`v_{\max}`$ | 350 cm/s (100–800) | harte Obergrenze der Partikelgeschwindigkeit | ↑ lässt wilde Sprints zu, auch Fehlsprünge · ↓ verhindert Teleportation, zu niedrig = hinkt echten Sprints nach |

> Warum ist die mittlere Ruhedauer so kurz, obwohl Katzen lange schlafen? $`T_R`$ ist eine
> Modellannahme für den Filter, keine Statistik: Mit kurzer Annahme bemerkt der Filter das
> Aufbrechen schnell. Die Messungen halten eine schlafende Lola trotzdem am Ort.

### 9.2 Anwesenheit

| In der App | Schlüssel | Standard (Bereich) | Wirkung | ↑ größer · ↓ kleiner |
|---|---|---|---|---|
| **„Weg“ melden nach** | `PRESENCE_LOST_SEC` | 30 s (5–600) | Wartezeit ohne ausreichend starke Sichtung, bis Lola als „außer Reichweite“ gilt ([§6](#6-schätzung-und-ausgabe)) | ↑ weniger falsches „weg“, verschwindet später · ↓ verschwindet schneller, kann in Funklöchern flackern |
| **Mindestsignal für „zu Hause“** | `PRESENCE_MIN_RSSI_DBM` | −100 dBm (−110 bis −50) | nur Sichtungen mindestens so stark halten Lola „anwesend“ | ↑ (z. B. −90) schwache Sichtungen von draußen zählen nicht mehr; zu hoch → gilt in entfernten Ecken als weg · ↓ jede Sichtung zählt |

### 9.3 Messmodell

| In der App | Schlüssel · Symbol | Standard (Bereich) | Wirkung im Modell | ↑ größer · ↓ kleiner |
|---|---|---|---|---|
| **Halsbandhöhe über dem Fußboden** | `TAG_HEIGHT_CM` · $`h_T`$ | 25 cm (0–200) | Höhe im schrägen Abstand ([§2](#2-geometrie-schräger-abstand)) | an die Katze anpassen: stehend ~25 cm, liegend ~10 cm; wirkt vor allem nahe an hoch montierten Sensoren |
| **Ausreißer-Toleranz** | `PF_STUDENT_NU` · $`\nu`$ | 4 (1–30) | Form der Likelihood ([§3.3](#33-robuste-likelihood-gesehen)) | ↑ Normalverteilung: jeder Wert zählt voll, genauer bei sauberen Daten, anfällig für Ausreißer · ↓ robust, einzelne verrückte Werte schaden kaum, reagiert zögerlicher |
| **Gedächtnis je Sensor** | `PF_SAME_SENSOR_CORRELATION_SEC` · $`\tau_c`$ | 6 s (0–30) | Abschwächung schneller Folgemeldungen ([§3.5](#35-gedächtnis-je-sensor)) | ↑ ein oft meldender Sensor dominiert weniger, Anzeige reagiert langsamer · ↓ jede Meldung zählt voll, schneller, aber übermütig |
| **„Nicht gesehen“ trotz Nähe** | `PF_MISS_BASE_PROB` · $`p_0`$ | 0,10 (0–0,5) | Grundrate verpasster Sichtungen ([§3.4](#34-nicht-gesehen-ist-auch-eine-information)) | ↑ „nicht gesehen“ schiebt Lola kaum weg (gut bei unzuverlässigen Sensoren) · ↓ „nicht gesehen“ wirkt stark, Lola wird von schweigenden Sensoren weggedrückt |
| **Messwert gilt als aktuell** | `SENSOR_TIMEOUT_SEC` | 30 s (5–120) | Höchstalter einer Sichtung für „sieht sie“ ([§6](#6-schätzung-und-ausgabe)) | ↑ übersteht Sensor-Aussetzer, meldet „weg“ später · ↓ strenger; wirkt vor allem, wenn ein Sensor ganz verstummt |

### 9.4 Robustheit

| In der App | Schlüssel · Symbol | Standard (Bereich) | Wirkung im Modell | ↑ größer · ↓ kleiner |
|---|---|---|---|---|
| **Anzahl Partikel** | `PF_PARTICLES` · $`N`$ | 1500 (300–5000) | Zahl der Hypothesen; eine Änderung setzt den Filter zurück | ↑ genauer, stabiler, mehrere Wolken gleichzeitig möglich, mehr Rechenzeit · ↓ schneller (Pi Zero: ≤ 800), gröber, verliert Lola leichter |
| **Wiederfinden: max. Anteil neuer Partikel** | `PF_MAX_INJECT` · $`\rho_{\max}`$ | 0,20 (0–0,5) | Obergrenze der eingestreuten Partikel ([§5](#5-resampling-und-wiederfinden)) | ↑ findet sie nach Fehlern schneller wieder, springt leichter · ↓ stabil, bleibt aber länger am falschen Ort hängen; 0 = aus |
| **Neustart nach Pause** | `PF_TRACK_RESET_SEC` | 600 s (30–3600) | nach so langer Zeit ohne Sichtung beginnt die Suche neu | ↑ knüpft nach Pausen an den alten Ort an · ↓ nach kurzer Abwesenheit sucht der Filter unvoreingenommen neu |

### 9.5 Ausgabe

| In der App | Schlüssel · Symbol | Standard (Bereich) | Wirkung | ↑ größer · ↓ kleiner |
|---|---|---|---|---|
| **Glättung der Anzeige in Ruhe** | `PF_OUTPUT_SMOOTHING_SEC` · $`\tau_{\text{out}}`$ | 0 s (0–60) | exponentielle Glättung, nur wenn $`P(\text{Bewegung}) < 0{,}3`$ | ↑ ruhige Anzeige beim Schlafen, kleine echte Ortswechsel erscheinen verzögert · 0 = aus |
| **Home Assistant: höchstens alle** | `PUBLISH_INTERVAL_SEC` | 1 s (0,5–30) | Mindestabstand zweier Positionsmeldungen an HA | ↑ weniger Einträge in der HA-Historie, HA-Karte träger · ↓ flüssiger, mehr Datenbank |
| **Home Assistant: erst ab Bewegung von** | `PUBLISH_MIN_MOVE_CM` | 5 cm (0–200) | kleinere Änderungen werden nicht gemeldet | ↑ ruhige HA-Historie · ↓ jede Kleinigkeit landet in HA |
| **„In Bewegung“ ab** | `MOVING_SPEED_CM_S` | 30 cm/s (5–150) | Schwelle für den HA-Sensor „Lola in Bewegung“ | ↑ nur echtes Laufen zählt · ↓ schaltet schon bei Zittern der Position |
| **Funkkarte: Nachleuchten von Veränderungen** | `RADIO_DYNAMIC_TAU_SEC` · $`\tau_{\text{RTI}}`$ | 120 s (10–900) | Abklingzeit der Karte „Veränderungen“ ([§8.1](#81-funkkarte-radio-tomographic-imaging)) | ↑ Störungen bleiben länger sichtbar (Spur) · ↓ zeigt nur, was gerade stört |

### 9.6 Kalibrierwerte je Sensor

Diese Werte setzt die [Kalibrierung](BEDIENUNG.md#6-kalibrieren); in Home Assistant stehen sie als
„*Sensor* Kalibrierung …“. Von Hand sollte man sie selten ändern.

| Wert | Symbol | Standard | Bedeutung | ↑ größer · ↓ kleiner |
|---|---|---|---|---|
| `tx_power` | $`P_{0,j}`$ | −59 dBm | Pegel des Halsbands in 1 m am Sensor | ↑ Modell erwartet mehr Signal → hält Lola für weiter weg · ↓ näher |
| `n_factor` | $`n_j`$ | 3,0 | wie schnell der Pegel mit der Entfernung fällt | ↑ Entfernungen wirken kürzer · ↓ länger |
| `sigma_db` | $`\sigma_{\text{sh},j}`$ | 4 dB | Shadowing: wie sehr man dem Sensor traut | ↑ Sensor zählt weniger · ↓ mehr |
| `r_min` / `r_max` | $`\sigma_{\text{f}}^2`$ | 5 / 20 dB² | Fading-Varianz einzelner Werte nah / fern | ↑ Einzelwerte zählen weniger |
| `detection_floor` | $`F_j`$ | −97 dBm | Empfangsschwelle für „nicht gesehen“ | ↑ „nicht gesehen“ erlaubt auch mittlere Entfernungen · ↓ bedeutet „sehr weit weg“ |
| Höhe über Fußboden | $`h_j`$ | 105 / 100 cm | Antennenhöhe ([§2](#2-geometrie-schräger-abstand)) | – |

### 9.7 Für Experten (nur in `secrets_tri.py`)

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `PF_MAX_SAMPLE_COUNT` | 4 | Obergrenze für $`k`$ in [§3.2](#32-streuung) |
| `PF_RESAMPLE_ESS` | 0,5 | Resampling, sobald $`N_{\text{eff}} <`$ Anteil · $`N`$ |
| `PF_ALPHA_SLOW`, `PF_ALPHA_FAST` | 0,005 / 0,1 | Mittelungsraten $`\alpha`$ für das Wiederfinden |
| `PF_ROUGHEN_CM` | 3 | Verrauschen nach dem Resampling |
| `PF_ACCURACY_FLOOR_CM` | 30 | kleinste gemeldete Genauigkeit |
| `PF_BOUNDS_MARGIN_CM` | 300 | Rand um die Sensoren, wenn kein Grundriss da ist |
| `OFFLINE_TIMEOUT_SEC` | 90 | ab wann ein stummer Sensor als offline gilt |
| `RADIO_BASELINE_LEARNING_SAMPLES` | 30 | Messungen je Strecke für den Mesh-Normalwert |
| `RADIO_LINK_TIMEOUT_SEC` | 30 | Höchstalter einer Mesh-Messung |
| `RADIO_MAP_CELL_CM`, `RADIO_MAP_INTERVAL_SEC` | 50 / 10 | Rastergröße und Rechenintervall der Funkkarte |
| `PUBLISH_HEARTBEAT_SEC` | 60 | spätestens so oft eine Meldung an HA |
| `MOVING_WINDOW_SEC`, `MOVING_HOLD_SEC` | 4 / 5 | Fenster und Nachlaufzeit für „Lola in Bewegung“ |
| `LIVE_INTERVAL_SEC`, `LIVE_CLOUD_POINTS` | 1 / 120 | Live-Daten für die Karte: Takt und Punkte der Aufenthaltswolke |

---

## 10. Das Modell `legacy`

Das ältere Modell, in Home Assistant unter **TriLola Modell** umschaltbar – nützlich zum Vergleich.
Es rechnet in Stufen statt in einem Filter:

1. je Sensor ein Median-of-3-Vorfilter und ein 1D-Kalman-Filter auf dem Pegel,
2. Umrechnung in eine Entfernung (mit derselben Höhenkorrektur, [§2](#2-geometrie-schräger-abstand)),
3. ein Locator aus allen Entfernungen, danach ein Partikelfilter auf neuen Messungen,
4. ein IMM-Filter mit echtem Stillstandsmodell für Ruhe und Bewegung.

Aus dem Grundriss nutzt es nur die Räume (für „Lola Raum“); Wände wirken weder auf den Funk noch
auf die Bewegung. Von den Stellschrauben wirken nur die mit
„pf,legacy“ markierten: Halsbandhöhe, Messwert gilt als aktuell, Anwesenheit und die Ausgabe-Werte
außer der *Glättung der Anzeige in Ruhe*.
Im Simulator (`tools/ab_compare.py`) und auf echten Aufzeichnungen (`tools/replay.py`) lassen
sich beide Modelle vergleichen – siehe [Technik-Referenz](TECHNIK.md#7-werkzeuge).
