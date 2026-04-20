"""
extract_credentials.py – Extrahiert Anycubic LAN-MQTT-Credentials aus dem RAM
des laufenden AnycubicSlicerNext-Prozesses.

Voraussetzungen:
  - AnycubicSlicerNext läuft und ist mit dem Drucker verbunden
  - Gleiches Benutzerkonto wie der Slicer-Prozess (kein Admin nötig)

Verwendung:
  python3 extract_credentials.py [--write-env] [--env-file ../.env]

Funktionsweise:
  1. Prozess "AnycubicSlicer.exe" (Windows) bzw. "AnycubicSlicer" (Linux) finden
  2. Speicherseiten des Prozesses durchsuchen (nur r/rw, keine Exec-Pages)
  3. Nach MQTT-Credential-Patterns suchen:
       Username:  user[A-Za-z0-9]{8,12}
       Password:  [A-Za-z0-9+/]{13,18}
       Drucker-IP: d{1,3}.d{1,3}.d{1,3}.d{1,3}
  4. Kandidaten nach Plausibilität filtern und ausgeben
  5. Optional: .env-Datei schreiben
"""

import argparse
import os
import re
import struct
import sys
import platform

# ---------------------------------------------------------------------------
# Plattform-Erkennung
# ---------------------------------------------------------------------------

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX   = platform.system() == "Linux"

# ---------------------------------------------------------------------------
# Pattern
# ---------------------------------------------------------------------------

# Username: "user" + 8–12 alphanumerische Zeichen (drucker-generiert)
RE_USERNAME = re.compile(rb'user[A-Za-z0-9]{8,12}(?=[^A-Za-z0-9]|$)')

# Password: 13–20 alphanumerische Zeichen (kein / da kein RTSP-Pfad)
# Anycubic-Passwörter: gemischte Groß/Klein/Ziffern, kein Slash
RE_PASSWORD = re.compile(rb'[A-Za-z0-9]{13,20}(?=[^A-Za-z0-9]|$)')

# Kontext-Pattern: sucht Passwort das direkt nach "password" im Speicher steht
RE_PASSWORD_CTX = re.compile(rb'(?:password|passwd|Password)\x00{0,4}([A-Za-z0-9]{10,25})(?=[^A-Za-z0-9]|$)', re.IGNORECASE)

# Proximity-Pattern: Username gefolgt von Passwort in naher Umgebung (<512 Bytes)
RE_USER_PASS_PROXIMITY = re.compile(
    rb'(user[A-Za-z0-9]{8,12}).{1,512}?([A-Za-z0-9]{13,20})(?=[^A-Za-z0-9]|$)',
    re.DOTALL
)

# IPv4-Adresse (kein localhost, kein Broadcast)
RE_IP = re.compile(rb'(?<![.\d])(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?![.\d])')

# mode_id: 5-stellige Zahl (z.B. 20030)
RE_MODE_ID = re.compile(rb'(?<!\d)(2\d{4})(?!\d)')

# device_id: 32 Hex-Zeichen (MD5-Format)
RE_DEVICE_ID = re.compile(rb'[0-9a-f]{32}(?=[^0-9a-f]|$)')


# ---------------------------------------------------------------------------
# Windows – Speicher lesen via ctypes / ReadProcessMemory
# ---------------------------------------------------------------------------

def _win_find_pid(name: str) -> "int | None":
    """Findet die PID eines Prozesses anhand des Namens (case-insensitive)."""
    import ctypes
    import ctypes.wintypes

    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize",              ctypes.wintypes.DWORD),
            ("cntUsage",            ctypes.wintypes.DWORD),
            ("th32ProcessID",       ctypes.wintypes.DWORD),
            ("th32DefaultHeapID",   ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID",        ctypes.wintypes.DWORD),
            ("cntThreads",          ctypes.wintypes.DWORD),
            ("th32ParentProcessID", ctypes.wintypes.DWORD),
            ("pcPriClassBase",      ctypes.c_long),
            ("dwFlags",             ctypes.wintypes.DWORD),
            ("szExeFile",           ctypes.c_char * 260),
        ]

    snap = ctypes.windll.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == ctypes.wintypes.HANDLE(-1).value:
        return None

    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)

    try:
        if not ctypes.windll.kernel32.Process32First(snap, ctypes.byref(entry)):
            return None
        while True:
            if entry.szExeFile.decode("utf-8", errors="replace").lower() == name.lower():
                return entry.th32ProcessID
            if not ctypes.windll.kernel32.Process32Next(snap, ctypes.byref(entry)):
                break
    finally:
        ctypes.windll.kernel32.CloseHandle(snap)
    return None


def _win_read_memory(pid: int, chunk_size: int = 0x10000) -> "list[bytes]":
    """Liest alle lesbaren Speicherseiten eines Windows-Prozesses."""
    import ctypes
    import ctypes.wintypes

    PROCESS_VM_READ          = 0x0010
    PROCESS_QUERY_INFORMATION = 0x0400
    MEM_COMMIT  = 0x1000
    PAGE_NOACCESS   = 0x01
    PAGE_GUARD      = 0x100

    class MEMORY_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BaseAddress",       ctypes.c_void_p),
            ("AllocationBase",    ctypes.c_void_p),
            ("AllocationProtect", ctypes.wintypes.DWORD),
            ("RegionSize",        ctypes.c_size_t),
            ("State",             ctypes.wintypes.DWORD),
            ("Protect",           ctypes.wintypes.DWORD),
            ("Type",              ctypes.wintypes.DWORD),
        ]

    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not handle:
        raise PermissionError(f"OpenProcess fehlgeschlagen (PID {pid}): {ctypes.GetLastError()}")

    chunks = []
    addr = 0
    mbi = MEMORY_BASIC_INFORMATION()

    try:
        while k32.VirtualQueryEx(handle, ctypes.c_void_p(addr),
                                 ctypes.byref(mbi), ctypes.sizeof(mbi)):
            skip = (
                mbi.State != MEM_COMMIT or
                mbi.Protect & PAGE_NOACCESS or
                mbi.Protect & PAGE_GUARD or
                mbi.RegionSize > 256 * 1024 * 1024  # >256 MB überspringen
            )
            if not skip:
                buf = ctypes.create_string_buffer(mbi.RegionSize)
                read = ctypes.c_size_t(0)
                if k32.ReadProcessMemory(handle, ctypes.c_void_p(addr),
                                         buf, mbi.RegionSize, ctypes.byref(read)):
                    chunks.append(bytes(buf[:read.value]))
            addr += mbi.RegionSize
            if addr >= 0x7FFFFFFFFFFF:  # Ende des User-Space (64-bit)
                break
    finally:
        k32.CloseHandle(handle)

    return chunks


# ---------------------------------------------------------------------------
# Linux – Speicher lesen via /proc/{pid}/mem
# ---------------------------------------------------------------------------

def _linux_find_pid(name: str) -> "int | None":
    """Findet PID anhand des Prozessnamens in /proc."""
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            cmdline = open(f"/proc/{entry}/cmdline", "rb").read()
            if name.lower().encode() in cmdline.lower():
                return int(entry)
        except (PermissionError, FileNotFoundError):
            continue
    return None


def _linux_read_memory(pid: int) -> "list[bytes]":
    """Liest lesbare Speichersegmente aus /proc/{pid}/mem."""
    chunks = []
    maps_path = f"/proc/{pid}/maps"
    mem_path  = f"/proc/{pid}/mem"

    try:
        maps = open(maps_path).readlines()
        mem  = open(mem_path, "rb")
    except PermissionError:
        raise PermissionError(
            f"Kein Zugriff auf /proc/{pid}/mem — "
            "Script als gleicher Benutzer wie der Slicer starten."
        )

    for line in maps:
        parts = line.split()
        if len(parts) < 2:
            continue
        perms = parts[1]
        if "r" not in perms:          # nur lesbare Seiten
            continue
        if "x" in perms:              # Code-Seiten überspringen (keine Strings)
            continue
        try:
            start, end = [int(x, 16) for x in parts[0].split("-")]
        except ValueError:
            continue
        size = end - start
        if size > 256 * 1024 * 1024:  # >256 MB überspringen
            continue
        try:
            mem.seek(start)
            data = mem.read(size)
            if data:
                chunks.append(data)
        except (OSError, ValueError):
            continue

    mem.close()
    return chunks


# ---------------------------------------------------------------------------
# Pattern-Suche
# ---------------------------------------------------------------------------

def _is_valid_ip(ip_bytes: bytes) -> bool:
    parts = ip_bytes.split(b".")
    if len(parts) != 4:
        return False
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return False
    if nums[0] in (0, 127, 255):
        return False
    if nums == [0, 0, 0, 0]:
        return False
    return all(0 <= n <= 255 for n in nums)


def search_chunks(chunks: "list[bytes]") -> dict:
    """Durchsucht Speicher-Chunks nach Credential-Patterns."""
    usernames  = {}   # value → count
    passwords  = {}
    ips        = {}
    device_ids = {}

    total = len(chunks)
    for i, chunk in enumerate(chunks):
        if i % 50 == 0 or i == total - 1:
            pct = (i + 1) * 100 // total
            mb_done = sum(len(c) for c in chunks[:i+1]) / 1024 / 1024
            print(f"\r[*] Analysiere ... {pct:3d}%  ({mb_done:.0f} MB)", end="", flush=True)
        for m in RE_USERNAME.finditer(chunk):
            v = m.group().decode("ascii", errors="replace")
            usernames[v] = usernames.get(v, 0) + 1

        # Proximity: Passwort das innerhalb von 512 Bytes nach einem Username steht
        for m in RE_USER_PASS_PROXIMITY.finditer(chunk):
            pw = m.group(2).decode("ascii", errors="replace")
            has_upper = any(c.isupper() for c in pw)
            has_lower = any(c.islower() for c in pw)
            has_digit = any(c.isdigit() for c in pw)
            if has_upper and has_lower and has_digit:
                passwords[pw] = passwords.get(pw, 0) + 500

        for m in RE_PASSWORD.finditer(chunk):
            v = m.group().decode("ascii", errors="replace")
            # Filter: mindestens 2 Großbuchstaben + 2 Kleinbuchstaben + 1 Ziffer
            has_upper = sum(1 for c in v if c.isupper()) >= 2
            has_lower = sum(1 for c in v if c.islower()) >= 2
            has_digit = sum(1 for c in v if c.isdigit()) >= 1
            if has_upper and has_lower and has_digit:
                passwords[v] = passwords.get(v, 0) + 1

        for m in RE_IP.finditer(chunk):
            ip = m.group(1)
            if _is_valid_ip(ip):
                v = ip.decode("ascii")
                ips[v] = ips.get(v, 0) + 1

        for m in RE_DEVICE_ID.finditer(chunk):
            v = m.group().decode("ascii")
            device_ids[v] = device_ids.get(v, 0) + 1

    print()  # Zeilenumbruch nach Fortschrittszeile

    # Nach Häufigkeit sortieren, häufigste zuerst
    def top(d, n=10):
        return sorted(d.items(), key=lambda x: -x[1])[:n]

    return {
        "usernames":  top(usernames),
        "passwords":  top(passwords),
        "ips":        top(ips, 10),
        "device_ids": top(device_ids),
    }


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

SLICER_NAMES = [
    "AnycubicSlicerNext.exe",  # Windows
    "AnycubicSlicer.exe",
    "AnycubicSlicerNext",      # Linux
    "AnycubicSlicer",
]


def find_slicer_pid() -> "tuple[int, str] | tuple[None, None]":
    for name in SLICER_NAMES:
        if IS_WINDOWS:
            pid = _win_find_pid(name)
        else:
            pid = _linux_find_pid(name)
        if pid:
            return pid, name
    return None, None


def read_process(pid: int) -> "list[bytes]":
    if IS_WINDOWS:
        return _win_read_memory(pid)
    else:
        return _linux_read_memory(pid)


def write_env(results: dict, env_path: str,
              username: str, password: str, ip: str,
              mode_id: str = "20030", device_id: str = ""):
    lines = []
    if os.path.isfile(env_path):
        with open(env_path) as f:
            lines = f.readlines()

    def set_val(key, val):
        nonlocal lines
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={val}\n"
                return
        lines.append(f"{key}={val}\n")

    set_val("PRINTER_IP",    ip)
    set_val("MQTT_USERNAME", username)
    set_val("MQTT_PASSWORD", password)
    if device_id:
        set_val("DEVICE_ID", device_id)
    if mode_id:
        set_val("MODE_ID", mode_id)

    with open(env_path, "w") as f:
        f.writelines(lines)
    print(f"\n✓ Credentials in '{env_path}' gespeichert.")


def main():
    parser = argparse.ArgumentParser(
        description="Extrahiert MQTT-Credentials aus dem RAM des AnycubicSlicer-Prozesses"
    )
    parser.add_argument("--write-env",  action="store_true",
                        help="Gefundene Credentials in .env schreiben")
    parser.add_argument("--env-file",   default=None,
                        help="Pfad zur .env-Datei (Standard: ../. env relativ zu diesem Script)")
    parser.add_argument("--pid",        type=int, default=None,
                        help="Prozess-PID direkt angeben (überspringt Auto-Erkennung)")
    parser.add_argument("--verbose",    action="store_true",
                        help="Alle Kandidaten ausgeben, nicht nur den besten")
    args = parser.parse_args()

    # .env-Pfad bestimmen
    if args.env_file:
        env_path = args.env_file
    else:
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        env_path = os.path.normpath(env_path)

    # Prozess finden
    if args.pid:
        pid, proc_name = args.pid, f"PID {args.pid}"
    else:
        print("[*] Suche AnycubicSlicer-Prozess ...")
        pid, proc_name = find_slicer_pid()
        if not pid:
            print("✗ AnycubicSlicer nicht gefunden. Bitte den Slicer starten und "
                  "mit dem Drucker verbinden, dann erneut ausführen.")
            sys.exit(1)

    print(f"[*] Prozess gefunden: {proc_name} (PID {pid})")
    print(f"[*] Lese Prozess-Speicher ...")

    try:
        chunks = read_process(pid)
    except PermissionError as e:
        print(f"✗ Zugriffsfehler: {e}")
        sys.exit(1)

    total_mb = sum(len(c) for c in chunks) / 1024 / 1024
    print(f"[*] {len(chunks)} Speichersegmente gelesen ({total_mb:.1f} MB)")
    print(f"[*] Durchsuche nach Credentials ...")

    results = search_chunks(chunks)

    # Ausgabe
    print("\n" + "="*55)
    print("  ERGEBNISSE")
    print("="*55)

    def show(label, items, verbose):
        if not items:
            print(f"  {label:12s}  —  nicht gefunden")
            return items[0][0] if items else ""
        best = items[0][0]
        print(f"  {label:12s}  {best}  (Treffer: {items[0][1]})")
        if verbose and len(items) > 1:
            for val, cnt in items[1:]:
                print(f"  {'':12s}  {val}  (Treffer: {cnt})")
        return best

    best_user   = show("Username",  results["usernames"],  args.verbose)
    best_pass   = show("Password",  results["passwords"],  args.verbose)
    best_device = show("Device-ID", results["device_ids"], args.verbose)

    # IP: 192.168.x.x bevorzugen
    lan_ips = [(ip, cnt) for ip, cnt in results["ips"]
               if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172.")]
    if not lan_ips:
        lan_ips = results["ips"]
    best_ip = show("Drucker-IP",  lan_ips, args.verbose)

    print("="*55)

    if not best_user or not best_pass:
        print("\n⚠  Keine vollständigen Credentials gefunden.")
        print("   Stelle sicher dass der Slicer MIT dem Drucker verbunden ist.")
        sys.exit(1)

    if args.write_env:
        write_env(results, env_path, best_user, best_pass, best_ip,
                  device_id=best_device)
    else:
        print(f"\nHinweis: --write-env übergeben um Credentials in '{env_path}' zu speichern.")


if __name__ == "__main__":
    main()
