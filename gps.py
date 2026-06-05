"""
GPS receiver thread and NMEA parsing.
Owns all GPS state — nothing in state.py.

Two modes:
  - direct serial: OM reads the NMEA dongle itself
  - OPS-TOC proxy: polls OPS-TOC /api/gps — avoids serial conflicts when both
                   apps run on the same machine. Falls back to direct serial if
                   OPS-TOC becomes unreachable and a fallback port is configured.
"""
import json
import logging
import threading
import time
import urllib.request

from config import CONFIG
from helpers import push_to_sse
from state import connections, connections_lock

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GPS state (owned here — not in state.py)
# ---------------------------------------------------------------------------

gps_state = {
    "lat": None, "lon": None, "alt": None,
    "sats": 0, "sats_view": 0, "fix": False, "speed": None,
}
gps_lock                = threading.Lock()
_gps_stop_event         = threading.Event()
_gps_thread             = None
_gps_last_push_ts       = 0.0      # epoch seconds — rate-limits auto-push to nodes
_GPS_AUTO_PUSH_INTERVAL = 30       # default seconds between auto-pushes (overridden by config)
_PROXY_FAIL_LIMIT = 5

_gps_runtime = {
    "port": "",
    "running": False,
    "port_present": False,
    "error": "",
    "source": "",
}


# ---------------------------------------------------------------------------
# NMEA helpers
# ---------------------------------------------------------------------------

def _parse_nmea_coord(val, hemi):
    """Convert NMEA DDMM.MMMM to decimal degrees."""
    if not val:
        return None
    try:
        raw = float(val)
        d   = int(raw / 100)
        m   = raw - d * 100
        dec = d + m / 60.0
        return -dec if hemi in ("S", "W") else dec
    except (ValueError, TypeError):
        return None


def _parse_gpgga(line):
    """Parse a $GPGGA/$GNGGA sentence and update gps_state + push SSE."""
    global _gps_last_push_ts
    try:
        if "*" in line:
            line = line[:line.index("*")]
        parts = line.split(",")
        if len(parts) < 10:
            return
        fix_q = int(parts[6]) if parts[6] else 0
        lat   = _parse_nmea_coord(parts[2], parts[3])
        lon   = _parse_nmea_coord(parts[4], parts[5])
        sats  = int(parts[7]) if parts[7] else 0
        alt   = float(parts[9]) if parts[9] else None
        with gps_lock:
            gps_state["sats"] = sats
            gps_state["fix"]  = fix_q > 0
            if fix_q > 0 and lat is not None and lon is not None:
                gps_state["lat"] = lat
                gps_state["lon"] = lon
                gps_state["alt"] = int(alt) if alt is not None else None
        push_to_sse(json.dumps({
            "type": "gps_position",
            "lat":  lat if fix_q > 0 else None,
            "lon":  lon if fix_q > 0 else None,
            "alt":  int(alt) if (fix_q > 0 and alt is not None) else None,
            "sats": sats,
            "fix":  fix_q > 0,
        }))
        # Auto-push to connected nodes if enabled and rate-limit allows
        if fix_q > 0 and lat is not None and lon is not None:
            gps_cfg = CONFIG.get("gps", {})
            if gps_cfg.get("auto_push"):
                now = time.time()
                interval = max(10, int(gps_cfg.get("push_interval", _GPS_AUTO_PUSH_INTERVAL)))
                if now - _gps_last_push_ts >= interval:
                    _gps_last_push_ts = now
                    precision_bits = gps_cfg.get("precision", 32)
                    threading.Thread(
                        target=_gps_push_to_nodes,
                        args=(lat, lon, int(alt) if alt is not None else 0, precision_bits),
                        daemon=True
                    ).start()
    except Exception as e:
        log.debug(f"GPS parse error: {e}")


def _parse_gpgsv(line):
    """Parse $GPGSV/$GNGSV — update sats_view (satellites in view, even without fix)."""
    try:
        if "*" in line:
            line = line[:line.index("*")]
        parts = line.split(",")
        # Field 3 = total satellites in view
        if len(parts) >= 4 and parts[3]:
            sats_view = int(parts[3])
            with gps_lock:
                gps_state["sats_view"] = sats_view
    except Exception as e:
        log.debug(f"GPS GSV parse error: {e}")


def gps_port_present(port):
    port = str(port or "").strip()
    if not port:
        return False
    try:
        import serial.tools.list_ports
        return any(p.device == port for p in serial.tools.list_ports.comports())
    except Exception:
        return False


def gps_port_conflict(port):
    """Return conflict info if the GPS port overlaps an enabled MT or MC radio."""
    port = str(port or "").strip()
    if not port:
        return None
    port_by_serial = {}
    try:
        import serial.tools.list_ports
        port_by_serial = {str(p.serial_number or ""): p.device for p in serial.tools.list_ports.comports()}
    except Exception:
        port_by_serial = {}

    def current_port(node):
        usb_serial = str(node.get("usb_serial") or "").strip()
        if usb_serial:
            return str(port_by_serial.get(usb_serial) or node.get("port") or "").strip()
        return str(node.get("port") or "").strip()

    for node in CONFIG.get("nodes", []):
        if not node.get("enabled", True):
            continue
        if current_port(node) == port:
            return {"network": "MT", "name": node.get("name") or node.get("id") or "MT radio"}

    for node in CONFIG.get("mc_nodes", []):
        if not node.get("enabled", True):
            continue
        if current_port(node) == port:
            return {"network": "MC", "name": node.get("name") or node.get("id") or "MC radio"}

    return None


def _gps_push_to_nodes(lat, lon, alt, precision_bits):
    """Push GPS position to all connected nodes. Called from background thread."""
    from meshtastic.protobuf import mesh_pb2, admin_pb2
    with connections_lock:
        radio_ids = list(connections.keys())
    for radio_id in radio_ids:
        with connections_lock:
            state = connections.get(radio_id, {})
            iface = state.get("iface") if state.get("status") == "connected" else None
        if not iface:
            continue
        # Update frontend cache unconditionally — iface.nodes won't reflect the new
        # position until the node broadcasts back a POSITION_APP, which may take minutes.
        # The fixed_lat/lon cache in helpers.py ensures /api/nodes returns the correct
        # position immediately, regardless of whether _sendAdmin succeeds.
        local_num = getattr(iface.myInfo, "my_node_num", None)
        with connections_lock:
            if local_num is not None and local_num in iface.nodesByNum:
                iface.nodesByNum[local_num]["position"] = {
                    "latitude": lat, "longitude": lon, "altitude": int(alt),
                    "latitudeI": int(lat * 1e7), "longitudeI": int(lon * 1e7),
                    "fixedPosition": True,
                }
            connections[radio_id]["fixed_lat"] = lat
            connections[radio_id]["fixed_lon"] = lon
        # Tell the frontend immediately — don't wait for loadLive() poll
        push_to_sse(json.dumps({
            "type": "local_node_position",
            "radio_id": radio_id,
            "lat": lat,
            "lon": lon,
        }))
        try:
            p = mesh_pb2.Position()
            p.latitude_i  = int(lat * 1e7)
            p.longitude_i = int(lon * 1e7)
            if alt:
                p.altitude = int(alt)
            if precision_bits and precision_bits < 32:
                p.precision_bits = precision_bits
            a = admin_pb2.AdminMessage()
            a.set_fixed_position.CopyFrom(p)
            iface.localNode.ensureSessionKey()
            iface.localNode._sendAdmin(a)
            log.info(f"GPS push → {radio_id} ({lat:.5f}, {lon:.5f}) precision_bits={precision_bits}")
        except Exception as e:
            log.warning(f"GPS push to {radio_id} firmware failed: {e} (frontend cache still updated)")


# ---------------------------------------------------------------------------
# Reader thread
# ---------------------------------------------------------------------------

def _ubx_msg(cls, id_, payload=b''):
    """Build a UBX binary message with checksum."""
    msg = bytes([0xB5, 0x62, cls, id_, len(payload) & 0xFF, (len(payload) >> 8) & 0xFF]) + payload
    ck_a, ck_b = 0, 0
    for b in msg[2:]:
        ck_a = (ck_a + b) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return msg + bytes([ck_a, ck_b])


def _gps_init(ser):
    """Ensure NMEA output is active. Recovers from UBX-only mode left by gpsd."""
    # CFG-MSG payload: [msgClass, msgID, rate_DDC, rate_UART1, rate_UART2, rate_USB, rate_SPI, reserved]
    # Enable GGA, RMC, GSV on USB (index 5) — VK-162 and similar dongles connect via USB CDC
    for nmea_id in (0x00, 0x04, 0x03):
        ser.write(_ubx_msg(0x06, 0x01, bytes([0xF0, nmea_id, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00])))
        time.sleep(0.05)
    # Disable UBX NAV-PVT spam gpsd turns on — all ports
    ser.write(_ubx_msg(0x06, 0x01, bytes([0x01, 0x07, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])))
    time.sleep(0.05)
    ser.reset_input_buffer()


def _gps_reader(port, stop_event):
    import serial as _serial
    log.info(f"GPS: opening {port}")
    with gps_lock:
        _gps_runtime.update({"port": port, "running": False, "port_present": gps_port_present(port),
                              "error": "", "source": "direct"})
    try:
        # Open without toggling DTR so u-blox doesn't cold-reset on OM restart
        ser = _serial.Serial()
        ser.port     = port
        ser.baudrate = 9600
        ser.timeout  = 1
        ser.dtr      = False
        ser.open()
    except Exception as e:
        log.error(f"GPS: cannot open {port}: {e}")
        with gps_lock:
            _gps_runtime.update({"port": port, "running": False, "port_present": gps_port_present(port), "error": str(e)})
        push_to_sse(json.dumps({"type": "gps_error", "message": str(e)}))
        return

    # Restore NMEA sentences in case gpsd left the device in UBX-only mode
    try:
        _gps_init(ser)
        log.info("GPS: NMEA sentences configured")
    except Exception as e:
        log.warning(f"GPS: init commands failed (continuing anyway): {e}")

    with gps_lock:
        _gps_runtime.update({"port": port, "running": True, "port_present": True, "error": ""})
    while not stop_event.is_set():
        try:
            raw  = ser.readline()
            line = raw.decode("ascii", errors="ignore").strip()
            if line.startswith(("$GPGGA", "$GNGGA")):
                _parse_gpgga(line)
            elif line.startswith(("$GPGSV", "$GNGSV")):
                _parse_gpgsv(line)
        except Exception as e:
            log.warning(f"GPS read: {e}")
            if "device disconnected" in str(e) or "device reports readiness" in str(e):
                log.warning("GPS: device disconnected, stopping reader thread")
                with gps_lock:
                    _gps_runtime.update({"port": port, "running": False, "port_present": gps_port_present(port), "error": str(e)})
                push_to_sse(json.dumps({"type": "gps_error", "message": str(e)}))
                break
            time.sleep(1)
    try:
        ser.close()
    except Exception:
        pass
    with gps_lock:
        _gps_runtime.update({"port": port, "running": False, "port_present": gps_port_present(port), "error": _gps_runtime.get("error", "")})
    log.info("GPS: thread stopped")


def _gps_start(port):
    global _gps_thread, _gps_stop_event
    port = str(port or "").strip()
    if not port:
        return
    if not gps_port_present(port):
        with gps_lock:
            _gps_runtime.update({
                "port": port,
                "running": False,
                "port_present": False,
                "error": "Selected GPS port is not currently connected.",
            })
        return
    _gps_stop_event.set()
    time.sleep(0.2)
    _gps_stop_event = threading.Event()
    t = threading.Thread(target=_gps_reader, args=(port, _gps_stop_event), daemon=True)
    t.start()
    _gps_thread = t


def _gps_stop():
    global _gps_thread
    _gps_stop_event.set()
    with gps_lock:
        gps_state.update({"lat": None, "lon": None, "alt": None, "sats": 0, "fix": False})
        _gps_runtime.update({"port": "", "running": False, "port_present": False, "error": "", "source": ""})
    _gps_thread = None


# ---------------------------------------------------------------------------
# OPS-TOC proxy reader
# ---------------------------------------------------------------------------

def _gps_proxy_reader(proxy_url, fallback_port, stop_event):
    url = proxy_url.rstrip("/") + "/api/gps"
    log.info(f"GPS: proxy mode → {url}")
    with gps_lock:
        _gps_runtime.update({"port": f"OPS-TOC ({proxy_url})", "running": True,
                              "port_present": True, "error": "", "source": "proxy"})
    fails = 0
    while not stop_event.is_set():
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                d = json.loads(resp.read())
            lat       = d.get("lat")
            lon       = d.get("lon")
            alt       = d.get("alt")
            fix       = d.get("fix", False)
            sats      = d.get("sats", 0)
            sats_view = d.get("sats_view", 0)
            with gps_lock:
                gps_state["fix"]       = fix
                gps_state["sats"]      = sats
                gps_state["sats_view"] = sats_view
                if fix and lat is not None and lon is not None:
                    gps_state["lat"] = lat
                    gps_state["lon"] = lon
                    gps_state["alt"] = alt
                _gps_runtime["error"] = ""
            push_to_sse(json.dumps({
                "type": "gps_position",
                "lat":  lat if fix else None,
                "lon":  lon if fix else None,
                "alt":  alt if fix else None,
                "sats": sats,
                "fix":  fix,
            }))
            # Auto-push to nodes (same logic as direct reader)
            if fix and lat is not None and lon is not None:
                global _gps_last_push_ts
                gps_cfg = CONFIG.get("gps", {})
                if gps_cfg.get("auto_push"):
                    now = time.time()
                    interval = max(10, int(gps_cfg.get("push_interval", _GPS_AUTO_PUSH_INTERVAL)))
                    if now - _gps_last_push_ts >= interval:
                        _gps_last_push_ts = now
                        precision_bits = gps_cfg.get("precision", 32)
                        threading.Thread(
                            target=_gps_push_to_nodes,
                            args=(lat, lon, int(alt) if alt is not None else 0, precision_bits),
                            daemon=True
                        ).start()
            fails = 0
        except Exception as e:
            fails += 1
            with gps_lock:
                _gps_runtime["error"] = f"OPS-TOC unreachable ({fails}x): {e}"
            if fails >= _PROXY_FAIL_LIMIT and fallback_port and gps_port_present(fallback_port):
                log.warning(f"GPS: OPS-TOC unreachable after {fails} tries — falling back to {fallback_port}")
                break  # watchdog will restart as direct serial reader
        time.sleep(2)
    with gps_lock:
        _gps_runtime.update({"running": False})
    log.info("GPS: proxy reader stopped")


def _gps_start_proxy(proxy_url, fallback_port=""):
    global _gps_thread, _gps_stop_event
    proxy_url = str(proxy_url or "").strip()
    if not proxy_url:
        return
    _gps_stop_event.set()
    time.sleep(0.2)
    _gps_stop_event = threading.Event()
    t = threading.Thread(target=_gps_proxy_reader,
                         args=(proxy_url, fallback_port, _gps_stop_event), daemon=True)
    t.start()
    _gps_thread = t


def gps_watchdog_loop():
    global _gps_thread
    while True:
        time.sleep(5)
        cfg = CONFIG.get("gps", {}) or {}
        enabled   = bool(cfg.get("enabled"))
        source    = str(cfg.get("source") or "direct").strip()
        port      = str(cfg.get("port") or "").strip()
        proxy_url = str(cfg.get("proxy_url") or "http://localhost:8090").strip()
        if not enabled:
            continue
        if _gps_thread and _gps_thread.is_alive():
            continue
        if source == "proxy":
            _gps_start_proxy(proxy_url, fallback_port=port)
            continue
        # direct serial path
        if not port:
            with gps_lock:
                _gps_runtime.update({"port": "", "running": False, "port_present": False, "error": "No GPS port selected."})
            continue
        conflict = gps_port_conflict(port)
        if conflict:
            with gps_lock:
                _gps_runtime.update({
                    "port": port,
                    "running": False,
                    "port_present": gps_port_present(port),
                    "error": f"GPS port {port} conflicts with enabled {conflict['network']} radio {conflict['name']}.",
                })
            continue
        if not gps_port_present(port):
            with gps_lock:
                _gps_runtime.update({
                    "port": port,
                    "running": False,
                    "port_present": False,
                    "error": "Selected GPS port is not currently connected.",
                })
            continue
        _gps_start(port)
