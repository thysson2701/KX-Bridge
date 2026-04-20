"""
kobrax_client.py – Anycubic Kobra X LAN-MQTT-Client

Protokoll vollständig rekonstruiert via Sniffer 2026-04-17 (953 Nachrichten).

Voraussetzungen:
  - /tmp/anycubic_slicer.crt und .key (aus cloud_mqtt.dll @ 0x2ed5b0 / 0x2edce0)
  - Drucker im LAN-Modus erreichbar auf Port 9883

Verwendung:
  client = KobraXClient(env_loader.PRINTER_IP, mode_id=env_loader.MODE_ID,
                        device_id=env_loader.DEVICE_ID)
  client.connect()
  info = client.query_info()
  print(info["data"]["temp"])
  client.disconnect()
"""

import json
import os
import socket
import ssl
import threading
import time
import uuid
from datetime import datetime

import env_loader

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CERT_FILE = os.path.join(_SCRIPT_DIR, "anycubic_slicer.crt")
KEY_FILE  = os.path.join(_SCRIPT_DIR, "anycubic_slicer.key")


# ---------------------------------------------------------------------------
# Low-level MQTT framing
# ---------------------------------------------------------------------------

def _enc_str(s: str) -> bytes:
    b = s.encode("utf-8")
    return len(b).to_bytes(2, "big") + b


def _enc_len(n: int) -> bytes:
    out = bytearray()
    while True:
        d = n % 128
        n //= 128
        if n > 0:
            d |= 0x80
        out.append(d)
        if n == 0:
            break
    return bytes(out)


def _build_connect(client_id: str, username: str, password: str) -> bytes:
    proto = b"\x00\x04MQTT\x04"
    ka    = b"\x00\x3c"           # keepalive = 60s
    flags = 0xC2                  # username + password, clean session
    payload = _enc_str(client_id) + _enc_str(username) + _enc_str(password)
    body = proto + bytes([flags]) + ka + payload
    return bytes([0x10]) + _enc_len(len(body)) + body


def _build_subscribe(topic: str, pid: int) -> bytes:
    p = pid.to_bytes(2, "big") + _enc_str(topic) + b"\x00"
    return bytes([0x82]) + _enc_len(len(p)) + p


def _build_publish(topic: str, payload: str) -> bytes:
    body = _enc_str(topic) + payload.encode("utf-8")
    return bytes([0x30]) + _enc_len(len(body)) + body


def _build_pingreq() -> bytes:
    return bytes([0xC0, 0x00])


def _parse_publish(pkt: bytes):
    if len(pkt) < 2:
        return None, None
    tlen = (pkt[0] << 8) | pkt[1]
    if 2 + tlen > len(pkt):
        return None, None
    topic   = pkt[2:2 + tlen].decode("utf-8", errors="replace")
    payload = pkt[2 + tlen:]
    return topic, payload


# ---------------------------------------------------------------------------
# KobraXClient
# ---------------------------------------------------------------------------

class KobraXClient:
    def __init__(self, host: str, username: str, password: str,
                 mode_id: str, device_id: str,
                 port: int = 9883, client_id: str = "kobrax_py"):
        self.host      = host
        self.port      = port
        self.username  = username
        self.password  = password
        self.mode_id   = mode_id
        self.device_id = device_id
        self.client_id = client_id

        self._sock    = None
        self._buf     = b""
        self._pid     = 1
        self._lock    = threading.Lock()
        self._running = False

        # Pending requests by msgid (for response ACK)
        self._pending_msgid: dict[str, dict] = {}
        # Pending requests by msg_type/report topic suffix
        self._pending_report: dict[str, dict] = {}

        # Optional callbacks: topic_suffix → callable(payload_dict)
        self.callbacks: dict[str, callable] = {}

    # -- Topics --------------------------------------------------------------

    def _pub_topic(self, msg_type: str) -> str:
        return (f"anycubic/anycubicCloud/v1/slicer/printer/"
                f"{self.mode_id}/{self.device_id}/{msg_type}")

    def _sub_topic(self) -> str:
        return (f"anycubic/anycubicCloud/v1/printer/public/"
                f"{self.mode_id}/{self.device_id}/#")

    # -- Connection ----------------------------------------------------------

    def _do_connect(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        ctx.load_cert_chain(CERT_FILE, KEY_FILE)

        raw        = socket.create_connection((self.host, self.port), timeout=5)
        self._sock = ctx.wrap_socket(raw)
        print(f"[kobrax] TLS: {self._sock.cipher()[0]}")

        self._sock.sendall(_build_connect(self.client_id, self.username, self.password))
        self._sock.settimeout(3)
        r = self._sock.recv(64)
        if len(r) < 4 or r[0] != 0x20 or r[3] != 0:
            raise RuntimeError(f"CONNACK failed: {r.hex()}")
        print(f"[kobrax] CONNACK rc=0")

        self._sock.settimeout(0.2)
        self._buf = b""
        self._subscribe(self._sub_topic())

    def connect(self):
        self._do_connect()
        self._running = True
        t = threading.Thread(target=self._read_loop, daemon=True)
        t.start()
        time.sleep(0.3)

    def disconnect(self):
        self._running = False
        try:
            self._sock.close()
        except Exception:
            pass

    def _reconnect(self):
        print("[kobrax] Verbindung verloren – reconnect…")
        try:
            self._sock.close()
        except Exception:
            pass
        for delay in [2, 4, 8, 15, 30]:
            try:
                self._do_connect()
                print("[kobrax] Reconnect erfolgreich")
                return True
            except Exception as e:
                print(f"[kobrax] Reconnect fehlgeschlagen ({e}), warte {delay}s…")
                time.sleep(delay)
        return False

    def _subscribe(self, topic: str):
        with self._lock:
            pid = self._pid
            self._pid += 1
            self._sock.sendall(_build_subscribe(topic, pid))
        print(f"[kobrax] SUB {topic}")

    # -- Read loop -----------------------------------------------------------

    def _read_loop(self):
        last_ping = time.time()
        while self._running:
            if time.time() - last_ping > 30:
                with self._lock:
                    try:
                        self._sock.sendall(_build_pingreq())
                    except Exception:
                        if self._running and not self._reconnect():
                            break
                        last_ping = time.time()
                        continue
                last_ping = time.time()
            try:
                data = self._sock.recv(65536)
                if not data:
                    raise ConnectionResetError("EOF")
                self._buf += data
                self._drain()
            except ssl.SSLWantReadError:
                continue
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    print(f"[kobrax] reader error: {e}")
                    if not self._reconnect():
                        break
                    last_ping = time.time()
                else:
                    break

    def _drain(self):
        buf = self._buf
        idx = 0
        while idx < len(buf):
            ptype = buf[idx] & 0xF0
            i = idx + 1
            mul = 1
            rem = 0
            while i < len(buf):
                b = buf[i]
                rem += (b & 0x7F) * mul
                mul *= 128
                i += 1
                if not (b & 0x80):
                    break
            if i + rem > len(buf):
                break
            pkt = buf[i:i + rem]
            idx = i + rem

            if ptype == 0x30:
                topic, raw_payload = _parse_publish(pkt)
                if topic is None:
                    continue
                try:
                    payload = json.loads(raw_payload)
                except Exception:
                    payload = {"_raw": raw_payload.decode("utf-8", errors="replace")}
                self._dispatch(topic, payload)

        self._buf = buf[idx:]

    def _dispatch(self, topic: str, payload: dict):
        # Resolve by report topic suffix (e.g. "info/report")
        suffix = "/".join(topic.split("/")[-2:])
        if suffix in self._pending_report:
            entry = self._pending_report[suffix]
            entry["result"] = payload
            entry["event"].set()

        # Resolve by msgid (for generic response ACK)
        msgid = payload.get("msgid")
        if msgid and msgid in self._pending_msgid:
            entry = self._pending_msgid[msgid]
            entry["result"] = payload
            entry["event"].set()

        # User callbacks by topic suffix (last two path components)
        suffix = "/".join(topic.split("/")[-2:])
        if suffix in self.callbacks:
            try:
                self.callbacks[suffix](payload)
            except Exception as e:
                print(f"[kobrax] callback error for {suffix}: {e}")

        # Generic wildcard callback
        if "*" in self.callbacks:
            try:
                self.callbacks["*"](topic, payload)
            except Exception as e:
                print(f"[kobrax] wildcard callback error: {e}")

    # -- Publish + request/response ------------------------------------------

    def publish(self, msg_type: str, action: str, data=None, timeout: float = 5.0) -> dict | None:
        msgid   = str(uuid.uuid4())
        payload = json.dumps({
            "type":      msg_type,
            "action":    action,
            "msgid":     msgid,
            "timestamp": int(time.time() * 1000),
            "data":      data,
        }, separators=(",", ":"))

        # Wait by msgid only — avoids collisions when multiple threads
        # call publish() for the same msg_type concurrently.
        # Also register by report topic as fallback for responses without msgid.
        report_key = f"{msg_type}/report"
        event  = threading.Event()
        entry  = {"event": event, "result": None}
        self._pending_msgid[msgid] = entry
        # Only register report-key waiter if nobody else is waiting on it
        report_registered = False
        if report_key not in self._pending_report:
            self._pending_report[report_key] = entry
            report_registered = True

        topic = self._pub_topic(msg_type)
        try:
            with self._lock:
                self._sock.sendall(_build_publish(topic, payload))
        except Exception as e:
            print(f"[kobrax] send error: {e}, reconnecting…")
            self._pending_msgid.pop(msgid, None)
            if report_registered:
                self._pending_report.pop(report_key, None)
            if not self._reconnect():
                return None
            # retry once after reconnect
            try:
                with self._lock:
                    self._sock.sendall(_build_publish(topic, payload))
                self._pending_msgid[msgid] = entry
                if report_registered:
                    self._pending_report[report_key] = entry
            except Exception:
                return None

        if timeout <= 0:
            self._pending_msgid.pop(msgid, None)
            if report_registered:
                self._pending_report.pop(report_key, None)
            return None

        received = event.wait(timeout)
        self._pending_msgid.pop(msgid, None)
        if report_registered:
            self._pending_report.pop(report_key, None)
        if not received:
            return None
        return entry["result"]

    # -- High-level commands -------------------------------------------------

    def query_info(self) -> dict | None:
        return self.publish("info", "query")

    def query_status(self) -> dict | None:
        return self.publish("status", "query")

    def query_multicolor_box(self) -> dict | None:
        return self.publish("multiColorBox", "getInfo")

    def set_temperature(self, nozzle: int, bed: int) -> dict | None:
        return self.publish("tempature", "set",
                            {"target_nozzle_temp": nozzle, "target_hotbed_temp": bed})

    def set_fan(self, pct: int) -> dict | None:
        return self.publish("fan", "set", {"fan_speed_pct": pct})

    def set_light(self, on: bool, brightness: int = 80) -> dict | None:
        return self.publish("light", "control",
                            {"type": 2, "status": 1 if on else 0, "brightness": brightness})

    def start_camera(self) -> dict | None:
        return self.publish("video", "startCapture")

    def stop_camera(self) -> dict | None:
        return self.publish("video", "stopCapture")

    def pause_print(self) -> dict | None:
        return self.publish("print", "pause")

    def resume_print(self) -> dict | None:
        return self.publish("print", "resume")

    def stop_print(self) -> dict | None:
        return self.publish("print", "stop")

    # -- G-Code Upload -------------------------------------------------------

    def upload_gcode(self, filepath: str, remote_filename: str | None = None,
                     upload_url: str | None = None) -> dict:
        """Upload a G-Code or .3mf file via HTTP POST to port 18910.

        Returns the parsed JSON response from the printer.
        Raises RuntimeError on HTTP or connection errors.

        Protocol captured via Wireshark 2026-04-18:
          POST /gcode_upload?s={session_token}
          Multipart fields: 'filename' (text) + 'gcode' (file bytes)
          Required headers: X-File-Length, X-BBL-* (BambuLab heritage)
        """
        if not upload_url:
            info = self.query_info()
            if not info:
                raise RuntimeError("Could not get info/report for upload URL")
            upload_url = info["data"]["urls"]["fileUploadurl"]
        # parse token from URL query string
        token = upload_url.split("?s=")[1] if "?s=" in upload_url else ""

        with open(filepath, "rb") as f:
            file_data = f.read()

        if remote_filename is None:
            remote_filename = os.path.basename(filepath)

        boundary = "------------------------a3a050b927d92a4c"
        sep = f"--{boundary}\r\n".encode()
        end = f"--{boundary}--\r\n".encode()

        part_filename = (
            sep +
            f'Content-Disposition: form-data; name="filename"\r\n\r\n'.encode() +
            remote_filename.encode() + b"\r\n"
        )
        part_gcode = (
            sep +
            f'Content-Disposition: form-data; name="gcode"; filename="{remote_filename}"\r\n'
            f'Content-Type: application/octet-stream\r\n\r\n'.encode() +
            file_data + b"\r\n"
        )
        body = part_filename + part_gcode + end

        headers = (
            f"POST /gcode_upload?s={token} HTTP/1.1\r\n"
            f"Host: {self.host}:18910\r\n"
            f"User-Agent: AnycubicSlicerNext/1.3.9.4\r\n"
            f"Accept: */*\r\n"
            f"X-BBL-Client-Name: AnycubicSlicerNext\r\n"
            f"X-BBL-Client-Type: slicer\r\n"
            f"X-BBL-Client-Version: 01.03.09.04\r\n"
            f"X-BBL-Device-ID: {str(uuid.uuid4())}\r\n"
            f"X-BBL-Language: de-DE\r\n"
            f"X-BBL-OS-Type: windows\r\n"
            f"X-BBL-OS-Version: 10.0.26200\r\n"
            f"X-File-Length: {len(file_data)}\r\n"
            f"Content-Type: multipart/form-data; boundary={boundary}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode()

        sock = socket.create_connection((self.host, 18910), timeout=30)
        sock.sendall(headers + body)
        sock.settimeout(10)
        response = b""
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                response += chunk
        except socket.timeout:
            pass
        sock.close()

        # parse HTTP response body
        if b"\r\n\r\n" in response:
            body_start = response.index(b"\r\n\r\n") + 4
            resp_body = response[body_start:]
        else:
            resp_body = response
        try:
            return json.loads(resp_body)
        except Exception:
            raise RuntimeError(f"Upload: unerwartete Antwort: {resp_body[:200]}")

    def move_axis(self, axis: int, move_type: int = 2, distance: int = 0) -> dict | None:
        return self.publish("axis", "move",
                            {"axis": axis, "move_type": move_type, "distance": distance})

    def home_all(self) -> dict | None:
        # axis=4 move_type=2 = Home all axes (~4-15s)
        return self.publish("axis", "move", {"axis": 4, "move_type": 2, "distance": 0}, timeout=30.0)

    def home_axis(self, axis: int) -> dict | None:
        # axis: 1=Y, 2=X, 3=Z
        return self.publish("axis", "move", {"axis": axis, "move_type": 2, "distance": 0}, timeout=30.0)

    def jog(self, axis: int, direction: int, distance_mm: int = 1) -> dict | None:
        # axis: 1=Y, 2=X, 3=Z  direction: 0=neg, 1=pos
        return self.move_axis(axis=axis, move_type=direction, distance=distance_mm)


# ---------------------------------------------------------------------------
# CLI Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Anycubic Kobra X LAN-Client")
    parser.add_argument("--ip",        default=env_loader.PRINTER_IP)
    parser.add_argument("--port",      type=int, default=env_loader.MQTT_PORT)
    parser.add_argument("--username",  default=env_loader.USERNAME)
    parser.add_argument("--password",  default=env_loader.PASSWORD)
    parser.add_argument("--mode-id",   default=env_loader.MODE_ID)
    parser.add_argument("--device-id", default=env_loader.DEVICE_ID)
    parser.add_argument("--monitor",   action="store_true",
                        help="Dauerhaft mithören und alle Reports ausgeben")
    args = parser.parse_args()

    client = KobraXClient(
        host=args.ip, port=args.port,
        username=args.username, password=args.password,
        mode_id=args.mode_id, device_id=args.device_id,
    )

    if args.monitor:
        def on_msg(topic, payload):
            suffix = "/".join(topic.split("/")[-2:])
            ts = datetime.now().strftime("%H:%M:%S")
            state = payload.get("state", "")
            data  = payload.get("data") or {}
            if "progress" in data:
                print(f"[{ts}] {suffix:25}  state={state:12}  progress={data['progress']}%  layer={data.get('curr_layer','?')}/{data.get('total_layers','?')}")
            elif "curr_nozzle_temp" in data:
                print(f"[{ts}] {suffix:25}  nozzle={data['curr_nozzle_temp']}°C/{data.get('target_nozzle_temp',0)}°C  bed={data['curr_hotbed_temp']}°C/{data.get('target_hotbed_temp',0)}°C")
            else:
                print(f"[{ts}] {suffix:25}  state={state}")

        client.callbacks["*"] = on_msg
        client.connect()
        print("[kobrax] Monitor-Modus aktiv (Ctrl-C zum Beenden)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        client.disconnect()
    else:
        client.connect()

        print("\n--- query_info ---")
        info = client.query_info()
        if info:
            d = info.get("data", {})
            print(f"  Drucker:  {d.get('printerName')}  FW {d.get('version')}")
            print(f"  Status:   {d.get('state')}")
            t = d.get("temp", {})
            print(f"  Nozzle:   {t.get('curr_nozzle_temp')}°C → {t.get('target_nozzle_temp')}°C")
            print(f"  Bett:     {t.get('curr_hotbed_temp')}°C → {t.get('target_hotbed_temp')}°C")
            urls = d.get("urls", {})
            print(f"  Upload:   {urls.get('fileUploadurl')}")
            print(f"  Kamera:   {urls.get('rtspUrl')}")
        else:
            print("  Keine Antwort")

        client.disconnect()
