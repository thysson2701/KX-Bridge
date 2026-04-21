# KX-Bridge – Anycubic Kobra X Moonraker Bridge

**Version:** 0.9.1-beta5  
**Status:** Public Beta – suitable for home users, feedback welcome

KX-Bridge is a Moonraker-compatible HTTP/WebSocket bridge for the **Anycubic Kobra X** 3D printer. It allows you to control the printer through OrcaSlicer and other Moonraker-compatible software — no Klipper, no Raspberry Pi required.

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

- Anycubic Kobra X on your local network (LAN, no Wi-Fi client isolation)
- Printer MQTT credentials (→ see [Extracting credentials](#extracting-credentials))
- Docker **or** Python 3.9+ **or** the pre-built Linux binary

---

## Quick start – Docker (recommended)

```bash
# 1. Create .env
cp .env.example .env
# Fill in your printer data (→ extract_credentials)

# 2. Start the bridge
docker compose up -d

# 3. In OrcaSlicer: add printer → "Moonraker" → http://BRIDGE-IP:7125
```

Check logs:
```bash
docker compose logs -f
```

Update:
```bash
docker compose pull && docker compose up -d
```

---

## Quick start – Binary (Linux)

```bash
chmod +x kx-bridge
./kx-bridge --printer-ip 192.168.x.x --username userXXXX --password XXXXX \
            --device-id XXXXX --mode-id 20030
```

Or place a `.env` file in the same directory — the bridge reads it automatically.

---

## Quick start – Python directly

```bash
pip install aiohttp
python kobrax_moonraker_bridge.py --printer-ip 192.168.x.x ...
# Or fill in .env and start without arguments
python kobrax_moonraker_bridge.py
```

---

## Extracting credentials

The MQTT credentials are printer-specific and are generated on first connection with AnycubicSlicerNext. The `extract_credentials` tool reads them from the memory of the running slicer.

**Requirement:** AnycubicSlicerNext must be running and connected to the printer.

### Windows

```
extract_credentials.exe --write-env
```

Writes the found credentials directly to `.env`.

### Linux

```bash
chmod +x extract_credentials
./extract_credentials --write-env
```

### Output

```
[*] Process found: AnycubicSlicerNext.exe (PID 1234)
[*] 1986 memory segments read (738.8 MB)
[*] Analyzing ... 100% (739 MB)

=======================================================
  RESULTS
=======================================================
  Username      userXXXXXXXXXX  (hits: 47)
  Password      ***************  (hits: 1046)
  Device-ID     xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx  (hits: 3504)
  Printer IP    192.168.x.x  (hits: 3036)
=======================================================

Hint: pass --write-env to save credentials to '.env'.
```

All credentials are **processed locally only** — nothing is sent to external servers.

---

## Configuration (.env)

```env
PRINTER_IP=192.168.x.x       # Printer IP address
MQTT_PORT=9883                # Default, do not change
MQTT_USERNAME=userXXXXXXXX   # Starts with "user"
MQTT_PASSWORD=XXXXXXXXXXXXXX  # ~15 characters, mixed case
DEVICE_ID=xxxxxxxx...         # 32-character hex string
MODE_ID=20030                 # Kobra X default
```

---

## OrcaSlicer setup

1. Add printer → **Anycubic Kobra X** (or generic Klipper printer)
2. Connection type: **Moonraker**
3. Host: `http://BRIDGE-HOST:7125`
4. Test connection → should show "Online"

---

## Web UI

The bridge serves a web interface at `http://BRIDGE-HOST:7125`:

| Section | Function |
|---------|----------|
| Dashboard | Printer status, progress, temperature overview |
| Temperatures | Set nozzle and bed temperature directly |
| Motion | X/Y/Z movement, motor release |
| Print Speed | Silent / Normal / Sport |
| Fan / Light | Fan speed and work light |
| AMS | Load / unload filament |
| Camera | Live preview (if supported by printer) |
| ⚙ Settings | MQTT credentials, poll interval, self-update |

### Self-update

The ⚙ menu in the web UI lets you check for new versions and update the bridge in place — no reinstallation needed. After the download the bridge restarts automatically with the new version.

---

## bridge.sh (Linux service manager)

```bash
./bridge.sh start    # Start bridge in background
./bridge.sh stop     # Stop bridge
./bridge.sh restart  # Restart
./bridge.sh status   # Check status and port
./bridge.sh log 50   # Show last 50 log lines
```

---

## Printer states

The bridge translates internal Kobra states into Moonraker-compatible states:

| Kobra state | Meaning |
|-------------|---------|
| free | Ready |
| printing / busy | Printing |
| pausing / paused | Paused |
| resuming / resumed | Resuming |
| stopping / stoped | Stopping |
| finished | Complete |
| canceled | Cancelled |
| failed | Error |

---

## Troubleshooting

**Port 7125 already in use:**
```bash
./bridge.sh stop    # or: fuser -k 7125/tcp
./bridge.sh start
```

**Invalid credentials / connection refused:**
- Start AnycubicSlicerNext, connect to the printer, then run `extract_credentials` again
- If the result looks uncertain: `extract_credentials --verbose` shows all candidates
- Manually enter the correct candidate in `.env` and restart the bridge

**Temperature changes are ignored:**
- During an active print, temperature changes are sent via a separate channel — this is normal and the bridge handles it automatically.

**Docker: Permission denied:**
```bash
sudo usermod -aG docker $USER
# Log out and back in
```

**Docker: .env not found:**
```bash
# .env must be in the same directory as docker-compose.yml
cp .env.example .env && nano .env
```

---

## Logs

```bash
# Docker
docker compose logs -f kx-bridge

# Binary / Python
tail -f /tmp/bridge.log   # when using bridge.sh
```

---

## Security notes

- The bridge binds to `0.0.0.0:7125` by default — use on your local network only
- `.env` contains printer credentials — do not share publicly
- The credentials are printer-specific and have no access to Anycubic cloud services

---

## License & legal

This project was created through interoperability research under §69e UrhG (German copyright law).  
For private, non-commercial use only.
