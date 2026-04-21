# KX-Bridge – Anycubic Kobra X Moonraker Bridge

**Version:** 0.9.1-beta4  
**Status:** Public Beta – für Heimanwender geeignet, Feedback willkommen

KX-Bridge ist eine Moonraker-kompatible HTTP/WebSocket-Bridge für den **Anycubic Kobra X** 3D-Drucker. Sie ermöglicht die Steuerung des Druckers über OrcaSlicer und andere Moonraker-kompatible Software, ohne dass Klipper oder ein Raspberry Pi benötigt wird.

---

## Was wird unterstützt?

- Druckerstatus (Temperatur, Fortschritt, Zustand)
- Dateiübertragung und Druckstart
- Drucksteuerung: Pause, Fortsetzen, Abbrechen
- Temperaturregelung während des laufenden Drucks
- Druckgeschwindigkeit (Leise / Normal / Sport)
- AMS-Farbwechsel (Einziehen / Ausziehen)
- Licht- und Lüftersteuerung
- Web-UI mit Dashboard, Temperaturkarten, Achsensteuerung und Kameraansicht
- Einstellungen und Self-Update direkt im Browser (⚙-Menü)
- OrcaSlicer-Verbindung (Moonraker-Protokoll)

---

## Voraussetzungen

- Anycubic Kobra X im lokalen Netzwerk (LAN, nicht WLAN-Isolation)
- MQTT-Credentials des Druckers (→ siehe [Credentials extrahieren](#credentials-extrahieren))
- Docker **oder** Python 3.9+ **oder** direkt die Linux-Binary

---

## Schnellstart – Docker (empfohlen)

```bash
# 1. .env anlegen
cp .env.example .env
# .env mit deinen Druckerdaten befüllen (→ extract_credentials)

# 2. Bridge starten
docker compose up -d

# 3. In OrcaSlicer: Drucker → "Moonraker" → http://BRIDGE-IP:7125
```

Logs prüfen:
```bash
docker compose logs -f
```

Update:
```bash
docker compose pull && docker compose up -d
```

---

## Schnellstart – Binary (Linux)

```bash
chmod +x kx-bridge
./kx-bridge --printer-ip 192.168.x.x --username userXXXX --password XXXXX \
            --device-id XXXXX --mode-id 20030
```

Oder mit `.env`-Datei im gleichen Verzeichnis – die Bridge liest sie automatisch.

---

## Schnellstart – Python direkt

```bash
pip install aiohttp
python kobrax_moonraker_bridge.py --printer-ip 192.168.x.x ...
# Oder .env befüllen, dann ohne Argumente starten
python kobrax_moonraker_bridge.py
```

---

## Credentials extrahieren

Die MQTT-Zugangsdaten sind druckerspezifisch und werden beim ersten Verbindungsaufbau mit dem AnycubicSlicerNext generiert. Das Tool `extract_credentials` liest sie aus dem RAM des laufenden Slicers aus.

**Voraussetzung:** AnycubicSlicerNext muss gestartet und mit dem Drucker verbunden sein.

### Windows

```
extract_credentials.exe --write-env
```

Schreibt die gefundenen Credentials direkt in `.env`.

### Linux

```bash
chmod +x extract_credentials
./extract_credentials --write-env
```

### Ausgabe

```
[*] Prozess gefunden: AnycubicSlicerNext.exe (PID 1234)
[*] 1986 Speichersegmente gelesen (738.8 MB)
[*] Analysiere ... 100% (739 MB)

=======================================================
  ERGEBNISSE
=======================================================
  Username      userXXXXXXXXXX  (Treffer: 47)
  Password      ***************  (Treffer: 1046)
  Device-ID     xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx  (Treffer: 3504)
  Drucker-IP    192.168.x.x  (Treffer: 3036)
=======================================================

Hinweis: --write-env übergeben um Credentials in '.env' zu speichern.
```

Alle Credentials werden **ausschließlich lokal verarbeitet** — keine Übertragung an externe Server.

---

## Konfiguration (.env)

```env
PRINTER_IP=192.168.x.x       # IP des Druckers
MQTT_PORT=9883                # Standard, nicht ändern
MQTT_USERNAME=userXXXXXXXX   # Beginnt mit "user"
MQTT_PASSWORD=XXXXXXXXXXXXXX  # ~15 Zeichen, gemischt
DEVICE_ID=xxxxxxxx...         # 32-stelliger Hex-String
MODE_ID=20030                 # Kobra X Standard
```

---

## OrcaSlicer verbinden

1. Drucker hinzufügen → **Anycubic Kobra X** (oder generischer Klipper-Drucker)
2. Verbindungstyp: **Moonraker**
3. IP: `http://BRIDGE-HOST:7125`
4. Verbindung testen → sollte "Online" anzeigen

---

## Web-UI

Die Bridge stellt unter `http://BRIDGE-HOST:7125` eine Web-Oberfläche bereit:

| Bereich | Funktion |
|---------|----------|
| Dashboard | Druckerstatus, Fortschritt, Temperaturübersicht |
| Temperaturen | Nozzle und Bett direkt setzen |
| Achsen | X/Y/Z-Bewegung, Motorfreigabe |
| Druckgeschwindigkeit | Leise / Normal / Sport |
| Lüfter / Licht | Lüfterdrehzahl und Drucklicht |
| AMS | Filament einziehen / ausziehen |
| Kamera | Live-Vorschau (falls Drucker unterstützt) |
| ⚙ Einstellungen | MQTT-Zugangsdaten, Poll-Intervall, Self-Update |

### Self-Update

Über das ⚙-Menü in der Web-UI kann die Bridge auf neue Versionen prüfen und sich selbst aktualisieren — ohne Neuinstallation. Nach dem Download startet die Bridge automatisch mit der neuen Version neu.

---

## bridge.sh (Linux Service-Manager)

```bash
./bridge.sh start    # Bridge im Hintergrund starten
./bridge.sh stop     # Bridge beenden
./bridge.sh restart  # Neustarten
./bridge.sh status   # Status und Port prüfen
./bridge.sh log 50   # Letzte 50 Log-Zeilen
```

---

## Druckerzustände

Die Bridge übersetzt die internen Kobra-Zustände in Moonraker-kompatible Zustände:

| Kobra-Zustand | Bedeutung |
|---------------|-----------|
| free | Bereit |
| printing / busy | Druckt |
| pausing / paused | Pausiert |
| resuming / resumed | Wird fortgesetzt |
| stopping / stoped | Wird gestoppt |
| finished | Abgeschlossen |
| canceled | Abgebrochen |
| failed | Fehler |

---

## Fehlerbehebung

**Port 7125 bereits belegt:**
```bash
./bridge.sh stop    # oder: fuser -k 7125/tcp
./bridge.sh start
```

**Credentials ungültig / Verbindung abgelehnt:**
- AnycubicSlicerNext starten, mit Drucker verbinden, `extract_credentials` erneut ausführen
- Falls das Ergebnis unsicher wirkt: `extract_credentials --verbose` zeigt alle Kandidaten an
- Den richtigen Kandidaten manuell in `.env` eintragen und Bridge neu starten

**Temperaturänderungen werden ignoriert:**
- Während eines laufenden Drucks werden Temperaturänderungen über einen separaten Kanal gesendet — das ist normal und wird von der Bridge automatisch erkannt.

**Docker: Permission denied:**
```bash
sudo usermod -aG docker $USER
# Neu einloggen
```

**Docker: .env nicht gefunden:**
```bash
# .env muss im gleichen Verzeichnis wie docker-compose.yml liegen
cp .env.example .env && nano .env
```

---

## Logs

```bash
# Docker
docker compose logs -f kx-bridge

# Binary / Python
tail -f /tmp/bridge.log   # bei Nutzung von bridge.sh
```

---

## Sicherheitshinweise

- Die Bridge bindet standardmäßig auf `0.0.0.0:7125` — nur im lokalen Netzwerk nutzen
- `.env` enthält Drucker-Credentials — nicht öffentlich teilen
- Die Credentials sind druckerspezifisch und haben keinen Zugang zu Anycubic-Cloud-Diensten

---

## Lizenz & Rechtliches

Dieses Projekt entstand durch Interoperabilitätsforschung gem. §69e UrhG.  
Ausschließlich für private, nicht-kommerzielle Nutzung.
