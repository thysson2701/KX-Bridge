#!/bin/bash
# Bridge-Manager: start | stop | restart | status | log
SCRIPT="$(dirname "$0")/kobrax_moonraker_bridge.py"
LOGFILE="/tmp/bridge.log"
PRINTER_IP="192.168.178.94"

case "${1:-restart}" in
  start)
    if ss -tlnp | grep -q 7125; then
      echo "Port 7125 belegt – beende alte Instanz..."
      fuser -k 7125/tcp 2>/dev/null || pkill -f kobrax_moonraker_bridge 2>/dev/null
      sleep 1
    fi
    nohup python3 "$SCRIPT" --printer-ip "$PRINTER_IP" > "$LOGFILE" 2>&1 &
    echo "Bridge gestartet PID=$!"
    sleep 2; tail -3 "$LOGFILE"
    ;;
  stop)
    pkill -f kobrax_moonraker_bridge && echo "Bridge beendet" || echo "Nicht aktiv"
    fuser -k 7125/tcp 2>/dev/null
    ;;
  restart)
    "$0" stop
    sleep 1
    "$0" start
    ;;
  status)
    if ss -tlnp | grep -q 7125; then
      echo "Bridge läuft (Port 7125 aktiv)"
      ps aux | grep kobrax_moonraker | grep -v grep
    else
      echo "Bridge nicht aktiv"
    fi
    ;;
  log)
    tail -${2:-20} "$LOGFILE"
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|log [N]}"
    exit 1
    ;;
esac
