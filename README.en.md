# KX-Bridge

Connects the Anycubic Kobra X to OrcaSlicer – no Klipper, no Raspberry Pi required.

KX-Bridge runs on your PC or NAS and provides a Moonraker-compatible interface so OrcaSlicer can control the printer directly: print start, temperatures, progress, pause/resume/cancel, AMS filament changes, print speed, and more.

**Version:** 0.9.1-beta4

---

## Included files

| File | Description |
|------|-------------|
| `kobrax_moonraker_bridge.py` | Bridge main program |
| `kx-bridge` | Pre-built Linux binary |
| `extract_credentials.exe` | Read credentials from AnycubicSlicerNext (Windows) |
| `extract_credentials` | Read credentials from AnycubicSlicerNext (Linux) |
| `kobra_x_orcaslicer_preset.zip` | OrcaSlicer printer profile for the Kobra X |
| `bridge.sh` | Linux service manager |
| `Dockerfile` / `docker-compose.yml` | Docker deployment |
| `.env.example` | Configuration template |

---

## What's supported?

- Printer status (temperature, progress, state)
- File transfer and print start
- Print control: pause, resume, cancel
- Temperature control during an active print
- Print speed (Silent / Normal / Sport)
- AMS filament change (load / unload)
- Light and fan control
- Web UI with dashboard, temperature cards, axis control, and camera view
- Settings and self-update directly in the browser (⚙ menu)
- OrcaSlicer connection (Moonraker protocol)

---

## Requirements

- Anycubic Kobra X in LAN mode (printer must be reachable via LAN, not only through Anycubic Cloud)
- PC, NAS, or server on the same network (Windows or Linux)
- Docker or Python 3.9+
- Printer MQTT credentials → [Step 1](#step-1-extract-credentials)

---

## Quick start

### Step 1: Extract credentials

The bridge needs printer-specific MQTT credentials.

> **Important:** The printer must be in LAN mode. The credentials can only be extracted when the printer is reachable directly over LAN (not solely through Anycubic Cloud).

Start AnycubicSlicerNext and connect to the printer (wait until the printer status is shown), then:

**Windows:**
```
extract_credentials.exe --write-env
```

**Linux:**
```bash
chmod +x extract_credentials
./extract_credentials --write-env
```

The credentials are saved automatically to `.env`.

> If the result looks uncertain: `--verbose` shows all found candidates. Enter the correct value manually in `.env`.

---

### Step 2: Check configuration

```bash
cp .env.example .env
# Open .env and verify the values
```

---

### Step 3: Start the bridge

**Option A – Docker (recommended):**
```bash
docker compose up -d
```
Runs in the background and restarts automatically after a system reboot.

**Option B – Linux binary:**
```bash
chmod +x kx-bridge
./kx-bridge
# Or with the service manager:
./bridge.sh start
```

**Option C – Python directly:**
```bash
pip install aiohttp
python kobrax_moonraker_bridge.py
```

---

### Step 4: Install OrcaSlicer profile

1. Import `kobra_x_orcaslicer_preset.zip` into OrcaSlicer:  
   File → Import → Import Configs → select ZIP
2. Select Anycubic Kobra X as your printer

---

### Step 5: Connect OrcaSlicer

1. Open printer settings
2. Connection type: **Moonraker**
3. Host: `http://BRIDGE-PC-IP:7125`
4. Click "Test" — a success message confirms the connection

---

## Web UI

The bridge serves a web interface at `http://BRIDGE-IP:7125`:

| Section | Function |
|---------|----------|
| Dashboard | Printer status, progress, temperature overview |
| Temperatures | Set nozzle and bed temperature directly |
| Motion | X/Y/Z movement, motor release |
| Print Speed | Silent / Normal / Sport |
| Fan / Light | Fan speed and work light |
| AMS | Load / unload filament |
| Camera | Live preview (if supported by the printer) |
| ⚙ Settings | MQTT credentials, poll interval, self-update |

### Self-update

The ⚙ menu in the web UI lets you check for new versions and update the bridge in place — no reinstallation needed. After the download the bridge restarts automatically with the new version.

---

## bridge.sh – Service manager (Linux)

```bash
./bridge.sh start     # Start in background
./bridge.sh stop      # Stop
./bridge.sh restart   # Restart
./bridge.sh status    # Show status
./bridge.sh log 50    # Show last 50 log lines
```

---

## Docker – Useful commands

```bash
docker compose up -d                         # Start
docker compose down                          # Stop
docker compose logs -f                       # Follow logs
docker compose pull && docker compose up -d  # Update
```

---

## Troubleshooting

**Port 7125 already in use:**
```bash
./bridge.sh stop
./bridge.sh start
```

**OrcaSlicer connection test fails:**
- Check firewall: port 7125 must be reachable
- Check bridge log: `./bridge.sh log` or `docker compose logs`
- Is the printer IP in `.env` correct?

**Credentials rejected:**
- Start AnycubicSlicerNext and connect to the printer
- Run `extract_credentials --verbose` and check all candidates
- Enter the correct value manually in `.env`, then restart the bridge

**Temperature changes are ignored:**
- During an active print, temperature changes are sent via a separate channel — this is normal and the bridge handles it automatically.

**Docker: Permission denied:**
```bash
sudo usermod -aG docker $USER
# Log out and back in, then try again
```

---

## Configuration reference (.env)

| Parameter | Description | Example |
|-----------|-------------|---------|
| `PRINTER_IP` | Printer IP address | `192.168.1.100` |
| `MQTT_PORT` | MQTT port (do not change) | `9883` |
| `MQTT_USERNAME` | Username (starts with "user") | `userXXXXXXXXXX` |
| `MQTT_PASSWORD` | Password (~15 characters) | `***` |
| `DEVICE_ID` | Device ID (32 hex characters) | `xxxxxxxx...` |
| `MODE_ID` | Model ID (Kobra X default) | `20030` |

---

## Security notes

- The bridge binds to `0.0.0.0:7125` by default — use on your local network only
- `.env` contains printer credentials — do not share publicly
- All credentials are processed locally only — nothing is sent to external servers

---

## Disclaimer

This project is intended for private use and to enable interoperability between the Anycubic Kobra X and free software (OrcaSlicer).

`extract_credentials` only reads the memory of the AnycubicSlicerNext process running on your own PC. No data is transmitted or stored anywhere other than the local `.env` file.

This project is not affiliated with Anycubic and is not operated commercially.
