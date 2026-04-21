# KX-Bridge

Verbindet den Anycubic Kobra X mit OrcaSlicer – ohne Klipper, ohne Raspberry Pi.

KX-Bridge läuft auf deinem PC oder NAS und stellt eine Moonraker-kompatible Schnittstelle bereit, über die OrcaSlicer den Drucker direkt steuern kann: Druckstart, Temperatur, Fortschritt, Pause/Fortsetzen/Abbrechen, AMS-Farbwechsel, Druckgeschwindigkeit und mehr.

**Version:** 0.9.1-beta4

---

## Enthaltene Dateien

| Datei | Beschreibung |
|-------|-------------|
| `kobrax_moonraker_bridge.py` | Bridge-Hauptprogramm |
| `kx-bridge` | Vorkompilierte Linux-Binary |
| `extract_credentials.exe` | Zugangsdaten aus AnycubicSlicerNext auslesen (Windows) |
| `extract_credentials` | Zugangsdaten aus AnycubicSlicerNext auslesen (Linux) |
| `kobra_x_orcaslicer_preset.zip` | OrcaSlicer-Druckerprofil für den Kobra X |
| `bridge.sh` | Service-Manager für Linux |
| `Dockerfile` / `docker-compose.yml` | Docker-Deployment |
| `.env.example` | Konfigurationsvorlage |

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

- Anycubic Kobra X im LAN-Modus (Drucker muss über LAN erreichbar sein, nicht nur über Anycubic-Cloud)
- PC, NAS oder Server im gleichen Netzwerk (Windows oder Linux)
- Docker oder Python 3.9+
- MQTT-Zugangsdaten des Druckers → [Schritt 1](#schritt-1-zugangsdaten-ermitteln)

---

## Schnellstart

### Schritt 1: Zugangsdaten ermitteln

Die Bridge benötigt druckerspezifische MQTT-Zugangsdaten.

> **Wichtig:** Der Drucker muss sich im LAN-Modus befinden. Nur wenn der Drucker direkt über LAN (nicht ausschließlich über die Anycubic-Cloud) erreichbar ist, können die Zugangsdaten ermittelt und die Bridge genutzt werden.

AnycubicSlicerNext starten und mit dem Drucker verbinden (bis der Drucker-Status angezeigt wird), dann:

**Windows:**
```
extract_credentials.exe --write-env
```

**Linux:**
```bash
chmod +x extract_credentials
./extract_credentials --write-env
```

Die Zugangsdaten werden automatisch in `.env` gespeichert.

> Falls das Ergebnis unsicher wirkt: `--verbose` zeigt alle gefundenen Kandidaten. Den richtigen Wert manuell in `.env` eintragen.

---

### Schritt 2: Konfiguration prüfen

```bash
cp .env.example .env
# .env öffnen und Werte kontrollieren
```

---

### Schritt 3: Bridge starten

**Option A – Docker (empfohlen):**
```bash
docker compose up -d
```
Läuft im Hintergrund, startet automatisch nach Systemneustart.

**Option B – Linux Binary:**
```bash
chmod +x kx-bridge
./kx-bridge
# Oder mit Service-Manager:
./bridge.sh start
```

**Option C – Python direkt:**
```bash
pip install aiohttp
python kobrax_moonraker_bridge.py
```

---

### Schritt 4: OrcaSlicer-Profil installieren

1. `kobra_x_orcaslicer_preset.zip` in OrcaSlicer importieren:  
   Datei → Konfigurationen importieren → ZIP auswählen
2. Anycubic Kobra X als Drucker auswählen

---

### Schritt 5: OrcaSlicer verbinden

1. Drucker-Einstellungen öffnen
2. Verbindungstyp: **Moonraker**
3. Adresse: `http://IP-DES-BRIDGE-PC:7125` eintragen
4. Auf „Test" klicken – bei erfolgreicher Verbindung erscheint eine Bestätigungsmeldung

---

## Web-UI

Die Bridge stellt unter `http://BRIDGE-IP:7125` eine Web-Oberfläche bereit:

| Bereich | Funktion |
|---------|----------|
| Dashboard | Druckerstatus, Fortschritt, Temperaturübersicht |
| Temperaturen | Nozzle und Bett direkt setzen |
| Achsen | X/Y/Z-Bewegung, Motorfreigabe |
| Druckgeschwindigkeit | Leise / Normal / Sport |
| Lüfter / Licht | Lüfterdrehzahl und Drucklicht |
| AMS | Filament einziehen / ausziehen |
| Kamera | Live-Vorschau (falls vom Drucker unterstützt) |
| ⚙ Einstellungen | MQTT-Zugangsdaten, Poll-Intervall, Self-Update |

### Self-Update

Über das ⚙-Menü in der Web-UI kann die Bridge auf neue Versionen prüfen und sich selbst aktualisieren — ohne Neuinstallation. Nach dem Download startet sie automatisch neu.

---

## bridge.sh – Service-Manager (Linux)

```bash
./bridge.sh start     # Im Hintergrund starten
./bridge.sh stop      # Beenden
./bridge.sh restart   # Neustarten
./bridge.sh status    # Status anzeigen
./bridge.sh log 50    # Letzte 50 Log-Zeilen
```

---

## Docker – Nützliche Befehle

```bash
docker compose up -d                         # Starten
docker compose down                          # Stoppen
docker compose logs -f                       # Logs verfolgen
docker compose pull && docker compose up -d  # Update
```

---

## Fehlerbehebung

**Port 7125 belegt:**
```bash
./bridge.sh stop
./bridge.sh start
```

**Verbindungstest in OrcaSlicer schlägt fehl:**
- Firewall prüfen: Port 7125 muss erreichbar sein
- Bridge-Log prüfen: `./bridge.sh log` oder `docker compose logs`
- Drucker-IP in `.env` korrekt?

**Zugangsdaten werden abgelehnt:**
- AnycubicSlicerNext starten, mit Drucker verbinden
- `extract_credentials --verbose` ausführen und alle Kandidaten prüfen
- Richtigen Wert manuell in `.env` eintragen, Bridge neu starten

**Temperaturänderungen werden ignoriert:**
- Während eines laufenden Drucks werden Temperaturänderungen über einen separaten Kanal gesendet — das ist normal und wird von der Bridge automatisch erkannt.

**Docker: Permission denied:**
```bash
sudo usermod -aG docker $USER
# Neu einloggen, dann erneut versuchen
```

---

## Konfigurationsreferenz (.env)

| Parameter | Beschreibung | Beispiel |
|-----------|-------------|---------|
| `PRINTER_IP` | IP-Adresse des Druckers | `192.168.1.100` |
| `MQTT_PORT` | MQTT-Port (nicht ändern) | `9883` |
| `MQTT_USERNAME` | Benutzername (beginnt mit „user") | `userXXXXXXXXXX` |
| `MQTT_PASSWORD` | Passwort (~15 Zeichen) | `***` |
| `DEVICE_ID` | Geräte-ID (32 Hex-Zeichen) | `xxxxxxxx...` |
| `MODE_ID` | Modell-ID (Kobra X Standard) | `20030` |

---

## Sicherheitshinweise

- Die Bridge bindet standardmäßig auf `0.0.0.0:7125` — nur im lokalen Netzwerk nutzen
- `.env` enthält Drucker-Credentials — nicht öffentlich teilen
- Alle Zugangsdaten werden ausschließlich lokal verarbeitet — keine Übertragung an externe Server

---

## Hinweis zur Nutzung

Dieses Projekt dient der privaten Nutzung und der Herstellung von Interoperabilität zwischen dem Anycubic Kobra X und freier Software (OrcaSlicer).

`extract_credentials` liest ausschließlich den Arbeitsspeicher des auf deinem eigenen PC laufenden AnycubicSlicerNext-Prozesses. Es werden keine Daten übertragen oder gespeichert, außer in die lokale `.env`-Datei.

Das Projekt steht in keiner Verbindung zu Anycubic und wird nicht kommerziell betrieben.
