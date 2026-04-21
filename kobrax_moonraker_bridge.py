"""
kobrax_moonraker_bridge.py – Moonraker-kompatibler HTTP/WebSocket-Bridge für Anycubic Kobra X

Emuliert die Moonraker/Klipper-API damit OrcaSlicer den Kobra X direkt ansteuern kann.

Verwendung:
  python kobrax_moonraker_bridge.py --printer-ip 192.168.178.94

OrcaSlicer-Konfiguration:
  Drucker-Typ: Klipper  |  Host: 127.0.0.1  |  Port: 7125
"""

import argparse
import env_loader
import asyncio
import hashlib
import json
import logging
import os
import pathlib
import re
import sys
import tempfile
import time
import threading

# kobrax_client aus dem selben Verzeichnis importieren
sys.path.insert(0, os.path.dirname(__file__))
from kobrax_client import KobraXClient

try:
    from aiohttp import web
    import aiohttp
except ImportError:
    print("Fehler: aiohttp nicht installiert. Bitte: pip install aiohttp")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("bridge")

KOBRA_TO_KLIPPER_STATE = {
    "free":          "standby",
    "busy":          "printing",
    "printing":      "printing",
    "preheating":    "printing",
    "auto_leveling": "printing",
    "checking":      "printing",
    "updated":       "printing",
    "init":          "printing",
    "pausing":       "paused",
    "paused":        "paused",
    "resuming":      "printing",
    "resumed":       "printing",
    "stopping":      "printing",
    "stoped":        "standby",
    "finished":      "complete",
    "failed":        "error",
    "canceled":      "standby",
}

MOONRAKER_VERSION = "v0.9.3-1"
KLIPPER_VERSION   = "v0.12.0-1"


class KobraXBridge:
    def __init__(self, client: KobraXClient, args=None):
        self.client = client
        self._args = args
        self.ws_clients: set[web.WebSocketResponse] = set()
        self._last_state: dict = {}
        self._state = {
            "nozzle_temp":        0.0,
            "nozzle_target":      0.0,
            "bed_temp":           0.0,
            "bed_target":         0.0,
            "print_state":        "standby",
            "kobra_state":        "free",
            "filename":           "",
            "progress":           0.0,
            "print_duration":     0,
            "curr_layer":         0,
            "total_layers":       0,
            "printer_name":       "Anycubic Kobra X",
            "firmware_version":   "unknown",
            "upload_url":         "",
            "camera_url":         "",
            "fan_speed":          0,
            "light_on":           False,
            "light_brightness":   80,
            "taskid":             "-1",
            "print_speed_mode":   2,
        }
        self._ams_slots: list[dict] = []
        self._ams_loaded_slot: int = -1
        self._last_uploaded_file: str = ""
        self._serve_dir = tempfile.TemporaryDirectory(prefix="kobrax_serve_")
        self._serve_dir_path: str = self._serve_dir.name

        self._thumbnail_b64: str = ""  # base64-PNG aus file/report

        # Register MQTT push callbacks
        client.callbacks["tempature/report"]      = self._on_temp
        client.callbacks["print/report"]          = self._on_print
        client.callbacks["info/report"]           = self._on_info
        client.callbacks["file/report"]           = self._on_file
        client.callbacks["multiColorBox/report"]  = self._on_multicolor_box

    # -------------------------------------------------------------------------
    # MQTT callbacks (called from reader thread)
    # -------------------------------------------------------------------------

    def _on_temp(self, payload: dict):
        d = payload.get("data") or {}
        self._state["nozzle_temp"]   = float(d.get("curr_nozzle_temp", 0))
        self._state["nozzle_target"] = float(d.get("target_nozzle_temp", 0))
        self._state["bed_temp"]      = float(d.get("curr_hotbed_temp", 0))
        self._state["bed_target"]    = float(d.get("target_hotbed_temp", 0))
        self._push_status_update()

    def _on_print(self, payload: dict):
        d = payload.get("data") or {}
        kobra_state = payload.get("state", "")
        self._state["print_state"]    = KOBRA_TO_KLIPPER_STATE.get(kobra_state, "printing")
        if kobra_state:
            self._state["kobra_state"] = kobra_state
        if kobra_state in ("stoped", "canceled"):
            self._state["progress"] = 0.0
            self._state["filename"] = ""
        self._state["filename"]       = d.get("filename", self._state["filename"])
        if "progress" in d:
            self._state["progress"]   = float(d["progress"]) / 100.0
        if "print_time" in d:
            self._state["print_duration"] = int(d["print_time"]) * 60
        if "curr_layer" in d:
            self._state["curr_layer"] = d["curr_layer"]
        if "total_layers" in d:
            self._state["total_layers"] = d["total_layers"]
        if "taskid" in d:
            self._state["taskid"] = str(d["taskid"])
        settings = d.get("settings") or {}
        if "print_speed_mode" in settings:
            self._state["print_speed_mode"] = int(settings["print_speed_mode"])
        self._push_status_update()

    def _on_info(self, payload: dict):
        d = payload.get("data") or {}
        self._state["printer_name"]     = d.get("printerName", self._state["printer_name"])
        self._state["firmware_version"] = d.get("version", self._state["firmware_version"])
        kobra_state = d.get("state", "")
        if kobra_state:
            self._state["print_state"] = KOBRA_TO_KLIPPER_STATE.get(kobra_state, "standby")
            self._state["kobra_state"] = kobra_state
        t = d.get("temp") or {}
        if t:
            self._state["nozzle_temp"]   = float(t.get("curr_nozzle_temp", 0))
            self._state["nozzle_target"] = float(t.get("target_nozzle_temp", 0))
            self._state["bed_temp"]      = float(t.get("curr_hotbed_temp", 0))
            self._state["bed_target"]    = float(t.get("target_hotbed_temp", 0))
        urls = d.get("urls") or {}
        if urls.get("fileUploadurl"):
            self._state["upload_url"] = urls["fileUploadurl"]
        if urls.get("rtspUrl"):
            self._state["camera_url"] = urls["rtspUrl"]
        fan = d.get("fan_speed_pct")
        if fan is not None:
            self._state["fan_speed"] = int(fan)
        speed_mode = d.get("print_speed_mode")
        if speed_mode is not None:
            self._state["print_speed_mode"] = int(speed_mode)
        self._push_status_update()

    def _on_file(self, payload: dict):
        d = payload.get("data") or {}
        details = d.get("file_details") or {}
        thumb = details.get("thumbnail") or details.get("png_image") or ""
        if thumb:
            self._thumbnail_b64 = thumb
            log.info(f"Vorschaubild empfangen: {len(thumb)} Zeichen base64")
        self._push_status_update()

    def _on_multicolor_box(self, payload: dict):
        boxes = (payload.get("data") or {}).get("multi_color_box") or []
        if not boxes:
            return
        box = boxes[0]
        slots = box.get("slots") or []
        loaded = box.get("loaded_slot", -1)
        if loaded != -1:
            self._ams_loaded_slot = loaded
        # Tip-Forming: nach Einziehen (status=10) oder Ausziehen (status=11)
        # schickt der originale Slicer automatisch type=3 (Extruder-Rückzug)
        fs = box.get("feed_status") or {}
        current_status = fs.get("current_status")
        slot_index = fs.get("slot_index", 0)
        if current_status in (10, 11):
            import threading
            def _tip_form():
                import time; time.sleep(2)
                self.client.publish(
                    "multiColorBox", "feedFilament",
                    {"multi_color_box": [{"id": -1, "feed_status": {"slot_index": slot_index, "type": 3}}]},
                    timeout=0
                )
                log.info(f"Tip-Forming (type=3) nach status={current_status} slot={slot_index}")
            threading.Thread(target=_tip_form, daemon=True).start()
        if slots:
            self._ams_slots = slots
            log.info(f"AMS-Slots empfangen: {len(slots)}, loaded_slot={self._ams_loaded_slot}")
            self._push_status_update()

    # -------------------------------------------------------------------------
    # WebSocket push
    # -------------------------------------------------------------------------

    def _push_status_update(self):
        if not self.ws_clients:
            return
        msg = {
            "jsonrpc": "2.0",
            "method":  "notify_status_update",
            "params": [self._build_printer_objects(), time.time()],
        }
        text = json.dumps(msg)
        dead = set()
        for ws in self.ws_clients:
            try:
                asyncio.run_coroutine_threadsafe(ws.send_str(text), ws._loop)
            except Exception:
                dead.add(ws)
        self.ws_clients -= dead

    def _build_printer_objects(self) -> dict:
        s = self._state
        return {
            "extruder": {
                "temperature": s["nozzle_temp"],
                "target":      s["nozzle_target"],
                "power":       0.0,
            },
            "heater_bed": {
                "temperature": s["bed_temp"],
                "target":      s["bed_target"],
                "power":       0.0,
            },
            "print_stats": {
                "state":          s["print_state"],
                "filename":       s["filename"],
                "print_duration": s["print_duration"],
                "total_duration": s["print_duration"],
                "info": {
                    "current_layer": s["curr_layer"],
                    "total_layer":   s["total_layers"],
                },
            },
            "display_status": {
                "progress": s["progress"],
                "message":  "",
            },
            "virtual_sdcard": {
                "progress":  s["progress"],
                "is_active": s["print_state"] == "printing",
                "file_path": s["filename"],
            },
            "toolhead": {
                "position":         [0, 0, 0, 0],
                "homed_axes":       "xyz",
                "print_time":       s["print_duration"],
                "estimated_print_time": s["print_duration"],
            },
        }

    # -------------------------------------------------------------------------
    # HTTP handlers
    # -------------------------------------------------------------------------

    async def handle_server_info(self, request):
        return web.json_response({
            "result": {
                "klippy_connected": True,
                "klippy_state":     "ready",
                "components":       ["file_manager", "job_state", "virtual_sdcard"],
                "failed_components":[],
                "registered_directories": ["gcodes"],
                "warnings":         [],
                "websocket_count":  len(self.ws_clients),
                "moonraker_version": MOONRAKER_VERSION,
                "api_version":      [1, 3, 0],
                "api_version_string": "1.3.0",
            }
        })

    async def handle_printer_info(self, request):
        s = self._state
        return web.json_response({
            "result": {
                "state":           "ready",
                "state_message":   "Printer is ready",
                "hostname":        "kobrax-bridge",
                "klipper_path":    "/home/pi/klipper",
                "python_path":     "/home/pi/klippy-env/bin/python",
                "log_file":        "/tmp/klippy.log",
                "config_file":     "/home/pi/printer.cfg",
                "software_version": KLIPPER_VERSION,
                "cpu_info":        s["printer_name"],
            }
        })

    async def handle_machine_system_info(self, request):
        return web.json_response({
            "result": {
                "system_info": {
                    "cpu_info": {"cpu_count": 4, "bits": "64bit", "processor": "armv7l",
                                 "cpu_desc": "Anycubic Kobra X Bridge", "serial_number": "",
                                 "hardware_desc": "", "model": "Kobra X Bridge",
                                 "total_memory": 524288, "memory_units": "kB"},
                    "sd_info": {},
                    "distribution": {"name": "Linux", "id": "linux", "version": "1.0",
                                     "version_parts": {}, "like": "", "codename": ""},
                    "available_services": [],
                    "service_state": {},
                    "python": {"version": list(sys.version_info[:3]), "version_string": sys.version},
                    "network": {},
                    "canbus": {},
                }
            }
        })

    async def handle_objects_query(self, request):
        objects = self._build_printer_objects()
        # filter by requested objects if specified
        requested = dict(request.rel_url.query)
        if requested:
            filtered = {k: objects[k] for k in requested if k in objects}
        else:
            filtered = objects
        return web.json_response({"result": {"status": filtered, "eventtime": time.time()}})

    async def handle_objects_list(self, request):
        return web.json_response({
            "result": {
                "objects": list(self._build_printer_objects().keys())
            }
        })

    async def handle_objects_subscribe(self, request):
        return web.json_response({
            "result": {
                "status": self._build_printer_objects(),
                "eventtime": time.time(),
            }
        })

    async def handle_files_list(self, request):
        filename = self._state.get("filename", "")
        files = []
        if filename:
            files.append({
                "path":     filename,
                "modified": time.time(),
                "size":     0,
                "permissions": "rw",
            })
        return web.json_response({"result": files})

    async def handle_file_upload(self, request):
        log.info(f"Upload-Request: {request.method} {request.path_qs}  CT={request.headers.get('Content-Type','')[:60]}")
        ct = request.headers.get("Content-Type", "")
        if "multipart" not in ct:
            return web.json_response({"error": "expected multipart"}, status=400)
        reader = await request.multipart()
        file_data = None
        remote_filename = self._last_uploaded_file or "upload.gcode"

        async for part in reader:
            if part.name in ("file", "gcode", "upload_file"):
                remote_filename = part.filename or remote_filename
                file_data = await part.read()
                log.info(f"Multipart-Feld '{part.name}': {remote_filename} ({len(file_data)} bytes)")
            elif part.name == "path":
                val = (await part.read()).decode("utf-8", errors="replace").strip()
                if val:
                    remote_filename = val
            else:
                log.debug(f"Unbekanntes Multipart-Feld: {part.name}")

        if not file_data:
            return web.json_response({"error": "no file received"}, status=400)

        file_md5  = hashlib.md5(file_data).hexdigest()
        file_size = len(file_data)

        # Datei auf Disk ablegen (temp-Verzeichnis) damit Drucker sie per HTTP abrufen kann
        safe_name = os.path.basename(remote_filename)  # keine Pfad-Traversal
        serve_path = os.path.join(self._serve_dir_path, safe_name)
        with open(serve_path, "wb") as f:
            f.write(file_data)
        del file_data  # RAM freigeben

        self._last_uploaded_file = remote_filename
        log.info(f"Upload: {remote_filename} ({file_size} bytes) md5={file_md5} → Drucker")

        # Datei per HTTP auf den Drucker hochladen (serve_path liegt bereits auf Disk)
        upload_url = self._state.get("upload_url") or None
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None, self.client.upload_gcode, serve_path, remote_filename, upload_url
            )
        except Exception as e:
            log.error(f"Upload fehlgeschlagen: {e}")
            return web.json_response({"error": str(e)}, status=500)

        log.info(f"Upload erfolgreich: {result}")

        # Druck starten mit vollständigem Payload (inkl. serve-URL + md5 + size)
        serve_url = f"http://{request.host}/serve/{remote_filename}"
        log.info(f"Starte Druck automatisch: {remote_filename}")
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, lambda: self._start_print(remote_filename, serve_url, file_md5, file_size))

        # OctoPrint-kompatibler Response (OrcaSlicer wertet refs aus)
        return web.json_response({
            "done": True,
            "files": {
                "local": {
                    "name": remote_filename,
                    "origin": "local",
                    "path": remote_filename,
                    "refs": {
                        "download": f"http://{request.host}/api/files/local/{remote_filename}",
                        "resource":  f"http://{request.host}/api/files/local/{remote_filename}",
                    }
                }
            },
            "result": {
                "item": {"path": remote_filename, "root": "gcodes"},
                "action": "create_file",
            }
        }, status=201)

    def _start_print(self, filename: str, url: str = "", md5: str = "", filesize: int = 0):
        use_ams = len(self._ams_slots) > 0
        ams_box_mapping = [
            {
                "paint_index":  i,
                "ams_index":    i,
                "paint_color":  [255, 255, 255, 255],
                "ams_color":    [255, 255, 255, 255],
                "material_type": s.get("material_type", "PLA"),
            }
            for i, s in enumerate(self._ams_slots)
        ]
        payload = {
            "taskid":       "-1",
            "url":          url,
            "filename":     filename,
            "md5":          md5,
            "filepath":     None,
            "filetype":     1,
            "project_type": 1,
            "filesize":     filesize,
            "ams_settings": {
                "use_ams":        use_ams,
                "ams_box_mapping": ams_box_mapping,
            },
            "task_settings": {
                "auto_leveling":            1,
                "vibration_compensation":   0,
                "flow_calibration":         0,
                "dry_mode":                 0,
                "ai_settings":   {"status": 0, "count": 0, "type": 1},
                "timelapse":     {"status": 0, "count": 0, "type": 64},
                "drying_settings": {"status": 0, "target_temp": 0, "duration": 0, "remain_time": 0},
                "model_objects_skip_parts": [],
            },
        }
        # Thumbnail vorab anfordern (Drucker antwortet async auf file/report)
        self._thumbnail_b64 = ""
        self.client.publish("file", "fileDetails",
                            {"root": "local", "filename": filename}, timeout=0)

        log.info(f"print/start → {filename}  url={url}  ams={len(self._ams_slots)} slots")
        result = self.client.publish("print", "start", payload, timeout=15.0)
        if result:
            log.info(f"Druckstart bestätigt: state={result.get('state')}")
        else:
            log.warning("Druckstart: keine Antwort vom Drucker")

    async def handle_print_start(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        filename = body.get("filename") or self._last_uploaded_file
        if not filename:
            return web.json_response({"error": "no filename"}, status=400)

        log.info(f"Druck starten: {filename}")

        # AMS-Mapping aus gecachtem State
        ams_box_mapping = []
        for i, slot in enumerate(self._ams_slots):
            ams_box_mapping.append({
                "slot_index": i,
                "material_type": slot.get("material_type", "PLA"),
                "color": slot.get("color", "FFFFFF"),
            })

        use_ams = len(self._ams_slots) > 0

        payload = {
            "filename": filename,
            "taskid":   str(int(time.time())),
            "use_ams":  use_ams,
        }
        if ams_box_mapping:
            payload["ams_box_mapping"] = ams_box_mapping

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: self.client.publish("print", "start", payload, timeout=15.0)
        )
        if result is None:
            return web.json_response({"error": "Keine Antwort vom Drucker"}, status=504)

        return web.json_response({"result": "ok"})

    async def handle_print_pause(self, request):
        loop = asyncio.get_event_loop()
        taskid = self._state.get("taskid", "-1")
        await loop.run_in_executor(None, lambda: self.client.pause_print(taskid))
        return web.json_response({"result": "ok"})

    async def handle_print_resume(self, request):
        loop = asyncio.get_event_loop()
        taskid = self._state.get("taskid", "-1")
        await loop.run_in_executor(None, lambda: self.client.resume_print(taskid))
        return web.json_response({"result": "ok"})

    async def handle_print_cancel(self, request):
        loop = asyncio.get_event_loop()
        taskid = self._state.get("taskid", "-1")
        await loop.run_in_executor(None, lambda: self.client.stop_print(taskid))
        return web.json_response({"result": "ok"})

    async def handle_octoprint_version(self, request):
        return web.json_response({
            "api":     "0.1",
            "server":  "1.9.0",
            "text":    "OctoPrint (Kobra X Bridge)",
        })

    async def handle_index(self, request):
        html = r"""<!DOCTYPE html>
<html lang="de" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>KX-Bridge</title>
<style>
:root{
  --bg:#1a1a1f;--card:#24242c;--raised:#2e2e3a;--border:#3a3a4a;
  --txt:#f0f0f5;--txt2:#8888aa;--accent:#00c8ff;--accent2:#ff6b35;
  --ok:#4cde80;--err:#ff4d6d;--warn:#ffb020;
  --font:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  --mono:"JetBrains Mono","Fira Code",monospace;
}
[data-theme=light]{
  --bg:#f0f0f5;--card:#fff;--raised:#e8e8f0;--border:#d0d0e0;
  --txt:#1a1a2e;--txt2:#666680;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:var(--font);font-size:14px;min-height:100vh;display:flex;flex-direction:column}
a{color:var(--accent);text-decoration:none}

/* ── HEADER ── */
header{background:var(--card);border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:12px;padding:0 20px;height:52px;
  position:sticky;top:0;z-index:100}
.logo{font-size:18px;font-weight:700;color:var(--accent);letter-spacing:-.02em}
.hname{font-size:13px;color:var(--txt2);flex:1}
.hbadge{display:flex;align-items:center;gap:6px;font-size:12px;font-weight:600;
  padding:4px 10px;border-radius:20px;background:var(--raised);color:var(--txt2);
  text-transform:uppercase;letter-spacing:.04em}
.hbadge.printing{background:#0d2d1a;color:var(--ok)}
.hbadge.complete{background:#0d1f38;color:#60b0ff}
.hbadge.error{background:#2d0d0d;color:var(--err)}
.hbadge .dot{width:7px;height:7px;border-radius:50%;background:currentColor}
.hbadge.printing .dot{animation:pulse 1.4s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.theme-btn{background:none;border:1px solid var(--border);color:var(--txt2);
  border-radius:8px;padding:6px 10px;cursor:pointer;font-size:13px;transition:.15s}
.theme-btn:hover{border-color:var(--accent);color:var(--accent)}

/* ── LAYOUT ── */
.layout{display:flex;flex:1;min-height:0}
nav.sidebar{width:200px;background:var(--card);border-right:1px solid var(--border);
  display:flex;flex-direction:column;padding:12px 8px;gap:2px;flex-shrink:0}
.nav-btn{background:none;border:none;color:var(--txt2);text-align:left;
  padding:9px 12px;border-radius:8px;cursor:pointer;font-size:13px;
  display:flex;align-items:center;gap:10px;transition:.12s;width:100%}
.nav-btn:hover{background:var(--raised);color:var(--txt)}
.nav-btn.active{background:var(--raised);color:var(--accent)}
.nav-icon{font-size:16px;width:20px;text-align:center}
main{flex:1;overflow-y:auto;padding:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px}

/* ── CARD ── */
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;
  padding:18px;transition:box-shadow .15s,transform .15s}
.card:hover{box-shadow:0 4px 20px rgba(0,0,0,.3);transform:translateY(-1px)}
.card-title{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--txt2);
  margin-bottom:14px;display:flex;align-items:center;gap:8px}
.card-title span{font-size:14px}

/* ── HERO ── */
.hero{grid-column:1/-1;display:grid;grid-template-columns:1fr 320px;gap:16px}
@media(max-width:900px){.hero{grid-template-columns:1fr}}
.cam-wrap{background:#0a0a0e;border-radius:10px;overflow:hidden;
  min-height:180px;max-height:320px;display:flex;align-items:center;justify-content:center;position:relative}
.cam-wrap img,.cam-wrap video{width:100%;max-height:320px;height:auto;display:block;object-fit:contain}
.cam-placeholder{color:var(--txt2);font-size:13px;text-align:center;padding:20px}
@keyframes spin{to{transform:rotate(360deg)}}
.cam-spinner{width:40px;height:40px;border:3px solid rgba(255,255,255,.15);
  border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite;display:none}
.cam-overlay{position:absolute;bottom:0;left:0;right:0;
  background:linear-gradient(transparent,rgba(0,0,0,.75));padding:14px}
.cam-toggle{position:absolute;top:10px;right:10px;background:rgba(0,0,0,.5);
  border:1px solid rgba(255,255,255,.2);color:#fff;border-radius:8px;
  padding:6px 10px;cursor:pointer;font-size:12px;backdrop-filter:blur(4px)}
.cam-toggle:hover{background:rgba(0,0,0,.7)}

/* ── PROGRESS ── */
.hero-info{display:flex;flex-direction:column;gap:12px}
.pct-big{font-size:52px;font-weight:700;line-height:1;color:var(--txt)}
.pct-big small{font-size:20px;font-weight:400;color:var(--txt2)}
.progress-bar{height:8px;background:var(--raised);border-radius:4px;overflow:hidden;margin:4px 0}
.progress-fill{height:100%;background:linear-gradient(90deg,var(--accent),#0080cc);
  border-radius:4px;transition:width .6s ease}
.meta-row{display:flex;justify-content:space-between;font-size:12px;color:var(--txt2)}
.layer-badge{background:var(--raised);border-radius:6px;padding:4px 8px;
  font-family:var(--mono);font-size:12px;color:var(--txt)}
.fname{font-size:12px;color:var(--txt2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  background:var(--raised);border-radius:6px;padding:6px 8px}

/* ── PRINT CONTROLS ── */
.ctrl-btns{display:flex;gap:8px;flex-wrap:wrap}
.btn{border:none;border-radius:8px;padding:10px 16px;font-size:13px;font-weight:600;
  cursor:pointer;transition:opacity .15s,transform .1s;white-space:nowrap}
.btn:hover{opacity:.85;transform:translateY(-1px)}
.btn:active{transform:translateY(0)}
.btn-start{background:var(--ok);color:#0d2010}
.btn-pause{background:var(--raised);color:var(--txt);border:1px solid var(--border)}
.btn-resume{background:#0d2d1a;color:var(--ok);border:1px solid var(--ok)}
.btn-cancel{background:#2d0d0d;color:var(--err);border:1px solid var(--err)}
.btn-accent{background:var(--accent);color:#001a24}
.btn-sm{padding:7px 12px;font-size:12px}
.spd-btn{flex:1;border:1.5px solid var(--border);background:var(--raised);color:var(--txt);
  border-radius:10px;padding:14px 8px;font-size:13px;font-weight:600;cursor:pointer;
  transition:all .15s;display:flex;flex-direction:column;align-items:center;gap:4px}
.spd-btn:hover{border-color:var(--accent);color:var(--accent)}
.spd-btn.spd-active{border-color:var(--accent);background:rgba(0,200,255,.12);color:var(--accent)}
.spd-btn .spd-icon{font-size:22px}
.spd-bar{height:4px;border-radius:2px;background:var(--border);margin-top:10px;overflow:hidden}
.spd-bar-fill{height:100%;border-radius:2px;background:linear-gradient(90deg,var(--accent2),var(--accent));transition:width .3s}

/* ── TEMPS ── */
.temp-pair{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.temp-card-inner{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.temp-block{background:var(--raised);border-radius:10px;padding:14px;position:relative}
.temp-label{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--txt2);margin-bottom:6px}
.temp-row{display:flex;align-items:baseline;gap:6px}
.temp-val{font-size:30px;font-weight:700;font-family:var(--mono)}
.temp-unit{font-size:14px;color:var(--txt2)}
.temp-target{font-size:11px;color:var(--txt2);margin-top:2px}
.temp-arc{position:absolute;top:12px;right:12px}
.temp-edit{display:flex;gap:6px;margin-top:10px}
.temp-input{background:var(--bg);border:1px solid var(--border);color:var(--txt);
  border-radius:6px;padding:5px 8px;font-size:13px;font-family:var(--mono);width:70px}
.temp-input:focus{outline:none;border-color:var(--accent)}
.chart-wrap{margin-top:12px}
canvas.tchart{width:100%;height:60px;display:block;border-radius:6px;background:var(--raised)}

/* ── MOTION ── */
.joypad{display:grid;grid-template-columns:repeat(3,52px);
  grid-template-rows:repeat(3,52px);gap:6px;justify-content:center;margin:8px auto}
.joy{background:var(--raised);border:1px solid var(--border);color:var(--txt);
  border-radius:10px;font-size:18px;cursor:pointer;transition:.12s;
  display:flex;align-items:center;justify-content:center}
.joy:hover{background:var(--accent);color:#001a24;border-color:var(--accent)}
.joy:active{transform:scale(.93)}
.joy.home{font-size:14px;background:var(--bg)}
.step-btns{display:flex;gap:6px;justify-content:center;flex-wrap:wrap;margin-top:10px}
.step-btn{background:var(--raised);border:1px solid var(--border);color:var(--txt2);
  border-radius:6px;padding:5px 10px;font-size:12px;cursor:pointer;transition:.12s}
.step-btn.active,.step-btn:hover{background:var(--accent);color:#001a24;border-color:var(--accent)}
.home-btns{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px;justify-content:center}

/* ── AMS ── */
.ams-slots{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.ams-slot{background:var(--raised);border-radius:10px;padding:10px;text-align:center;
  border:2px solid transparent;transition:.2s;position:relative}
.ams-slot.active{border-color:var(--slot-color,var(--accent));
  box-shadow:0 0 12px rgba(var(--slot-rgb,0,200,255),.3)}
.slot-circle{width:36px;height:36px;border-radius:50%;margin:0 auto 6px;border:2px solid rgba(255,255,255,.15)}
.slot-label{font-size:11px;color:var(--txt2);font-family:var(--mono)}
.slot-material{font-size:12px;font-weight:600;margin-bottom:2px}

/* ── LIGHT + FAN ── */
.toggle-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.toggle-label{font-size:13px;font-weight:600}
.toggle{position:relative;width:44px;height:24px;cursor:pointer}
.toggle input{opacity:0;width:0;height:0;position:absolute}
.toggle-track{width:44px;height:24px;background:var(--raised);border-radius:12px;
  border:1px solid var(--border);transition:.25s;display:block}
.toggle input:checked+.toggle-track{background:var(--accent)}
.toggle-thumb{position:absolute;top:3px;left:3px;width:18px;height:18px;
  background:#fff;border-radius:50%;transition:.25s;pointer-events:none}
.toggle input:checked~.toggle-thumb{transform:translateX(20px)}
.slider-row{display:flex;align-items:center;gap:10px;margin-top:8px}
.slider-label{font-size:12px;color:var(--txt2);width:80px}
.slider{flex:1;-webkit-appearance:none;height:4px;border-radius:2px;
  background:var(--raised);outline:none;cursor:pointer}
.slider::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;
  border-radius:50%;background:var(--accent);cursor:pointer;transition:.1s}
.slider::-webkit-slider-thumb:hover{transform:scale(1.2)}
.slider-val{font-family:var(--mono);font-size:12px;color:var(--txt);width:30px;text-align:right}

/* ── CONSOLE ── */
.console{background:#0a0a0e;border-radius:8px;padding:10px;font-family:var(--mono);
  font-size:11px;color:#8888aa;height:160px;overflow-y:auto;line-height:1.6}
.console .ts{color:#444;margin-right:6px}
.console .msg-info{color:#8888aa}
.console .msg-ok{color:var(--ok)}
.console .msg-err{color:var(--err)}
.console .msg-warn{color:var(--warn)}

/* ── PANELS ── */
.panel{display:none}
.panel.active{display:block}

/* ── MODAL ── */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);
  z-index:200;align-items:center;justify-content:center;padding:16px}
.modal-overlay.open{display:flex}
.modal-box{background:var(--card);border:1px solid var(--border);border-radius:14px;
  width:100%;max-width:480px;max-height:90vh;overflow-y:auto;padding:24px;
  display:flex;flex-direction:column;gap:18px}
.modal-header{display:flex;align-items:center;justify-content:space-between}
.modal-title{font-size:15px;font-weight:700;color:var(--txt)}
.modal-close{background:none;border:none;color:var(--txt2);font-size:20px;
  cursor:pointer;padding:4px 8px;border-radius:6px}
.modal-close:hover{background:var(--raised);color:var(--txt)}
.modal-section{font-size:10px;text-transform:uppercase;letter-spacing:.1em;
  color:var(--txt2);margin-bottom:6px;margin-top:4px}
.modal-field{display:flex;flex-direction:column;gap:4px;margin-bottom:10px}
.modal-field label{font-size:12px;color:var(--txt2)}
.modal-field input{background:var(--raised);border:1px solid var(--border);
  border-radius:7px;color:var(--txt);padding:7px 10px;font-size:13px;width:100%}
.modal-field input:focus{outline:none;border-color:var(--accent)}
.poll-btns{display:flex;gap:8px}
.poll-btn{flex:1;padding:7px;background:var(--raised);border:1px solid var(--border);
  border-radius:7px;color:var(--txt2);cursor:pointer;font-size:13px;transition:all .15s}
.poll-btn.active{background:var(--accent);border-color:var(--accent);color:#000;font-weight:600}
.update-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.update-status{font-size:12px;color:var(--txt2);flex:1;min-width:0}
.modal-save{width:100%;padding:10px;background:var(--accent);border:none;
  border-radius:8px;color:#000;font-weight:700;font-size:14px;cursor:pointer;margin-top:4px}
.modal-save:hover{opacity:.88}

/* ── BOTTOM NAV (mobile) ── */
nav.bottom-nav{display:none;position:fixed;bottom:0;left:0;right:0;
  background:var(--card);border-top:1px solid var(--border);
  justify-content:space-around;padding:8px 0 max(8px,env(safe-area-inset-bottom))}
.bnav-btn{background:none;border:none;color:var(--txt2);display:flex;
  flex-direction:column;align-items:center;gap:3px;cursor:pointer;font-size:10px;padding:4px 8px}
.bnav-btn.active{color:var(--accent)}
.bnav-icon{font-size:20px}

/* ── Tablet (769–1100px): schmale Sidebar ── */
@media(min-width:769px) and (max-width:1100px){
  nav.sidebar{width:52px;padding:12px 4px}
  .nav-btn .nav-text{display:none}
  .nav-btn{justify-content:center;padding:10px}
  .nav-icon{width:auto}
  .grid{grid-template-columns:repeat(2,1fr)}
  .hero{grid-template-columns:1fr}
}

/* ── Mobile (≤768px): Bottom-Nav, 1-Spalte ── */
@media(max-width:768px){
  nav.sidebar{display:none}
  nav.bottom-nav{display:flex}
  main{padding:10px;padding-bottom:72px}

  /* Header kompakt */
  header{padding:0 12px;gap:8px}
  .hname{display:none}

  /* 1-Spalten-Grid, full-width spans funktionieren weiterhin */
  .grid{grid-template-columns:1fr;gap:12px}

  /* Hero: Kamera über Info */
  .hero{grid-template-columns:1fr}
  .cam-wrap{max-height:220px}

  /* Temp-Pair und Temp-Card übereinander */
  .temp-pair{grid-template-columns:1fr}
  .temp-card-inner{grid-template-columns:1fr}

  /* AMS: 2 Spalten */
  .ams-slots{grid-template-columns:repeat(2,1fr)}

  /* Joypad etwas kleiner */
  .joypad{grid-template-columns:repeat(3,44px);grid-template-rows:repeat(3,44px);gap:5px}
  .joy{font-size:16px}

  /* Buttons größere Touch-Targets */
  .btn{padding:10px 14px;font-size:13px}
  .btn-sm{padding:8px 12px}
  .step-btn{padding:8px 12px;font-size:13px}

  /* Modal vollbreite auf kleinen Screens */
  .modal-box{padding:16px;border-radius:10px}
  .poll-btns{gap:6px}
}
</style>
</head>
<body>

<header>
  <div class="logo">⬡ KX-Bridge</div>
  <div class="hname" id="h-pname">Anycubic Kobra X</div>
  <div class="hbadge" id="h-badge"><span class="dot"></span><span id="h-state">Standby</span></div>
  <button class="theme-btn" onclick="toggleTheme()">☀ / ☾</button>
  <button class="theme-btn" onclick="toggleLang()" id="lang-btn">EN</button>
  <button class="theme-btn" onclick="openSettings()" id="settings-btn" title="Einstellungen">⚙</button>
</header>

<!-- ═══ SETTINGS MODAL ═══ -->
<div class="modal-overlay" id="settings-modal" onclick="if(event.target===this)closeSettings()">
  <div class="modal-box">
    <div class="modal-header">
      <span class="modal-title" id="modal-title-settings">Einstellungen</span>
      <button class="modal-close" onclick="closeSettings()">✕</button>
    </div>

    <div>
      <div class="modal-section" id="modal-sec-connection">Verbindung</div>
      <div class="modal-field">
        <label id="lbl-printer-ip">Drucker-IP</label>
        <input type="text" id="s-printer-ip" placeholder="192.168.x.x">
      </div>
      <div class="modal-field">
        <label id="lbl-mqtt-port">MQTT-Port</label>
        <input type="number" id="s-mqtt-port" placeholder="9883">
      </div>
      <div class="modal-field">
        <label id="lbl-username">MQTT-Benutzername</label>
        <input type="text" id="s-username" placeholder="userXXXXXXXX" autocomplete="off">
      </div>
      <div class="modal-field">
        <label id="lbl-password">MQTT-Passwort</label>
        <input type="password" id="s-password" autocomplete="off">
      </div>
      <div class="modal-field">
        <label id="lbl-device-id">Device-ID</label>
        <input type="text" id="s-device-id" placeholder="32 Hex-Zeichen">
      </div>
      <div class="modal-field">
        <label id="lbl-mode-id">Mode-ID</label>
        <input type="text" id="s-mode-id" placeholder="20030">
      </div>
    </div>

    <div>
      <div class="modal-section" id="modal-sec-poll">Poll-Intervall</div>
      <div class="poll-btns">
        <button class="poll-btn" onclick="setPoll(1000)" id="poll-1">1s</button>
        <button class="poll-btn active" onclick="setPoll(2000)" id="poll-2">2s</button>
        <button class="poll-btn" onclick="setPoll(5000)" id="poll-5">5s</button>
      </div>
    </div>

    <div>
      <div class="modal-section" id="modal-sec-version">Version</div>
      <div class="update-row">
        <span id="s-version-label" style="font-size:13px;color:var(--txt)">–</span>
        <button class="btn btn-sm" style="background:var(--raised);color:var(--txt)" onclick="checkUpdate()" id="btn-update-check">🔄 <span id="lbl-update-check">Auf Updates prüfen</span></button>
      </div>
      <div class="update-status" id="update-status" style="margin-top:6px"></div>
      <button class="btn btn-sm btn-accent" id="btn-update-apply" style="display:none;margin-top:8px" onclick="applyUpdate()">
        <span id="lbl-update-apply">Jetzt installieren</span>
      </button>
    </div>

    <button class="modal-save" onclick="saveSettings()" id="btn-save-settings">Speichern &amp; Neustart</button>
  </div>
</div>

<div class="layout">
  <nav class="sidebar">
    <button class="nav-btn active" onclick="showPanel('dashboard')" id="nb-dashboard">
      <span class="nav-icon">⊞</span><span class="nav-text">Dashboard</span></button>
    <button class="nav-btn" onclick="showPanel('console')" id="nb-console">
      <span class="nav-icon">≡</span><span class="nav-text">Konsole</span></button>
  </nav>

  <main>
    <!-- ═══ DASHBOARD ═══ -->
    <div class="panel active" id="panel-dashboard">
      <div class="grid">
        <!-- Kamera -->
        <div class="card" style="grid-column:1/-1">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
            <div class="card-title" style="margin-bottom:0"><span>📷</span> <span id="d-card-cam">Kamera</span></div>
            <div style="display:flex;align-items:center;gap:10px">
              <span id="d-lbl-light" style="font-size:12px;color:var(--txt2)">💡 Licht</span>
              <label class="toggle">
                <input type="checkbox" id="d-light-toggle" onchange="setLight()">
                <span class="toggle-track"></span>
                <span class="toggle-thumb"></span>
              </label>
            </div>
          </div>
          <div class="cam-wrap" id="cam-wrap">
            <div class="cam-placeholder" id="cam-placeholder">📷 Kamera nicht gestartet</div>
            <div class="cam-spinner" id="cam-spinner"></div>
            <img id="cam-img" style="display:none;width:100%;height:auto" alt="Kamera">
            <div class="cam-overlay" id="cam-overlay" style="display:none">
              <div style="font-size:12px;color:#fff" id="cam-fname"></div>
            </div>
            <button class="cam-toggle" onclick="toggleCam()" id="cam-toggle-btn">▶ Kamera</button>
          </div>
        </div>

        <!-- Fortschritt -->
        <div class="card" style="grid-column:1/-1">
          <div class="card-title"><span>◉</span> <span id="d-card-progress">Fortschritt</span></div>
          <img id="d-thumbnail" src="" alt="" style="display:none;width:100%;max-height:160px;object-fit:contain;border-radius:8px;background:#111;margin-bottom:10px">
          <div class="pct-big"><span id="d-pct">0</span><small>%</small></div>
          <div class="progress-bar" style="margin:8px 0"><div class="progress-fill" id="d-pbar" style="width:0%"></div></div>
          <div class="meta-row" style="margin-top:6px">
            <span id="d-elapsed">–</span>
            <span id="d-layers" class="layer-badge">–</span>
          </div>
          <div class="fname" id="d-fname" title="" style="margin-top:6px">–</div>
          <div class="ctrl-btns" id="d-ctrl-btns" style="margin-top:12px">
            <button class="btn btn-pause btn-sm" id="d-btn-pause" onclick="printAction('pause')">⏸ Pause</button>
            <button class="btn btn-resume btn-sm" id="d-btn-resume" onclick="printAction('resume')">▶ Weiter</button>
            <button class="btn btn-cancel btn-sm" id="d-btn-cancel" onclick="confirmCancel()">✕ Stopp</button>
          </div>
        </div>

        <!-- Temperatursteuerung + Verlauf -->
        <div class="card" style="grid-column:1/-1">
          <div class="card-title"><span>⊙</span> <span id="d-card-temps">Temperaturen</span></div>
          <div class="temp-card-inner">
            <div class="temp-block">
              <div class="temp-label">Nozzle</div>
              <div class="temp-row">
                <div class="temp-val" id="d-nt">–</div>
                <div class="temp-unit">°C</div>
              </div>
              <div class="temp-target">→ <span id="d-nt-t">0</span>°C</div>
              <div class="progress-bar" style="margin:8px 0 0">
                <div class="progress-fill" id="d-ntbar" style="width:0%;background:linear-gradient(90deg,var(--accent2),#ffb020)"></div>
              </div>
              <div class="temp-edit" style="margin-top:10px">
                <input type="number" class="temp-input" id="p-nozzle-inp" placeholder="Ziel" min="0" max="300" style="flex:1">
                <button class="btn btn-sm btn-accent" onclick="setNozzle()"><span class="lbl-set">Set</span></button>
                <button class="btn btn-sm" style="background:var(--raised);color:var(--txt)" onclick="document.getElementById('p-nozzle-inp').value=0;setNozzle()"><span class="lbl-off">Aus</span></button>
              </div>
            </div>
            <div class="temp-block">
              <div class="temp-label" id="d-lbl-bed">Bett</div>
              <div class="temp-row">
                <div class="temp-val" id="d-bt">–</div>
                <div class="temp-unit">°C</div>
              </div>
              <div class="temp-target">→ <span id="d-bt-t">0</span>°C</div>
              <div class="progress-bar" style="margin:8px 0 0">
                <div class="progress-fill" id="d-btbar" style="width:0%;background:linear-gradient(90deg,#ff6b35,var(--warn))"></div>
              </div>
              <div class="temp-edit" style="margin-top:10px">
                <input type="number" class="temp-input" id="p-bed-inp" placeholder="Ziel" min="0" max="120" style="flex:1">
                <button class="btn btn-sm btn-accent" onclick="setBed()"><span class="lbl-set">Set</span></button>
                <button class="btn btn-sm" style="background:var(--raised);color:var(--txt)" onclick="document.getElementById('p-bed-inp').value=0;setBed()"><span class="lbl-off">Aus</span></button>
              </div>
            </div>
          </div>
          <div style="margin-top:14px">
            <div style="font-size:10px;color:var(--txt2);margin-bottom:4px" id="d-chart-label">Verlauf (letzte 60 Messungen)</div>
            <canvas id="d-chart" width="800" height="120" style="width:100%;height:120px;background:var(--raised);border-radius:8px"></canvas>
          </div>
        </div>

        <!-- Achsensteuerung -->
        <div class="card">
          <div class="card-title"><span>✛</span> <span id="ptitle-motion-xy">XY-Achsen</span></div>
          <div class="joypad">
            <div></div>
            <button class="joy" onclick="move(1,1,getStep())" title="Y+">▲</button>
            <div></div>
            <button class="joy" onclick="move(0,-1,getStep())" title="X−">◀</button>
            <button class="joy home" onclick="homeAll()" title="Home All">⌂</button>
            <button class="joy" onclick="move(0,1,getStep())" title="X+">▶</button>
            <div></div>
            <button class="joy" onclick="move(1,-1,getStep())" title="Y−">▼</button>
            <div></div>
          </div>
          <div class="step-btns">
            <button class="step-btn" onclick="setStep(this,0.1)">0.1</button>
            <button class="step-btn active" onclick="setStep(this,1)">1</button>
            <button class="step-btn" onclick="setStep(this,5)">5</button>
            <button class="step-btn" onclick="setStep(this,10)">10 mm</button>
          </div>
          <div class="home-btns">
            <button class="btn btn-sm" style="background:var(--raised);color:var(--txt)" onclick="homeAxis('X')"><span class="lbl-home-x">Home X</span></button>
            <button class="btn btn-sm" style="background:var(--raised);color:var(--txt)" onclick="homeAxis('Y')"><span class="lbl-home-y">Home Y</span></button>
            <button class="btn btn-sm" style="background:var(--raised);color:var(--txt)" onclick="homeAxis('Z')"><span class="lbl-home-z">Home Z</span></button>
            <button class="btn btn-sm btn-accent" onclick="homeAll()"><span class="lbl-home-all">Home All</span></button>
          </div>
        </div>
        <div class="card">
          <div class="card-title"><span>↕</span> <span id="ptitle-motion-z">Z-Achse</span></div>
          <div class="joypad" style="grid-template-columns:52px;grid-template-rows:repeat(2,52px)">
            <button class="joy" onclick="move(2,-1,getStep())" title="Z−">▲</button>
            <button class="joy" onclick="move(2,1,getStep())" title="Z+">▼</button>
          </div>
          <div style="text-align:center;margin-top:8px;font-size:12px;color:var(--txt2)">Schrittweite: <span id="step-display">1</span> mm</div>
        </div>

        <!-- Print Speed -->
        <div class="card">
          <div class="card-title"><span>🏎</span> <span id="d-card-speed">Druckgeschwindigkeit</span></div>
          <div style="display:flex;gap:8px;margin-top:4px">
            <button class="spd-btn" id="d-spd-1" onclick="setSpeed(1)">
              <span class="spd-icon">🐢</span>
              <span id="d-spd-lbl-1">Leise</span>
            </button>
            <button class="spd-btn spd-active" id="d-spd-2" onclick="setSpeed(2)">
              <span class="spd-icon">⚡</span>
              <span id="d-spd-lbl-2">Normal</span>
            </button>
            <button class="spd-btn" id="d-spd-3" onclick="setSpeed(3)">
              <span class="spd-icon">🚀</span>
              <span id="d-spd-lbl-3">Sport</span>
            </button>
          </div>
          <div class="spd-bar" style="margin-top:12px">
            <div class="spd-bar-fill" id="d-spd-bar" style="width:50%"></div>
          </div>
        </div>

        <!-- Lüfter -->
        <div class="card">
          <div class="card-title"><span>🌀</span> <span id="d-card-lightfan">Lüfter</span></div>
          <div class="slider-row">
            <input type="range" class="slider" min="0" max="100" value="0" id="d-fan" oninput="document.getElementById('d-fan-val').textContent=this.value" onchange="setFan()">
            <span class="slider-val" id="d-fan-val">0</span>
          </div>
          <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
            <button class="btn btn-sm" style="background:var(--raised);color:var(--txt)" onclick="quickFan(0)">Aus</button>
            <button class="btn btn-sm" style="background:var(--raised);color:var(--txt)" onclick="quickFan(25)">25%</button>
            <button class="btn btn-sm" style="background:var(--raised);color:var(--txt)" onclick="quickFan(50)">50%</button>
            <button class="btn btn-sm" style="background:var(--raised);color:var(--txt)" onclick="quickFan(75)">75%</button>
            <button class="btn btn-sm btn-accent" onclick="quickFan(100)">100%</button>
          </div>
        </div>

        <!-- AMS -->
        <div class="card" style="grid-column:1/-1" id="d-ams-card">
          <div class="card-title"><span>◫</span> <span id="d-card-ams">AMS / Filamentbox</span></div>
          <div class="ams-slots" id="ams-slots">
            <div style="grid-column:1/-1;text-align:center;color:var(--txt2);padding:20px" id="ams-no-data">Keine AMS-Daten empfangen</div>
          </div>
          <div id="ams-controls" style="margin-top:16px;display:flex;flex-direction:column;gap:10px">
            <div style="font-size:12px;color:var(--txt2);margin-bottom:2px" id="ams-slot-lbl">Slot auswählen</div>
            <div style="display:flex;align-items:center;gap:10px">
              <input type="range" id="ams-slot-sel" min="0" max="3" step="1" value="0"
                class="slider" style="flex:1"
                oninput="document.getElementById('ams-slot-label').textContent='Slot '+(parseInt(this.value)+1)">
              <span id="ams-slot-label" style="min-width:48px;font-size:13px;font-weight:600">Slot 1</span>
            </div>
            <div style="display:flex;gap:10px">
              <button class="btn" style="flex:1" onclick="amsFeed(1)">⬇ <span class="lbl-feed">Einziehen</span></button>
              <button id="btn-unload" class="btn" style="flex:1" onclick="amsFeed(2)">⬆ <span class="lbl-unload">Ausziehen</span></button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ═══ CONSOLE ═══ -->
    <div class="panel" id="panel-console">
      <div class="card">
        <div class="card-title"><span>≡</span> <span id="ptitle-console">Ereignis-Log</span></div>
        <div class="console" id="console-log"></div>
      </div>
    </div>
  </main>
</div>

<nav class="bottom-nav">
  <button class="bnav-btn active" onclick="showPanel('dashboard')" id="bnb-dashboard"><span class="bnav-icon">⊞</span>Dashboard</button>
  <button class="bnav-btn" onclick="showPanel('console')" id="bnb-console"><span class="bnav-icon">≡</span>Log</button>
</nav>

<script>
// ── State ──
var S={nozzle_temp:0,nozzle_target:0,bed_temp:0,bed_target:0,
  print_state:'standby',filename:'',progress:0,print_duration:0,
  curr_layer:0,total_layers:0,printer_name:'Kobra X',firmware_version:'–',
  camera_url:'',fan_speed:0,print_speed_mode:2,light_on:false,light_brightness:80,ams_slots:[]};
var tempHistory={n:[],b:[]};
var camOn=false;
var currentStep=1;
var currentPanel='dashboard';

// ── Theme ──
function toggleTheme(){
  var h=document.documentElement;
  h.setAttribute('data-theme',h.getAttribute('data-theme')==='dark'?'light':'dark');
  localStorage.setItem('theme',h.getAttribute('data-theme'));
}
(function(){var t=localStorage.getItem('theme');if(t)document.documentElement.setAttribute('data-theme',t)})();

// ── i18n ──
var LANG_DE={
  header_status_standby:'Bereit',header_status_printing:'Druckt',header_status_complete:'Fertig',header_status_error:'Fehler',
  kobra_free:'Bereit',kobra_busy:'Beschäftigt',kobra_printing:'Druckt',kobra_preheating:'Aufheizen',kobra_auto_leveling:'Nivellierung',kobra_checking:'Prüfung',kobra_updated:'Aktualisierung',kobra_init:'Initialisierung',kobra_pausing:'Pausiert...',kobra_paused:'Pausiert',kobra_resuming:'Fortsetzen...',kobra_resumed:'Fortgesetzt',kobra_stopping:'Stoppt...',kobra_stoped:'Gestoppt',kobra_finished:'Abgeschlossen',kobra_failed:'Fehler',kobra_canceled:'Abgebrochen',kobra_offline:'Offline',
  nav_dashboard:'Dashboard',nav_print:'Druck',nav_temps:'Temperaturen',nav_motion:'Achsen',nav_ams:'AMS',nav_extras:'Licht / Lüfter',nav_console:'Konsole',
  card_progress:'Fortschritt',card_temps:'Temperaturen',card_light_fan:'Lüfter',card_speed:'Druckgeschwindigkeit',card_cam:'Kamera',
  speed_silent:'🐢 Leise',speed_normal:'⚡ Normal',speed_sport:'🚀 Sport',
  lbl_light:'💡 Licht',lbl_feed:'Einziehen',lbl_unload:'Ausziehen',
  cam_placeholder:'📷 Kamera nicht gestartet',btn_cam_start:'▶ Kamera',btn_cam_stop:'◼ Kamera',
  btn_pause:'⏸ Pause',btn_resume:'▶ Weiter',btn_cancel:'✕ Stopp',
  label_nozzle:'Nozzle',label_bed:'Bett',label_fan:'🌀 Lüfter',label_light:'💡 Licht',label_on_off:'Ein / Aus',label_speed:'Geschwindigkeit',
  panel_print_title:'Drucksteuerung',panel_print_btn_pause:'⏸ Pause',panel_print_btn_resume:'▶ Fortsetzen',panel_print_btn_cancel:'✕ Abbrechen',panel_print_temps_live:'Temperaturen (Live)',
  label_set:'Setzen',label_off:'Aus',
  panel_temps_nozzle:'Nozzle',panel_temps_bed:'Heizbett',panel_temps_chart:'Verlauf (letzte 60 Messungen)',label_target_c:'Ziel:',
  panel_motion_xy:'XY-Achsen',panel_motion_z:'Z-Achse',label_step:'Schrittweite:',btn_home_x:'Home X',btn_home_y:'Home Y',btn_home_z:'Home Z',btn_home_all:'Home All',
  panel_ams_title:'AMS / Filamentbox',ams_no_data:'Keine AMS-Daten empfangen',label_slot:'Slot',
  panel_extras_light:'Licht',panel_extras_fan:'Lüfter',panel_extras_camera:'Kamera',btn_cam_start2:'▶ Start',btn_cam_stop2:'◼ Stop',
  panel_console_title:'Ereignis-Log',
  log_light_on:'Licht an',log_light_off:'Licht aus',log_fan:'Lüfter →',log_nozzle:'Nozzle →',log_bed:'Bett →',log_axis:'Achse',log_home:'Home',log_home_all:'Home All',log_cam_start:'Kamera gestartet:',log_cam_stop:'Kamera gestoppt',log_poll_error:'Poll-Fehler:',log_error:'Fehler:',
  confirm_cancel:'Druck wirklich abbrechen?',
  settings_title:'Einstellungen',settings_connection:'Verbindung',settings_poll:'Poll-Intervall',settings_version:'Version',
  settings_save:'Speichern & Neustart',settings_printer_ip:'Drucker-IP',settings_mqtt_port:'MQTT-Port',
  settings_username:'MQTT-Benutzername',settings_password:'MQTT-Passwort',settings_device_id:'Device-ID',settings_mode_id:'Mode-ID',
  update_check:'Auf Updates prüfen',update_checking:'Prüfe...',update_available:'verfügbar',update_none:'Bereits aktuell',
  update_apply:'Jetzt installieren',update_applying:'Lade herunter...',update_restarting:'Starte neu...',update_error:'Fehler'
};
var LANG_EN={
  header_status_standby:'Ready',header_status_printing:'Printing',header_status_complete:'Complete',header_status_error:'Error',
  kobra_free:'Ready',kobra_busy:'Busy',kobra_printing:'Printing',kobra_preheating:'Preheating',kobra_auto_leveling:'Auto Leveling',kobra_checking:'Checking',kobra_updated:'Updating',kobra_init:'Initializing',kobra_pausing:'Pausing...',kobra_paused:'Paused',kobra_resuming:'Resuming...',kobra_resumed:'Resumed',kobra_stopping:'Stopping...',kobra_stoped:'Stopped',kobra_finished:'Finished',kobra_failed:'Error',kobra_canceled:'Cancelled',kobra_offline:'Offline',
  nav_dashboard:'Dashboard',nav_print:'Print',nav_temps:'Temperatures',nav_motion:'Motion',nav_ams:'AMS',nav_extras:'Light / Fan',nav_console:'Console',
  card_progress:'Progress',card_temps:'Temperatures',card_light_fan:'Fan',card_speed:'Print Speed',card_cam:'Camera',
  speed_silent:'🐢 Silent',speed_normal:'⚡ Normal',speed_sport:'🚀 Sport',
  lbl_light:'💡 Light',lbl_feed:'Load',lbl_unload:'Unload',
  cam_placeholder:'📷 Camera not started',btn_cam_start:'▶ Camera',btn_cam_stop:'◼ Camera',
  btn_pause:'⏸ Pause',btn_resume:'▶ Resume',btn_cancel:'✕ Stop',
  label_nozzle:'Nozzle',label_bed:'Bed',label_fan:'🌀 Fan',label_light:'💡 Light',label_on_off:'On / Off',label_speed:'Speed',
  panel_print_title:'Print Control',panel_print_btn_pause:'⏸ Pause',panel_print_btn_resume:'▶ Resume',panel_print_btn_cancel:'✕ Cancel',panel_print_temps_live:'Temperatures (Live)',
  label_set:'Set',label_off:'Off',
  panel_temps_nozzle:'Nozzle',panel_temps_bed:'Heated Bed',panel_temps_chart:'History (last 60 readings)',label_target_c:'Target:',
  panel_motion_xy:'XY Axes',panel_motion_z:'Z Axis',label_step:'Step size:',btn_home_x:'Home X',btn_home_y:'Home Y',btn_home_z:'Home Z',btn_home_all:'Home All',
  panel_ams_title:'AMS / Filament Box',ams_no_data:'No AMS data received',label_slot:'Slot',
  panel_extras_light:'Light',panel_extras_fan:'Fan',panel_extras_camera:'Camera',btn_cam_start2:'▶ Start',btn_cam_stop2:'◼ Stop',
  panel_console_title:'Event Log',
  log_light_on:'Light on',log_light_off:'Light off',log_fan:'Fan →',log_nozzle:'Nozzle →',log_bed:'Bed →',log_axis:'Axis',log_home:'Home',log_home_all:'Home All',log_cam_start:'Camera started:',log_cam_stop:'Camera stopped',log_poll_error:'Poll error:',log_error:'Error:',
  confirm_cancel:'Really cancel the print?',
  settings_title:'Settings',settings_connection:'Connection',settings_poll:'Poll Interval',settings_version:'Version',
  settings_save:'Save & Restart',settings_printer_ip:'Printer IP',settings_mqtt_port:'MQTT Port',
  settings_username:'MQTT Username',settings_password:'MQTT Password',settings_device_id:'Device ID',settings_mode_id:'Mode ID',
  update_check:'Check for Updates',update_checking:'Checking...',update_available:'available',update_none:'Already up to date',
  update_apply:'Install Now',update_applying:'Downloading...',update_restarting:'Restarting...',update_error:'Error'
};
var currentLang='de';
var T=LANG_DE;
function toggleLang(){
  currentLang=currentLang==='de'?'en':'de';
  T=currentLang==='de'?LANG_DE:LANG_EN;
  localStorage.setItem('lang',currentLang);
  document.getElementById('lang-btn').textContent=currentLang==='de'?'EN':'DE';
  document.documentElement.setAttribute('lang',currentLang);
  applyLang();
}
function applyLang(){
  // Nav
  var nb=document.getElementById('nb-dashboard');if(nb)nb.querySelector('.nav-text').textContent=T.nav_dashboard;
  nb=document.getElementById('nb-console');if(nb)nb.querySelector('.nav-text').textContent=T.nav_console;
  // Bottom nav
  var bnb=document.getElementById('bnb-dashboard');if(bnb)bnb.lastChild.textContent=T.nav_dashboard;
  bnb=document.getElementById('bnb-console');if(bnb)bnb.lastChild.textContent=T.nav_console;
  // Dashboard card titles
  setText('d-card-progress',T.card_progress);
  setText('d-card-temps',T.card_temps);
  setText('d-card-lightfan',T.card_light_fan);
  setText('d-card-speed',T.card_speed);
  setText('d-card-cam',T.card_cam);
  setText('d-card-ams',T.panel_ams_title);
  setText('d-lbl-light',T.lbl_light);
  setText('d-lbl-bed',T.label_bed);
  // Dashboard buttons
  setText('d-btn-pause',T.btn_pause);
  setText('d-btn-resume',T.btn_resume);
  setText('d-btn-cancel',T.btn_cancel);
  setText('cam-toggle-btn',camOn?T.btn_cam_stop:T.btn_cam_start);
  setText('cam-placeholder-txt',T.cam_placeholder);
  // Temp labels
  document.querySelectorAll('.lbl-set').forEach(e=>e.textContent=T.label_set);
  document.querySelectorAll('.lbl-off').forEach(e=>e.textContent=T.label_off);
  setText('d-chart-label',T.panel_temps_chart);
  // Axis labels
  setText('ptitle-motion-xy',T.panel_motion_xy);
  setText('ptitle-motion-z',T.panel_motion_z);
  document.querySelectorAll('.lbl-home-x').forEach(e=>e.textContent=T.btn_home_x);
  document.querySelectorAll('.lbl-home-y').forEach(e=>e.textContent=T.btn_home_y);
  document.querySelectorAll('.lbl-home-z').forEach(e=>e.textContent=T.btn_home_z);
  document.querySelectorAll('.lbl-home-all').forEach(e=>e.textContent=T.btn_home_all);
  document.querySelectorAll('.lbl-step').forEach(e=>e.textContent=T.label_step);
  // Console
  setText('ptitle-console',T.panel_console_title);
  // Settings modal
  setText('modal-title-settings',T.settings_title);
  setText('modal-sec-connection',T.settings_connection);
  setText('modal-sec-poll',T.settings_poll);
  setText('modal-sec-version',T.settings_version);
  setText('btn-save-settings',T.settings_save);
  setText('lbl-printer-ip',T.settings_printer_ip);
  setText('lbl-mqtt-port',T.settings_mqtt_port);
  setText('lbl-username',T.settings_username);
  setText('lbl-password',T.settings_password);
  setText('lbl-device-id',T.settings_device_id);
  setText('lbl-mode-id',T.settings_mode_id);
  setText('lbl-update-check',T.update_check);
  setText('lbl-update-apply',T.update_apply);
  // Speed buttons
  setText('d-spd-lbl-1',T.speed_silent.replace(/^\S+\s/,''));
  setText('d-spd-lbl-2',T.speed_normal.replace(/^\S+\s/,''));
  setText('d-spd-lbl-3',T.speed_sport.replace(/^\S+\s/,''));
  // AMS feed/unload
  document.querySelectorAll('.lbl-feed').forEach(e=>e.textContent=T.lbl_feed);
  document.querySelectorAll('.lbl-unload').forEach(e=>e.textContent=T.lbl_unload);
}
function setText(id,txt){var el=document.getElementById(id);if(el)el.textContent=txt;}
(function(){
  var l=localStorage.getItem('lang')||'de';
  currentLang=l;T=l==='de'?LANG_DE:LANG_EN;
  document.getElementById('lang-btn').textContent=l==='de'?'EN':'DE';
  document.documentElement.setAttribute('lang',l);
  // defer until DOM ready
  window.addEventListener('DOMContentLoaded',function(){applyLang();});
})();

// ── Panel nav ──
function showPanel(id){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.getElementById('panel-'+id).classList.add('active');
  document.querySelectorAll('.nav-btn,.bnav-btn').forEach(b=>b.classList.remove('active'));
  var nb=document.getElementById('nb-'+id);if(nb)nb.classList.add('active');
  var bnb=document.getElementById('bnb-'+id);if(bnb)bnb.classList.add('active');
  currentPanel=id;
}

// ── Console log ──
var consoleLogs=[];
function clog(msg,cls){
  cls=cls||'msg-info';
  var ts=new Date().toLocaleTimeString('de',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
  consoleLogs.push({ts,msg,cls});
  if(consoleLogs.length>100)consoleLogs.shift();
  var el=document.getElementById('console-log');
  el.innerHTML=consoleLogs.map(l=>`<div><span class="ts">${l.ts}</span><span class="${l.cls}">${l.msg}</span></div>`).join('');
  el.scrollTop=el.scrollHeight;
}

// ── Helpers ──
function fmtTime(s){if(!s||s<0)return'–';var m=Math.floor(s/60),h=Math.floor(m/60);m%=60;return h>0?h+'h '+m+'m':m+'m'}
function post(url,body){return fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})}
function clamp(v,lo,hi){return Math.min(hi,Math.max(lo,v))}

// ── Apply state to DOM ──
function applyState(){
  var s=S;
  // header
  var b=document.getElementById('h-badge');
  b.className='hbadge '+s.print_state;
  document.getElementById('h-state').textContent=T['kobra_'+s.kobra_state]||s.kobra_state||T.header_status_standby;
  document.getElementById('h-pname').textContent=s.printer_name;

  // temps
  var nt=document.getElementById('d-nt');if(nt)nt.textContent=s.nozzle_temp.toFixed(1);
  var ntt=document.getElementById('d-nt-t');if(ntt)ntt.textContent=s.nozzle_target.toFixed(0);
  var bt=document.getElementById('d-bt');if(bt)bt.textContent=s.bed_temp.toFixed(1);
  var btt=document.getElementById('d-bt-t');if(btt)btt.textContent=s.bed_target.toFixed(0);

  // temp bars (dashboard)
  var nb=document.getElementById('d-ntbar');if(nb)nb.style.width=clamp(s.nozzle_temp/300*100,0,100)+'%';
  var bb=document.getElementById('d-btbar');if(bb)bb.style.width=clamp(s.bed_temp/120*100,0,100)+'%';

  // progress
  var pct=Math.round(s.progress*100);
  var dpct=document.getElementById('d-pct');if(dpct)dpct.textContent=pct;
  var dpbar=document.getElementById('d-pbar');if(dpbar)dpbar.style.width=pct+'%';

  var layers=s.curr_layer&&s.total_layers?'L '+s.curr_layer+' / '+s.total_layers:'–';
  var dlayers=document.getElementById('d-layers');if(dlayers)dlayers.textContent=layers;

  var elapsed=fmtTime(s.print_duration);
  var delapsed=document.getElementById('d-elapsed');if(delapsed)delapsed.textContent=elapsed;

  var fn=s.filename||'–';
  var dfname=document.getElementById('d-fname');if(dfname){dfname.textContent=fn;dfname.title=fn};
  var pfname=document.getElementById('p-fname');if(pfname){pfname.textContent=fn;pfname.title=fn};
  var cfo=document.getElementById('cam-fname');if(cfo)cfo.textContent=fn!=='–'?fn:'';

  // thumbnail
  var thumb=document.getElementById('d-thumbnail');
  if(thumb){
    if(s.thumbnail){
      thumb.src='data:image/png;base64,'+s.thumbnail;
      thumb.style.display='block';
    } else {
      thumb.style.display='none';
      thumb.src='';
    }
  }

  // light/fan sync
  document.getElementById('d-light-toggle').checked=s.light_on;
  var dfan=document.getElementById('d-fan');if(dfan)dfan.value=s.fan_speed;
  var dfanval=document.getElementById('d-fan-val');if(dfanval)dfanval.textContent=s.fan_speed;

  // speed mode buttons
  var spdWidths={1:25,2:55,3:90};
  [1,2,3].forEach(function(m){
    var b=document.getElementById('d-spd-'+m);
    if(b) b.classList.toggle('spd-active', s.print_speed_mode===m);
  });
  var spdBar=document.getElementById('d-spd-bar');
  if(spdBar) spdBar.style.width=(spdWidths[s.print_speed_mode]||55)+'%';

  // AMS
  if(s.ams_slots&&s.ams_slots.length){
    var html='';
    s.ams_slots.forEach(function(slot,i){
      var rgb=Array.isArray(slot.color)?slot.color:[128,128,128];
      var col='rgb('+rgb[0]+','+rgb[1]+','+rgb[2]+')';
      var active=slot.status===1||slot.active;
      var pct=slot.consumables_percent!=null?slot.consumables_percent+'%':'–';
      html+='<div class="ams-slot'+(active?' active':'')+ '" style="--slot-color:'+col+'">'
        +'<div class="slot-circle" style="background:'+col+'"></div>'
        +'<div class="slot-material">'+(slot.type||slot.material_type||'–')+'</div>'
        +'<div class="slot-label">Slot '+(slot.index!=null?slot.index+1:i+1)+'</div>'
        +'<div class="slot-label" style="font-size:10px;color:var(--txt2)">'+pct+'</div>'
        +'</div>';
    });
    document.getElementById('ams-slots').innerHTML=html;
  }

  // camera overlay
  var co=document.getElementById('cam-overlay');
  if(co)co.style.display=(s.print_state==='printing'&&camOn)?'block':'none';

  // auto-start camera during print
  if(s.print_state==='printing'&&!camOn&&s.camera_url){
    camStart();
  }
}

// ── Temp history + chart ──
function updateHistory(){
  tempHistory.n.push(S.nozzle_temp);
  tempHistory.b.push(S.bed_temp);
  if(tempHistory.n.length>60)tempHistory.n.shift();
  if(tempHistory.b.length>60)tempHistory.b.shift();
  drawChart('d-chart',tempHistory,[{data:tempHistory.n,color:'#00c8ff',max:300},{data:tempHistory.b,color:'#ff6b35',max:120}]);
}
function drawChart(id,_,series){
  var canvas=document.getElementById(id);if(!canvas)return;
  var ctx=canvas.getContext('2d');
  var W=canvas.offsetWidth*window.devicePixelRatio||canvas.width;
  var H=canvas.offsetHeight*window.devicePixelRatio||canvas.height;
  canvas.width=W;canvas.height=H;
  ctx.clearRect(0,0,W,H);
  series.forEach(function(s){
    var data=s.data;if(!data.length)return;
    var max=s.max;
    ctx.beginPath();ctx.strokeStyle=s.color;ctx.lineWidth=2;ctx.lineJoin='round';
    data.forEach(function(v,i){
      var x=i/(Math.max(data.length-1,1))*(W-4)+2;
      var y=H-4-(v/max)*(H-8);
      if(i===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);
    });
    ctx.stroke();
  });
}

// ── Settings Modal ──
var _updateTag='';
var _updateUrl='';
function openSettings(){
  fetch('/api/settings').then(function(r){return r.json()}).then(function(d){
    document.getElementById('s-printer-ip').value=d.printer_ip||'';
    document.getElementById('s-mqtt-port').value=d.mqtt_port||9883;
    document.getElementById('s-username').value=d.username||'';
    document.getElementById('s-password').value=d.password||'';
    document.getElementById('s-device-id').value=d.device_id||'';
    document.getElementById('s-mode-id').value=d.mode_id||'';
  });
  var v=localStorage.getItem('pollInterval')||'2000';
  document.querySelectorAll('.poll-btn').forEach(function(b){b.classList.remove('active')});
  var pb=document.getElementById('poll-'+Math.round(parseInt(v)/1000));
  if(pb)pb.classList.add('active');
  document.getElementById('s-version-label').textContent='v'+('__VERSION__'||'?');
  document.getElementById('update-status').textContent='';
  document.getElementById('btn-update-apply').style.display='none';
  _updateTag='';_updateUrl='';
  document.getElementById('settings-modal').classList.add('open');
}
function closeSettings(){
  document.getElementById('settings-modal').classList.remove('open');
}
function setPoll(ms){
  document.querySelectorAll('.poll-btn').forEach(function(b){b.classList.remove('active')});
  var id='poll-'+Math.round(ms/1000);
  var pb=document.getElementById(id);if(pb)pb.classList.add('active');
  localStorage.setItem('pollInterval',ms);
  clearInterval(pollTimer);
  pollTimer=setInterval(poll,ms);
}
function saveSettings(){
  var btn=document.getElementById('btn-save-settings');
  btn.disabled=true;btn.textContent='…';
  post('/api/settings',{
    printer_ip: document.getElementById('s-printer-ip').value,
    mqtt_port:  parseInt(document.getElementById('s-mqtt-port').value)||9883,
    username:   document.getElementById('s-username').value,
    password:   document.getElementById('s-password').value,
    device_id:  document.getElementById('s-device-id').value,
    mode_id:    document.getElementById('s-mode-id').value,
  }).then(function(){
    btn.textContent=T.update_restarting;
    setTimeout(function(){
      btn.disabled=false;
      setText('btn-save-settings',T.settings_save);
      closeSettings();
      poll();
    },4000);
  }).catch(function(e){
    btn.disabled=false;setText('btn-save-settings',T.settings_save);
    clog('Settings-Fehler: '+e,'msg-err');
  });
}
function checkUpdate(){
  var sb=document.getElementById('update-status');
  sb.textContent=T.update_checking;
  document.getElementById('btn-update-apply').style.display='none';
  _updateTag='';_updateUrl='';
  fetch('/api/update/check').then(function(r){return r.json()}).then(function(d){
    if(d.error){sb.textContent=T.update_error+': '+d.error;return;}
    if(d.update_available){
      sb.textContent='v'+d.latest+' '+T.update_available;
      sb.style.color='var(--ok)';
      _updateTag=d.tag;_updateUrl=d.download_url;
      document.getElementById('btn-update-apply').style.display='inline-block';
    } else {
      sb.textContent=T.update_none;
      sb.style.color='var(--txt2)';
    }
  }).catch(function(e){sb.textContent=T.update_error+': '+e;});
}
function applyUpdate(){
  if(!_updateUrl)return;
  var sb=document.getElementById('update-status');
  var btn=document.getElementById('btn-update-apply');
  btn.disabled=true;sb.textContent=T.update_applying;
  post('/api/update/apply',{download_url:_updateUrl,tag:_updateTag}).then(function(){
    sb.textContent=T.update_restarting;
    closeSettings();
    setTimeout(function(){poll();},5000);
  }).catch(function(e){
    btn.disabled=false;sb.textContent=T.update_error+': '+e;
  });
}

// ── Poll ──
async function poll(){
  try{
    var r=await fetch('/api/state');
    if(!r.ok)return;
    var d=await r.json();
    Object.assign(S,d);
    applyState();
    updateHistory();
  }catch(e){clog('Poll-Fehler: '+e,'msg-err')}
}
var pollTimer;
(function(){
  var ms=parseInt(localStorage.getItem('pollInterval')||'2000');
  poll();pollTimer=setInterval(poll,ms);
})();

// ── Print actions ──
function printAction(a){
  post('/printer/print/'+a,{}).then(function(){clog('Druck: '+a,'msg-ok');poll()})
    .catch(function(e){clog('Fehler: '+e,'msg-err')});
}
function confirmCancel(){if(confirm('Druck wirklich abbrechen?'))printAction('cancel')}

// ── Axis motion ──
// axis codes: 0=X, 1=Y, 2=Z
// move_type 1=relative, distance positive/negative
function getStep(){return currentStep}
function setStep(btn,v){
  currentStep=v;
  document.querySelectorAll('.step-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('step-display').textContent=v;
}
function move(axis,dir,dist){
  // axis: 0=X,1=Y,2=Z → printer axis codes: 1=X,2=Y,3=Z
  var axisMap={0:1,1:2,2:3};
  post('/api/axis',{axis:axisMap[axis],move_type:1,distance:dir*dist})
    .then(function(){clog('Achse '+(axis===0?'X':axis===1?'Y':'Z')+' '+(dir>0?'+':'')+dir*dist+'mm','msg-ok')})
    .catch(function(e){clog('Achse-Fehler: '+e,'msg-err')});
}
function homeAll(){
  post('/api/axis',{axis:4,move_type:2,distance:0})
    .then(function(){clog('Home All','msg-ok')})
    .catch(function(e){clog('Home-Fehler: '+e,'msg-err')});
}
function homeAxis(ax){
  var m={X:1,Y:2,Z:3};
  post('/api/axis',{axis:m[ax],move_type:2,distance:0})
    .then(function(){clog('Home '+ax,'msg-ok')})
    .catch(function(e){clog('Home-Fehler: '+e,'msg-err')});
}

// ── Temperature ──
function setNozzle(){
  var v=parseFloat(document.getElementById('p-nozzle-inp').value||0);
  post('/api/temperature',{nozzle:v,bed:S.bed_target})
    .then(function(){clog('Nozzle → '+v+'°C','msg-ok')})
    .catch(function(e){clog('Temp-Fehler: '+e,'msg-err')});
}
function setBed(){
  var v=parseFloat(document.getElementById('p-bed-inp').value||0);
  post('/api/temperature',{nozzle:S.nozzle_target,bed:v})
    .then(function(){clog(T.label_bed+' → '+v+'°C','msg-ok')})
    .catch(function(e){clog('Temp-Fehler: '+e,'msg-err')});
}

// ── Light ──
function setLight(){
  var on=document.getElementById('d-light-toggle').checked;
  post('/api/light',{on:on,brightness:80})
    .then(function(){clog('Licht '+(on?'an, '+br+'%':'aus'),'msg-ok')})
    .catch(function(e){clog('Licht-Fehler: '+e,'msg-err')});
}

// ── Print Speed ──
function setSpeed(mode){
  S.print_speed_mode=mode;
  [1,2,3].forEach(function(m){
    var b=document.getElementById('d-spd-'+m);
    if(b) b.classList.toggle('spd-active',m===mode);
  });
  post('/api/speed',{mode:mode})
    .catch(function(e){clog('Speed-Fehler: '+e,'msg-err')});
}

// ── Fan ──
function setFan(){
  var v=parseInt(document.getElementById('d-fan').value);
  document.getElementById('d-fan-val').textContent=v;
  post('/api/fan',{speed:v})
    .then(function(){clog('Lüfter → '+v+'%','msg-ok')})
    .catch(function(e){clog('Lüfter-Fehler: '+e,'msg-err')});
}
function quickFan(v){
  document.getElementById('d-fan').value=v;
  document.getElementById('d-fan-val').textContent=v;
  post('/api/fan',{speed:v})
    .then(function(){clog('Lüfter → '+v+'%','msg-ok')})
    .catch(function(e){clog('Lüfter-Fehler: '+e,'msg-err')});
}

// ── AMS ──
function amsFeed(type){
  var slot=parseInt(document.getElementById('ams-slot-sel').value);
  post('/api/ams/feed',{slot_index:slot,type:type})
    .then(function(){clog((type===1?T.lbl_feed:T.lbl_unload)+' Slot '+(slot+1),'msg-ok')})
    .catch(function(e){clog('AMS-Fehler: '+e,'msg-err')});
}

// ── Camera ──
function camStart(){
  var img=document.getElementById('cam-img');
  var ph=document.getElementById('cam-placeholder');
  var sp=document.getElementById('cam-spinner');
  ph.style.display='none';
  img.style.display='none';
  sp.style.display='block';
  post('/api/camera/start',{}).then(function(){
    img.onerror=function(){
      sp.style.display='none';
      img.style.display='none';
      ph.style.display='flex';
      camOn=false;
      document.getElementById('cam-toggle-btn').textContent=T.btn_cam_start||'▶ Kamera';
      clog((T.log_error||'Fehler:')+' Stream nicht verfügbar','msg-err');
    };
    img.src='/api/camera/stream?t='+Date.now();
    camOn=true;
    document.getElementById('cam-toggle-btn').textContent=T.btn_cam_stop||'◼ Kamera';
    clog((T.log_cam_start||'Kamera gestartet'),'msg-ok');
    // MJPEG liefert kein onload – Spinner nach kurzem Timeout ausblenden
    setTimeout(function(){
      sp.style.display='none';
      img.style.display='block';
    },1200);
  }).catch(function(e){
    sp.style.display='none';
    ph.style.display='flex';
    clog((T.log_error||'Fehler:')+' '+e,'msg-err');
  });
}
function camStop(){
  post('/api/camera/stop',{}).then(function(){
    var img=document.getElementById('cam-img');
    img.src='';
    img.style.display='none';
    document.getElementById('cam-spinner').style.display='none';
    document.getElementById('cam-placeholder').style.display='flex';
    camOn=false;
    document.getElementById('cam-toggle-btn').textContent=T.btn_cam_start||'▶ Kamera';
    clog(T.log_cam_stop||'Kamera gestoppt','msg-ok');
  }).catch(function(e){clog((T.log_error||'Fehler:')+' '+e,'msg-err')});
}
function toggleCam(){if(camOn)camStop();else camStart()}
</script>
<footer style="text-align:center;padding:12px;font-size:11px;color:var(--txt2);border-top:1px solid var(--border);margin-top:auto">
  &copy; ViewIT 2026
</footer>
</body>
</html>"""
        version = self._read_version()
        html = html.replace("'__VERSION__'", f"'{version}'")
        return web.Response(text=html, content_type="text/html",
                            headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

    async def handle_api_light(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        on         = bool(body.get("on", True))
        brightness = int(body.get("brightness", self._state["light_brightness"]))
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: self.client.publish(
            "light", "control",
            {"type": 3, "status": 1 if on else 0, "brightness": brightness},
            timeout=0
        ))
        self._state["light_on"]         = on
        self._state["light_brightness"] = brightness
        return web.json_response({"result": "ok"})

    async def handle_api_fan(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        speed = int(body.get("speed", 0))
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: self.client.publish(
            "fan", "setSpeed", {"fan_speed_pct": speed}, timeout=0
        ))
        self._state["fan_speed"] = speed
        return web.json_response({"result": "ok"})

    async def handle_api_speed(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        mode = int(body.get("mode", 2))
        loop = asyncio.get_event_loop()
        taskid = self._state.get("taskid", "-1")
        await loop.run_in_executor(None, lambda: self.client.publish_web(
            "print", "update",
            {"taskid": taskid, "settings": {"print_speed_mode": mode}},
        ))
        self._state["print_speed_mode"] = mode
        return web.json_response({"result": "ok"})

    async def handle_api_ams_feed(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        slot_index = int(body.get("slot_index", 0))
        feed_type  = int(body.get("type", 1))
        # Ausziehen (type=2): wenn kein Slot explizit gewählt, den zuletzt geladenen nehmen
        if feed_type == 2 and self._ams_loaded_slot >= 0:
            slot_index = self._ams_loaded_slot
        loop = asyncio.get_event_loop()
        def _send():
            resp = self.client.publish(
                "multiColorBox", "feedFilament",
                {"multi_color_box": [{"id": -1, "feed_status": {"slot_index": slot_index, "type": feed_type}}]},
                timeout=5
            )
            log.info(f"feedFilament type={feed_type} slot={slot_index} loaded_slot={self._ams_loaded_slot} → {resp}")
        await loop.run_in_executor(None, _send)
        return web.json_response({"result": "ok"})

    async def handle_api_axis(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        axis      = int(body.get("axis", 4))
        move_type = int(body.get("move_type", 2))
        distance  = float(body.get("distance", 0))
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: self.client.publish(
            "axis", "move",
            {"axis": axis, "move_type": move_type, "distance": distance},
            timeout=0
        ))
        return web.json_response({"result": "ok"})

    async def handle_api_temperature(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        nozzle = body.get("nozzle")
        bed    = body.get("bed")
        loop = asyncio.get_event_loop()
        printing = self._state.get("print_state") == "printing"
        if printing:
            # During print: runtime update via web/printer topic, one setting at a time
            taskid = self._state.get("taskid", "-1")
            if nozzle is not None:
                n = int(float(nozzle))
                await loop.run_in_executor(None, lambda: self.client.publish_web(
                    "print", "update",
                    {"taskid": taskid, "settings": {"target_nozzle_temp": n}},
                ))
            if bed is not None:
                b = int(float(bed))
                await loop.run_in_executor(None, lambda: self.client.publish_web(
                    "print", "update",
                    {"taskid": taskid, "settings": {"target_hotbed_temp": b}},
                ))
        else:
            # Idle: standard tempature/set with both values
            n = int(float(nozzle)) if nozzle is not None else int(self._state["nozzle_target"])
            b = int(float(bed))    if bed    is not None else int(self._state["bed_target"])
            await loop.run_in_executor(None, lambda: self.client.publish(
                "tempature", "set",
                {"target_nozzle_temp": n, "target_hotbed_temp": b},
                timeout=0
            ))
        return web.json_response({"result": "ok"})

    async def handle_api_camera(self, request):
        return web.json_response({"url": self._state["camera_url"]})

    async def handle_api_camera_start(self, request):
        loop = asyncio.get_event_loop()
        # Wait for pushStarted confirmation before returning
        result = await loop.run_in_executor(None, lambda: self.client.publish(
            "video", "startCapture", None, timeout=8.0
        ))
        state = (result or {}).get("state", "")
        log.info(f"Kamera startCapture: state={state}")
        return web.json_response({"result": "ok", "state": state})

    async def handle_api_camera_stop(self, request):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: self.client.publish(
            "video", "stopCapture", None, timeout=0
        ))
        return web.json_response({"result": "ok"})

    async def handle_camera_stream(self, request):
        """MJPEG proxy: FLV → MJPEG via ffmpeg, served as multipart/x-mixed-replace."""
        url = self._state.get("camera_url", "")
        if not url:
            return web.Response(status=503, text="Keine Kamera-URL bekannt")

        boundary = "kobraxframe"
        resp = web.StreamResponse(headers={
            "Content-Type": f"multipart/x-mixed-replace;boundary={boundary}",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        })
        await resp.prepare(request)

        is_rtsp = url.lower().startswith("rtsp://")
        ffmpeg_input_args = [
            "-fflags", "nobuffer",
            "-flags", "low_delay",
        ]
        if is_rtsp:
            ffmpeg_input_args += ["-probesize", "32", "-analyzeduration", "0", "-rtsp_transport", "tcp"]
        else:
            # HTTP-FLV/HLS: braucht mehr Probe-Puffer für Container-Erkennung
            ffmpeg_input_args += ["-probesize", "1000000", "-analyzeduration", "1000000"]

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "quiet",
            *ffmpeg_input_args,
            "-i", url,
            "-vf", "fps=15,scale=640:-1",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "-q:v", "3",
            "-flush_packets", "1",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        buf = b""
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                # Extract complete JPEG frames (SOI=FFD8, EOI=FFD9)
                while True:
                    start = buf.find(b"\xff\xd8")
                    if start == -1:
                        buf = b""
                        break
                    end = buf.find(b"\xff\xd9", start + 2)
                    if end == -1:
                        buf = buf[start:]
                        break
                    frame = buf[start:end + 2]
                    buf = buf[end + 2:]
                    header = (
                        f"--{boundary}\r\n"
                        f"Content-Type: image/jpeg\r\n"
                        f"Content-Length: {len(frame)}\r\n\r\n"
                    ).encode()
                    try:
                        await resp.write(header + frame + b"\r\n")
                    except Exception:
                        return resp
        except Exception as e:
            log.warning(f"Kamera-Stream unterbrochen: {e}")
        finally:
            try:
                proc.kill()
            except Exception:
                pass

        return resp

    async def handle_serve_file(self, request):
        """Liefert hochgeladene G-Code-Dateien vom Temp-Verzeichnis (für Drucker-Download)."""
        filename = os.path.basename(request.match_info.get("filename", ""))
        serve_path = os.path.join(self._serve_dir_path, filename)
        if not os.path.isfile(serve_path):
            return web.Response(status=404, text="not found")
        size = os.path.getsize(serve_path)
        log.info(f"Drucker lädt Datei ab: {filename} ({size} bytes)")
        return web.FileResponse(serve_path, headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        })

    async def handle_api_state(self, request):
        s = self._state
        return web.json_response({
            "printer_name":     s["printer_name"],
            "firmware_version": s["firmware_version"],
            "print_state":      s["print_state"],
            "kobra_state":      s["kobra_state"],
            "nozzle_temp":      s["nozzle_temp"],
            "nozzle_target":    s["nozzle_target"],
            "bed_temp":         s["bed_temp"],
            "bed_target":       s["bed_target"],
            "progress":         s["progress"],
            "print_duration":   s["print_duration"],
            "curr_layer":       s["curr_layer"],
            "total_layers":     s["total_layers"],
            "filename":         s["filename"],
            "camera_url":       s["camera_url"],
            "fan_speed":        s["fan_speed"],
            "print_speed_mode": s["print_speed_mode"],
            "light_on":         s["light_on"],
            "light_brightness": s["light_brightness"],
            "ams_slots":        self._ams_slots,
            "ams_loaded_slot":  self._ams_loaded_slot,
            "thumbnail":        self._thumbnail_b64,
        })

    async def handle_moonraker_database(self, request):
        """OrcaSlicer 'Synchronize filament list from AMS' liest /server/database/item?namespace=lane_data"""
        namespace = request.rel_url.query.get("namespace", "")
        if namespace != "lane_data":
            return web.json_response({"result": {"namespace": namespace, "value": {}}})
        loop = asyncio.get_event_loop()
        slots = await loop.run_in_executor(None, lambda: self._get_ams_slots_fresh())
        lane_data = {}
        for i, slot in enumerate(slots):
            rgb = slot.get("color", [128, 128, 128])
            if isinstance(rgb, list) and len(rgb) >= 3:
                alpha = rgb[3] if len(rgb) == 4 else 255
                color_hex = f"{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}{alpha:02X}"
            else:
                color_hex = "808080FF"
            material = slot.get("type", "")
            default_temps = {
                "PLA":  {"nozzle": 220, "bed": 60},
                "PETG": {"nozzle": 240, "bed": 70},
                "ABS":  {"nozzle": 250, "bed": 100},
                "TPU":  {"nozzle": 230, "bed": 40},
            }
            temps = default_temps.get(material.upper(), {"nozzle": 220, "bed": 60})
            lane_data[f"lane{i}"] = {
                "vendor_name": "Anycubic",
                "name":        material,
                "color":       color_hex,
                "material":    material,
                "bed_temp":    temps["bed"],
                "nozzle_temp": temps["nozzle"],
                "scan_time":   None,
                "td":          None,
                "lane":        str(i),
                "spool_id":    None,
                "filament_id": None,
            }
        log.info(f"AMS-Sync: {len(lane_data)} Slots an OrcaSlicer")
        return web.json_response({"result": {"namespace": "lane_data", "value": lane_data}})

    def _get_ams_slots_fresh(self):
        """Frische Slot-Daten per getInfo holen, Fallback auf gecachte."""
        resp = self.client.publish("multiColorBox", "getInfo", None, timeout=5)
        if resp and resp.get("data"):
            boxes = resp["data"].get("multi_color_box") or []
            if boxes:
                slots = boxes[0].get("slots") or []
                if slots:
                    self._ams_slots = slots
        return self._ams_slots

    # ─── Settings ────────────────────────────────────────────────────────────

    def _find_env_path(self) -> pathlib.Path:
        """Gibt den Pfad zur .env-Datei zurück (neben Script oder im Parent)."""
        script_dir = pathlib.Path(__file__).parent
        for base in (script_dir, script_dir.parent):
            p = base / ".env"
            if p.is_file():
                return p
        return script_dir.parent / ".env"

    async def handle_api_settings_get(self, request):
        return web.json_response({
            "printer_ip":  self._args.printer_ip,
            "mqtt_port":   self._args.mqtt_port,
            "username":    self._args.username,
            "password":    self._args.password,
            "mode_id":     self._args.mode_id,
            "device_id":   self._args.device_id,
        })

    async def handle_api_settings_post(self, request):
        data = await request.json()
        env_path = self._find_env_path()
        # Bestehende .env einlesen um Kommentare/Extra-Keys zu erhalten
        existing: "dict[str, str]" = {}
        lines: "list[str]" = []
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    k, _, v = stripped.partition("=")
                    existing[k.strip()] = v.strip()
                lines.append(line)
        # Werte aktualisieren
        mapping = {
            "PRINTER_IP":    str(data.get("printer_ip",  existing.get("PRINTER_IP",  ""))),
            "MQTT_PORT":     str(data.get("mqtt_port",   existing.get("MQTT_PORT",   "9883"))),
            "MQTT_USERNAME": str(data.get("username",    existing.get("MQTT_USERNAME",""))),
            "MQTT_PASSWORD": str(data.get("password",    existing.get("MQTT_PASSWORD",""))),
            "MODE_ID":       str(data.get("mode_id",     existing.get("MODE_ID",      ""))),
            "DEVICE_ID":     str(data.get("device_id",   existing.get("DEVICE_ID",    ""))),
        }
        # Zeilen ersetzen oder neue Keys anhängen
        written: "set[str]" = set()
        new_lines: "list[str]" = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.partition("=")[0].strip()
                if k in mapping:
                    new_lines.append(f"{k}={mapping[k]}")
                    written.add(k)
                    continue
            new_lines.append(line)
        for k, v in mapping.items():
            if k not in written:
                new_lines.append(f"{k}={v}")
        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        log.info(f"Settings gespeichert in {env_path}")
        # Response senden, dann Neustart
        response = web.json_response({"status": "restarting"})
        asyncio.get_event_loop().call_later(0.3, self._restart_bridge)
        return response

    def _restart_bridge(self):
        log.info("Bridge wird neu gestartet …")
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # ─── Update ──────────────────────────────────────────────────────────────

    GITEA_RELEASE_API = "https://gitea.it-drui.de/api/v1/repos/viewit/KX-Bridge-Release/releases?limit=1&pre-release=true"
    GITEA_RAW_BASE    = "https://gitea.it-drui.de/viewit/KX-Bridge-Release/raw/tag"

    def _read_version(self) -> str:
        for base in (pathlib.Path(__file__).parent, pathlib.Path(__file__).parent.parent):
            p = base / "VERSION"
            if p.is_file():
                return p.read_text(encoding="utf-8").strip()
        return "unknown"

    def _write_version(self, version: str):
        for base in (pathlib.Path(__file__).parent, pathlib.Path(__file__).parent.parent):
            p = base / "VERSION"
            if p.is_file():
                p.write_text(version + "\n", encoding="utf-8")
                return
        # Fallback: neben dem Script
        (pathlib.Path(__file__).parent.parent / "VERSION").write_text(version + "\n", encoding="utf-8")

    @staticmethod
    def _parse_version(v: str) -> "tuple[int, ...]":
        """'v0.9.1-beta1' → (0, 9, 1)  –  nur numerische Teile vor dem ersten '-'"""
        v = v.lstrip("v").split("-")[0]
        parts = re.split(r"[.\s]+", v)
        result = []
        for p in parts:
            try:
                result.append(int(p))
            except ValueError:
                break
        return tuple(result) or (0,)

    async def handle_api_update_check(self, request):
        current = self._read_version()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.GITEA_RELEASE_API, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return web.json_response({"error": f"Gitea HTTP {resp.status}"}, status=502)
                    releases = await resp.json(content_type=None)
            if not releases:
                return web.json_response({"error": "Keine Releases gefunden"}, status=404)
            data = releases[0]
            tag = data.get("tag_name", "")
            latest = tag.lstrip("v")
            update_available = self._parse_version(tag) > self._parse_version(current)
            download_url = f"{self.GITEA_RAW_BASE}/{tag}/kobrax_moonraker_bridge.py"
            return web.json_response({
                "current":          current,
                "latest":           latest,
                "update_available": update_available,
                "tag":              tag,
                "download_url":     download_url,
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=502)

    async def handle_api_update_apply(self, request):
        data = await request.json()
        download_url = data.get("download_url", "")
        new_tag      = data.get("tag", "")
        if not download_url:
            return web.json_response({"error": "download_url fehlt"}, status=400)
        script_path = pathlib.Path(__file__).resolve()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(download_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        return web.json_response({"error": f"Download HTTP {resp.status}"}, status=502)
                    content = await resp.read()
            # Atomisch ersetzen
            tmp = script_path.with_suffix(".py.new")
            tmp.write_bytes(content)
            os.replace(tmp, script_path)
            if new_tag:
                self._write_version(new_tag.lstrip("v"))
            log.info(f"Update auf {new_tag} installiert, starte neu …")
        except Exception as e:
            return web.json_response({"error": str(e)}, status=502)
        response = web.json_response({"status": "updating"})
        asyncio.get_event_loop().call_later(0.3, self._restart_bridge)
        return response

    async def handle_catchall(self, request):
        body = await request.read()
        log.warning(f"UNBEKANNT {request.method} {request.path_qs}  body={body[:200]}")
        return web.json_response({"result": {}}, status=200)

    async def handle_favicon(self, request):
        # Minimales 1x1 ICO damit der Browser nicht 404 loggt
        ico = bytes([
            0,0,1,0,1,0,1,1,0,0,1,0,24,0,40,0,0,0,22,0,0,0,40,0,0,0,
            1,0,0,0,2,0,0,0,1,0,24,0,0,0,0,0,4,0,0,0,0,0,0,0,0,0,0,0,
            0,0,0,0,0,0,0,0,255,102,0,0,0,0,0,0
        ])
        return web.Response(body=ico, content_type="image/x-icon")

    # -------------------------------------------------------------------------
    # WebSocket handler
    # -------------------------------------------------------------------------

    async def handle_websocket(self, request):
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        ws._loop = asyncio.get_event_loop()
        self.ws_clients.add(ws)
        log.info(f"WS client verbunden ({len(self.ws_clients)} gesamt)")

        # Send klippy_ready notification
        await ws.send_str(json.dumps({
            "jsonrpc": "2.0",
            "method":  "notify_klippy_ready",
            "params":  [],
        }))
        # Send initial status
        await ws.send_str(json.dumps({
            "jsonrpc": "2.0",
            "method":  "notify_status_update",
            "params":  [self._build_printer_objects(), time.time()],
        }))

        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await self._handle_ws_rpc(ws, msg.data)
            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break

        self.ws_clients.discard(ws)
        log.info(f"WS client getrennt ({len(self.ws_clients)} verbleibend)")
        return ws

    async def _handle_ws_rpc(self, ws: web.WebSocketResponse, raw: str):
        try:
            req = json.loads(raw)
        except Exception:
            return
        rpc_id = req.get("id")
        method  = req.get("method", "")
        log.info(f"WS RPC: {method}  params={str(req.get('params',''))[:120]}")
        params  = req.get("params") or {}
        if isinstance(params, list):
            params = params[0] if params else {}

        result = None
        error  = None

        try:
            if method in ("printer.info", "printer_info"):
                result = {
                    "state":           "ready",
                    "state_message":   "Printer is ready",
                    "hostname":        "kobrax-bridge",
                    "software_version": KLIPPER_VERSION,
                    "cpu_info":        self._state["printer_name"],
                    "klipper_path":    "/home/pi/klipper",
                    "python_path":     "/home/pi/klippy-env/bin/python",
                }
            elif method in ("server.info", "server_info"):
                result = {
                    "klippy_connected": True,
                    "klippy_state":     "ready",
                    "moonraker_version": MOONRAKER_VERSION,
                    "components":       [],
                    "failed_components": [],
                    "registered_directories": ["gcodes"],
                    "warnings":         [],
                }
            elif method in ("printer.objects.list",):
                result = {"objects": list(self._build_printer_objects().keys())}
            elif method in ("printer.objects.query", "printer.objects.get"):
                objects = params.get("objects", {})
                all_objs = self._build_printer_objects()
                if objects:
                    filtered = {k: all_objs.get(k, {}) for k in objects}
                else:
                    filtered = all_objs
                result = {"status": filtered, "eventtime": time.time()}
            elif method == "printer.objects.subscribe":
                objects = params.get("objects", {})
                all_objs = self._build_printer_objects()
                if objects:
                    filtered = {k: all_objs.get(k, {}) for k in objects}
                else:
                    filtered = all_objs
                result = {"status": filtered, "eventtime": time.time()}
            elif method == "printer.print.start":
                filename = params.get("filename", self._last_uploaded_file)
                loop = asyncio.get_event_loop()
                resp = await loop.run_in_executor(
                    None, lambda: self.client.publish("print", "start",
                        {"filename": filename, "use_ams": False}, timeout=15.0)
                )
                result = "ok" if resp else "timeout"
            elif method == "printer.print.pause":
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self.client.pause_print)
                result = "ok"
            elif method == "printer.print.resume":
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self.client.resume_print)
                result = "ok"
            elif method == "printer.print.cancel":
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self.client.stop_print)
                result = "ok"
            elif method == "machine.system_info":
                result = {"system_info": {"cpu_info": {"cpu_desc": "Kobra X Bridge"}}}
            elif method == "server.files.list":
                result = []
            else:
                log.debug(f"Unbekannte RPC-Methode: {method}")
                result = {}
        except Exception as e:
            log.error(f"RPC-Fehler für {method}: {e}")
            error = {"code": -32603, "message": str(e)}

        if rpc_id is not None:
            response = {"jsonrpc": "2.0", "id": rpc_id}
            if error:
                response["error"] = error
            else:
                response["result"] = result
            await ws.send_str(json.dumps(response))

    # -------------------------------------------------------------------------
    # Poll loop (sync, runs in executor)
    # -------------------------------------------------------------------------

    def _printer_reachable(self) -> bool:
        """TCP-Probe auf den MQTT-Port – kein ICMP nötig, kein root erforderlich."""
        import socket as _socket
        try:
            with _socket.create_connection(
                (self._args.printer_ip, self._args.mqtt_port), timeout=2.0
            ):
                return True
        except OSError:
            return False

    def _poll_loop(self, stop_event: threading.Event):
        _offline = False          # True = Drucker zuletzt nicht erreichbar
        _probe_interval = 10.0   # Sekunden zwischen TCP-Probes im Offline-Modus

        while not stop_event.is_set():
            # ── Offline-Modus: warten bis Drucker wieder erreichbar ──────────
            if _offline:
                if self._printer_reachable():
                    log.info("Drucker erreichbar – stelle MQTT-Verbindung her …")
                    try:
                        self.client.connect()
                        _offline = False
                        self._state["print_state"] = "standby"
                        self._state["kobra_state"] = "free"
                        log.info("MQTT-Verbindung wiederhergestellt")
                    except Exception as e:
                        log.warning(f"Verbindungsaufbau fehlgeschlagen: {e}")
                        stop_event.wait(_probe_interval)
                        continue
                else:
                    stop_event.wait(_probe_interval)
                    continue

            # ── Online-Modus: normaler Poll ──────────────────────────────────
            try:
                info = self.client.query_info()
                if info:
                    self._on_info(info)
                # Während Druck: print/report direkt abfragen
                if self._state["print_state"] in ("printing", "preheating",
                                                   "auto_leveling", "checking", "init"):
                    print_r = self.client.publish("print", "query", timeout=3.0)
                    if print_r:
                        self._on_print(print_r)
                box = self.client.query_multicolor_box()
                if box:
                    boxes = (box.get("data") or {}).get("multi_color_box") or []
                    slots = boxes[0].get("slots") or [] if boxes else []
                    if slots:
                        self._ams_slots = slots
            except Exception as e:
                log.warning(f"Poll-Fehler: {e}")
                # Prüfen ob Drucker wirklich weg ist
                if not self._printer_reachable():
                    log.info("Drucker nicht erreichbar – wechsle in Offline-Modus")
                    self._state["print_state"] = "error"
                    self._state["kobra_state"] = "offline"
                    try:
                        self.client.disconnect()
                    except Exception:
                        pass
                    _offline = True
            stop_event.wait(3.0)


# ---------------------------------------------------------------------------
# App factory + main
# ---------------------------------------------------------------------------

def build_app(bridge: KobraXBridge) -> web.Application:
    app = web.Application()
    r = app.router

    # Moonraker API
    r.add_get("/server/info",                bridge.handle_server_info)
    r.add_get("/printer/info",               bridge.handle_printer_info)
    r.add_get("/machine/system_info",        bridge.handle_machine_system_info)
    r.add_get("/printer/objects/list",       bridge.handle_objects_list)
    r.add_get("/printer/objects/query",      bridge.handle_objects_query)
    r.add_get("/printer/objects/subscribe",  bridge.handle_objects_subscribe)
    r.add_post("/printer/objects/subscribe", bridge.handle_objects_subscribe)
    r.add_get("/server/files/list",          bridge.handle_files_list)
    r.add_post("/server/files/upload",       bridge.handle_file_upload)
    r.add_post("/printer/print/start",       bridge.handle_print_start)
    r.add_post("/printer/print/pause",       bridge.handle_print_pause)
    r.add_post("/printer/print/resume",      bridge.handle_print_resume)
    r.add_post("/printer/print/cancel",      bridge.handle_print_cancel)

    # OctoPrint compatibility (OrcaSlicer probes this + uploads here)
    r.add_get("/api/version",                bridge.handle_octoprint_version)
    r.add_post("/api/files/local",           bridge.handle_file_upload)
    r.add_post("/api/files/{path:.*}",       bridge.handle_file_upload)

    # Moonraker database (OrcaSlicer AMS-Sync)
    r.add_get("/server/database/item",       bridge.handle_moonraker_database)

    # New API endpoints
    r.add_post("/api/light",               bridge.handle_api_light)
    r.add_post("/api/fan",                 bridge.handle_api_fan)
    r.add_post("/api/speed",               bridge.handle_api_speed)
    r.add_post("/api/ams/feed",            bridge.handle_api_ams_feed)
    r.add_post("/api/axis",                bridge.handle_api_axis)
    r.add_post("/api/temperature",         bridge.handle_api_temperature)
    r.add_get("/api/camera",               bridge.handle_api_camera)
    r.add_get("/api/camera/stream",        bridge.handle_camera_stream)
    r.add_post("/api/camera/start",        bridge.handle_api_camera_start)
    r.add_post("/api/camera/stop",         bridge.handle_api_camera_stop)
    r.add_get("/api/state",                bridge.handle_api_state)
    r.add_get("/api/settings",             bridge.handle_api_settings_get)
    r.add_post("/api/settings",            bridge.handle_api_settings_post)
    r.add_get("/api/update/check",         bridge.handle_api_update_check)
    r.add_post("/api/update/apply",        bridge.handle_api_update_apply)
    r.add_get("/serve/{filename}",         bridge.handle_serve_file)

    # Root + favicon (OrcaSlicer öffnet / in eingebettetem Browser)
    r.add_get("/",                           bridge.handle_index)
    r.add_get("/favicon.ico",               bridge.handle_favicon)

    # WebSocket
    r.add_get("/websocket",                  bridge.handle_websocket)

    # Catch-all: alle unbekannten Requests loggen statt 404
    r.add_route("*", "/{path:.*}",           bridge.handle_catchall)

    return app


async def run_bridge(args):
    log.info(f"Verbinde mit Drucker {args.printer_ip}:{args.mqtt_port} …")
    client = KobraXClient(
        host=args.printer_ip,
        port=args.mqtt_port,
        username=args.username,
        password=args.password,
        mode_id=args.mode_id,
        device_id=args.device_id,
        client_id="kobrax_bridge",
    )

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, client.connect)
    log.info("MQTT verbunden")

    bridge  = KobraXBridge(client, args=args)
    app     = build_app(bridge)

    stop_event = threading.Event()
    poll_thread = threading.Thread(
        target=bridge._poll_loop, args=(stop_event,), daemon=True, name="poll"
    )
    poll_thread.start()

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()

    log.info(f"Bridge läuft auf http://{args.host}:{args.port}")
    log.info(f"OrcaSlicer → Klipper → Host: {args.host}  Port: {args.port}")
    log.info("Ctrl-C zum Beenden")

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        stop_event.set()
        await runner.cleanup()
        client.disconnect()
        log.info("Bridge beendet")


def main():
    parser = argparse.ArgumentParser(description="Moonraker-Bridge für Anycubic Kobra X")
    parser.add_argument("--printer-ip",  default=env_loader.PRINTER_IP,
                        help="IP-Adresse des Druckers")
    parser.add_argument("--mqtt-port",   type=int, default=env_loader.MQTT_PORT)
    parser.add_argument("--username",    default=env_loader.USERNAME)
    parser.add_argument("--password",    default=env_loader.PASSWORD)
    parser.add_argument("--mode-id",     default=env_loader.MODE_ID)
    parser.add_argument("--device-id",   default=env_loader.DEVICE_ID)
    parser.add_argument("--host",        default="0.0.0.0",
                        help="Bind-Adresse für den Bridge-Server")
    parser.add_argument("--port",        type=int, default=7125,
                        help="HTTP/WS-Port (Moonraker-Standard: 7125)")
    args = parser.parse_args()

    asyncio.run(run_bridge(args))


if __name__ == "__main__":
    main()
