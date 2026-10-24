"""
GPS receiver thread and NMEA parsing.
Owns all GPS state — nothing in state.py.

OM Position has exactly ONE source, chosen by hand in Settings (S471): the hand-picked map position (default,
nothing is read), a built-in UART GPS, a picked USB GPS dongle, or OPS-TOC's GPS (polled over HTTP, so both apps
can share one receiver). No fallback between them: a source without a fix means OM has no live position.
"""
import json
import logging
import os
import threading
import time
import urllib.request

from config import CONFIG
from helpers import push_to_sse
from state import connections, connections_lock, mc_connections, mc_connections_lock

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
_gps_last_push_ts       = 0.0      # epoch seconds — rate-limits auto-push to MT radios
_gps_last_push_mc_ts    = 0.0      # epoch seconds — rate-limits auto-push to MC radios (own clock, S471)
_GPS_AUTO_PUSH_INTERVAL = 30       # default seconds between auto-pushes (overridden by config)
_PROXY_FAIL_LIMIT = 5
_PROXY_RETRY_SECONDS = 2      # pause between proxy attempts
_PROXY_WARN_EVERY = 300       # a sustained outage logs at most once per 5 minutes

_gps_runtime = {
    "port": "",
    "running": False,
    "port_present": False,
    "error": "",
    "source": "",
}

_gps_silent_suppressed = False   # one-shot log guard for Silent Running (see _gps_tx_blocked)


def _gps_tx_blocked(context="GPS push"):
    """True when Silent Running must block a GPS transmission.

    Logs once per transition (not on every auto-push tick), so the journal shows
    why positions stopped being pushed without flooding while silent. Mirrors the
    app-layer gating the rest of the send paths use.
    """
    global _gps_silent_suppressed
    silent = bool(CONFIG.get("silent_mode"))
    if silent and not _gps_silent_suppressed:
        _gps_silent_suppressed = True
        log.info("[gps] Silent Running active — %s suppressed", context)
    elif not silent and _gps_silent_suppressed:
        _gps_silent_suppressed = False
        log.info("[gps] Silent Running cleared — %s resumed", context)
    return silent


def _is_internal_uart(port: str) -> bool:
    return os.path.basename(str(port or "")).startswith("ttyAS")


def _baud_for_port(port: str) -> int:
    try:
        return int(CONFIG.get("gps", {}).get("baud") or (115200 if _is_internal_uart(port) else 9600))
    except (TypeError, ValueError):
        return 115200 if _is_internal_uart(port) else 9600


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
            sats_view = gps_state.get("sats_view", 0)
        push_to_sse(json.dumps({
            "type": "gps_position",
            "lat":  lat if fix_q > 0 else None,
            "lon":  lon if fix_q > 0 else None,
            "alt":  int(alt) if (fix_q > 0 and alt is not None) else None,
            "sats": sats,
            "sats_view": sats_view,   # satellites HEARD (GSV): 0 here while outdoors = antenna/interference (S471)
            "fix":  fix_q > 0,
        }))
        if fix_q > 0 and lat is not None and lon is not None:
            _gps_auto_push(lat, lon, alt)
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
    if _is_internal_uart(port):
        return os.path.exists(port)
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
            # A radio whose board is not plugged in holds no port: its saved /dev/ttyACMx name may belong to another
            # device now. Radios are recognised by serial in gps_device_check() instead (S471).
            return str(port_by_serial.get(usb_serial) or "").strip()
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


# ---------------------------------------------------------------------------
# Which device is the GPS? (S471)
#
# A /dev/ttyACMx name is not an identity: it follows plug and power order. On CD (2026-10-24) the T114 was off, the
# FakeTec running Meshtastic came up on /dev/ttyACM0 (the saved GPS fallback), and OM opened it and wrote UBX setup
# bytes to the radio. So a USB serial device is only opened as a GPS when we know WHAT it is:
#   * never a board whose USB serial belongs to any saved radio (enabled or not);
#   * the device the user picked in Settings (usb_id captured at save time), or
#   * with no saved usb_id (older configs): a device whose USB strings say GPS/GNSS (or u-blox's vendor id).
# Internal UARTs (ttyS*, ttyAS*) are fixed hardware wiring, so their path IS their identity.
# ---------------------------------------------------------------------------

_GPS_KEYWORDS = (
    "gps", "gnss", "u-blox", "ublox", "navigation",
    "globalsat", "garmin", "navilock", "holux", "beitian",
    "bu-353", "skytraq", "sirf",
)
_UBLOX_VID = 0x1546


def _is_hw_uart(port: str) -> bool:
    return os.path.basename(str(port or "")).startswith(("ttyS", "ttyAS"))


def _usb_port_info(port):
    """The comports() entry behind a port (by-id symlinks resolved), or None."""
    real = os.path.realpath(port) if str(port).startswith("/dev/serial/by-id/") else port
    try:
        import serial.tools.list_ports
        for p in serial.tools.list_ports.comports():
            if p.device == real:
                return p
    except Exception:
        pass
    return None


def _radio_serials():
    """USB serial -> radio name, over EVERY saved radio: a disabled radio's board is still a radio."""
    out = {}
    for key, net in (("nodes", "MT"), ("mc_nodes", "MC")):
        for node in CONFIG.get(key, []):
            s = str(node.get("usb_serial") or "").strip()
            if s:
                out[s] = f"{net} radio {node.get('name') or node.get('id') or ''}".strip()
    return out


def gps_port_radio(port):
    """Name of the saved radio whose board is behind `port` right now (matched by USB serial), or None."""
    if _is_hw_uart(port):
        return None
    info = _usb_port_info(port)
    serial_no = str(getattr(info, "serial_number", "") or "").strip() if info else ""
    return _radio_serials().get(serial_no) if serial_no else None


def _looks_like_gps(info) -> bool:
    if info is None:
        return False
    if getattr(info, "vid", None) == _UBLOX_VID:
        return True
    text = " ".join(str(getattr(info, k, "") or "") for k in ("product", "manufacturer", "description")).lower()
    return any(k in text for k in _GPS_KEYWORDS)


def gps_usb_id(port):
    """Identity to save with a picked USB GPS port: {serial, vid, pid}, or None (internal UART / not present)."""
    if _is_hw_uart(port):
        return None
    info = _usb_port_info(port)
    if info is None:
        return None
    return {"serial": str(info.serial_number or ""), "vid": info.vid, "pid": info.pid}


def gps_device_check(port, usb_id=None):
    """(ok, reason). ok=True only when the device behind `port` may be opened as the GPS."""
    port = str(port or "").strip()
    if not port:
        return False, "no GPS port selected"
    if _is_hw_uart(port):
        return (True, "") if os.path.exists(port) else (False, f"{port} does not exist")
    info = _usb_port_info(port)
    if info is None:
        return False, f"{port} is not connected"
    serial_no = str(info.serial_number or "").strip()
    radio = gps_port_radio(port)
    if radio:
        return False, f"{port} is the {radio}"
    what = info.product or info.description or "an unknown device"
    if usb_id:
        same = (serial_no == str(usb_id.get("serial") or "") if usb_id.get("serial")
                else (info.vid, info.pid) == (usb_id.get("vid"), usb_id.get("pid")))
        return (True, "") if same else (False, f"{port} is now {what}, not the GPS picked in Settings")
    if _looks_like_gps(info):
        return True, ""
    return False, f"{port} is {what}, which does not identify as a GPS (pick the GPS again in Settings to trust it)"


# ---------------------------------------------------------------------------
# OM Position source (S471, Filip: "no automation. Manual select, defaulting to a hand-picked position").
#
# Exactly ONE source, changed only by the user in Settings -> OM Position:
#   manual   - the hand-picked map position (default). No GPS is read at all.
#   internal - a built-in UART GPS (CD: BN-280 on /dev/ttyS3; HD: /dev/ttyAS*).
#   usb      - a USB GPS dongle the user picked (remembered by usb_id).
#   proxy    - OPS-TOC's GPS. No fix there = no fix in OM.
# No fallback, no auto-detect, no switching between them.
# ---------------------------------------------------------------------------

GPS_SOURCES = ("manual", "internal", "usb", "proxy")


def gps_source(cfg=None):
    """The chosen source. Configs from before S471 carried `enabled` + direct/proxy and are read as:
    disabled -> manual, proxy -> proxy, direct on a built-in UART -> internal, other direct -> usb."""
    cfg = (CONFIG.get("gps") or {}) if cfg is None else cfg
    src = str(cfg.get("source") or "").strip()
    if "enabled" in cfg:
        if not cfg.get("enabled"):
            return "manual"
        if src == "proxy":
            return "proxy"
        return "internal" if _is_hw_uart(cfg.get("port")) else "usb"
    return src if src in GPS_SOURCES else "manual"


def gps_source_port(cfg=None, source=None):
    """The port of the chosen serial source ('' for manual/proxy). Internal and USB keep their own port, so
    switching between sources never loses the other one."""
    cfg = (CONFIG.get("gps") or {}) if cfg is None else cfg
    source = source or gps_source(cfg)
    legacy = str(cfg.get("port") or "").strip()
    if source == "internal":
        return str(cfg.get("internal_port") or (legacy if _is_hw_uart(legacy) else "")).strip()
    if source == "usb":
        return str(cfg.get("usb_port") or (legacy if legacy and not _is_hw_uart(legacy) else "")).strip()
    return ""


def _gps_push_to_nodes(lat, lon, alt, precision_bits):
    """Push GPS position to all connected nodes. Called from background thread."""
    if _gps_tx_blocked("GPS push"):
        return
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



def _blur_coord(value, precision_bits):
    """Meshtastic-style position precision: keep the top `precision_bits` of the 1e-7 integer and centre it in the
    cell, so MC gets the same blur the firmware applies for MT (S471)."""
    if not precision_bits or precision_bits >= 32:
        return value
    v = int(round(value * 1e7)) & 0xFFFFFFFF
    v = (v & ((0xFFFFFFFF << (32 - precision_bits)) & 0xFFFFFFFF)) + (1 << (31 - precision_bits))
    if v >= 1 << 31:
        v -= 1 << 32
    return v / 1e7


def _gps_push_to_mc(lat, lon, precision_bits):
    """Set the position on every connected MeshCore companion (local USB command; the companion includes it in
    its own adverts only if its advert-location policy is on). Same Silent Running gate as the MT push."""
    if _gps_tx_blocked("GPS push (MC)"):
        return []
    from mesh_mc import set_device_coords   # lazy: mesh_mc is heavy and imports a lot at load
    lat, lon = _blur_coord(lat, precision_bits), _blur_coord(lon, precision_bits)
    with mc_connections_lock:
        radio_ids = [rid for rid, st in mc_connections.items() if st.get("status") == "connected"]
    done = []
    for radio_id in radio_ids:
        try:
            set_device_coords(radio_id, lat, lon)
            done.append(radio_id)
            log.info(f"GPS push → MC {radio_id} ({lat:.5f}, {lon:.5f}) precision_bits={precision_bits}")
        except Exception as e:
            log.warning(f"GPS push to MC {radio_id} failed: {e}")
    return done


def _gps_auto_push(lat, lon, alt):
    """Auto-push a live fix, separately per network (Filip, S471: two toggles). Both default OFF. `auto_push`
    (before S471) only ever pushed to MT, so it is read as the MT toggle."""
    global _gps_last_push_ts, _gps_last_push_mc_ts
    cfg = CONFIG.get("gps", {}) or {}
    push_mt = cfg.get("auto_push_mt", cfg.get("auto_push", False))
    push_mc = cfg.get("auto_push_mc", False)
    if not (push_mt or push_mc) or _gps_tx_blocked("GPS auto-push"):
        return
    now = time.time()
    interval = max(10, int(cfg.get("push_interval", _GPS_AUTO_PUSH_INTERVAL)))
    precision_bits = cfg.get("precision", 32)
    if push_mt and now - _gps_last_push_ts >= interval:
        _gps_last_push_ts = now
        threading.Thread(target=_gps_push_to_nodes,
                         args=(lat, lon, int(alt) if alt is not None else 0, precision_bits), daemon=True).start()
    if push_mc and now - _gps_last_push_mc_ts >= interval:
        _gps_last_push_mc_ts = now
        threading.Thread(target=_gps_push_to_mc, args=(lat, lon, precision_bits), daemon=True).start()

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
    baud = _baud_for_port(port)
    log.info(f"GPS: opening {port} at {baud}")
    with gps_lock:
        _gps_runtime.update({"port": port, "running": False, "port_present": gps_port_present(port),
                              "error": "", "baud": baud})
    try:
        # Open without toggling DTR so u-blox doesn't cold-reset on OM restart
        ser = _serial.Serial()
        ser.port     = port
        ser.baudrate = baud
        ser.timeout  = 1
        ser.dtr      = False
        ser.open()
    except Exception as e:
        log.error(f"GPS: cannot open {port}: {e}")
        with gps_lock:
            _gps_runtime.update({"port": port, "running": False, "port_present": gps_port_present(port), "error": str(e)})
        push_to_sse(json.dumps({"type": "gps_error", "message": str(e)}))
        return

    # Restore NMEA sentences on USB u-blox-style dongles. GPIO UART GNSS modules
    # such as the HD BE-222Q already stream NMEA and may run at a different baud.
    if not _is_internal_uart(port):
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

def _gps_proxy_reader(proxy_url, stop_event):
    url = proxy_url.rstrip("/") + "/api/gps"
    log.info(f"GPS: proxy mode → {url}")
    with gps_lock:
        _gps_runtime.update({"port": f"OPS-TOC ({proxy_url})", "running": True,
                              "port_present": True, "error": "", "source": "proxy"})
    fails = 0
    last_warn = 0.0
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
                "sats_view": sats_view,
                "fix":  fix,
            }))
            if fix and lat is not None and lon is not None:
                _gps_auto_push(lat, lon, alt)
            if fails:
                log.info(f"GPS: OPS-TOC reachable again after {fails} failed attempt(s)")
            fails = 0
        except Exception as e:
            fails += 1
            with gps_lock:
                _gps_runtime["error"] = f"OPS-TOC unreachable ({fails}x): {e}"
            if fails >= _PROXY_FAIL_LIMIT:
                # No fallback (S471): OPS-TOC is the chosen source, so no answer means no position.
                now = time.monotonic()
                if now - last_warn >= _PROXY_WARN_EVERY:
                    log.warning(f"GPS: OPS-TOC unreachable ({fails} tries) - no position until it answers, retrying every {_PROXY_RETRY_SECONDS}s")
                    last_warn = now
        time.sleep(_PROXY_RETRY_SECONDS)
    with gps_lock:
        _gps_runtime.update({"running": False})
    log.info("GPS: proxy reader stopped")


def _gps_start_proxy(proxy_url):
    global _gps_thread, _gps_stop_event
    proxy_url = str(proxy_url or "").strip()
    if not proxy_url:
        return
    _gps_stop_event.set()
    time.sleep(0.2)
    _gps_stop_event = threading.Event()
    t = threading.Thread(target=_gps_proxy_reader,
                         args=(proxy_url, _gps_stop_event), daemon=True)
    t.start()
    _gps_thread = t


def gps_apply_config(restart=False):
    """Make the reader match the ONE chosen source. Returns a user-facing warning, or None.

    restart=False (watchdog): only (re)start a reader that is not running, e.g. a dongle plugged back in.
    restart=True (Settings save, startup): start the chosen source fresh."""
    cfg = CONFIG.get("gps", {}) or {}
    source = gps_source(cfg)
    alive = _gps_thread is not None and _gps_thread.is_alive()
    if source == "manual":
        if alive or _gps_runtime.get("source"):
            _gps_stop()
        return None
    if alive and not restart:
        return None
    if source == "proxy":
        _gps_start_proxy(str(cfg.get("proxy_url") or "http://localhost:8090").strip())
        return None
    port = gps_source_port(cfg, source)
    why = ""
    if not port:
        why = "no port selected"
    elif (source == "internal") != _is_hw_uart(port):
        why = f"{port} is not {'a built-in UART' if source == 'internal' else 'a USB device'}"
    else:
        ok, why = gps_device_check(port, cfg.get("usb_id") if source == "usb" else None)
        conflict = gps_port_conflict(port) if ok else None
        if conflict:
            why = f"{port} is the {conflict['network']} radio {conflict['name']}"
        elif ok:
            why = ""
    if why:
        if alive:
            _gps_stop()
        with gps_lock:
            _gps_runtime.update({"port": port, "running": False, "port_present": gps_port_present(port),
                                 "error": f"Not reading {port or 'a GPS'}: {why}.", "source": source})
        return f"Not reading {port or 'a GPS'}: {why}."
    _gps_start(port)
    with gps_lock:
        _gps_runtime["source"] = source
    return None


def gps_watchdog_loop():
    while True:
        time.sleep(5)
        try:
            gps_apply_config(restart=False)
        except Exception as e:
            log.warning(f"GPS watchdog: {e}")
