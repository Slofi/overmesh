"""
MeshCore interface management — async bridge for the meshcore Python library.

The meshcore library is fully async (asyncio). Flask is sync/threaded.
Bridge pattern: run a dedicated asyncio event loop in a background daemon
thread; submit coroutines to it from Flask routes via run_mc().
"""
import asyncio
import atexit
import concurrent.futures
import datetime
import logging
import threading
import time

from meshcore import MeshCore, EventType
from meshcore.packets import BinaryReqType

from config import CONFIG, CONFIG_LOCK, save_config
from cross import maybe_forward_mc_message
from db import log_position
from helpers import push_to_sse
from state import mc_connections, mc_connections_lock

log = logging.getLogger(__name__)


def _mc_bg_task(coro, label=""):
    """Schedule a background asyncio task and log any unhandled exception."""
    task = asyncio.ensure_future(coro)
    def _cb(t):
        if not t.cancelled():
            try:
                exc = t.exception()
                if exc:
                    log.warning(f"[MC] Background task '{label}' raised: {exc}")
            except Exception:
                pass
    task.add_done_callback(_cb)
    return task


def _mc_disable_debug_logging_locked(meshcore_log):
    global _mc_debug_raw_handler, _mc_debug_atexit_registered
    handler = _mc_debug_raw_handler
    if handler is None:
        return
    try:
        meshcore_log.removeHandler(handler)
    except Exception:
        pass
    if _mc_debug_atexit_registered:
        try:
            atexit.unregister(handler.close)
        except Exception:
            pass
        _mc_debug_atexit_registered = False
    try:
        handler.close()
    except Exception:
        pass
    _mc_debug_raw_handler = None
    meshcore_log.setLevel(logging.INFO)
    meshcore_log.propagate = True


def _mc_ensure_debug_logging_locked(meshcore_log):
    global _mc_debug_raw_handler, _mc_debug_atexit_registered
    meshcore_log.setLevel(logging.DEBUG)
    meshcore_log.propagate = False  # stop propagation so root INFO filter doesn't swallow DEBUG
    if _mc_debug_raw_handler is None:
        handler = logging.FileHandler("/tmp/overmesh-mc-raw.log")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        meshcore_log.addHandler(handler)
        _mc_debug_raw_handler = handler
        atexit.register(handler.close)
        _mc_debug_atexit_registered = True


def _push_mc_node(data):
    """Push an mc_node SSE event and log the GPS position if present."""
    push_to_sse(data)
    lat, lon = data.get("latitude"), data.get("longitude")
    if lat is not None and lon is not None:
        try:
            log_position(data["id"], data.get("radio_id", ""), lat, lon,
                         data.get("last_heard_ts") or int(time.time()))
        except Exception as e:
            log.debug(f"[MC] log_position failed: {e}")


# ---------------------------------------------------------------------------
# Asyncio event loop — lives in its own daemon thread
# ---------------------------------------------------------------------------

_mc_loop: asyncio.AbstractEventLoop | None = None
_mc_loop_ready = threading.Event()

# ---------------------------------------------------------------------------
# Per-radio debug event logging
# ---------------------------------------------------------------------------
_mc_debug_flags: set = set()   # radio config_ids with debug logging enabled
_mc_recent_rx_logs: dict[str, list[dict]] = {}  # radio id -> recent RX_LOG_DATA payloads
_mc_drain_active: set[str] = set()
_mc_drain_pending: set[str] = set()   # fired MESSAGES_WAITING while drain was active — re-drain on finish
_mc_path_refresh_active: set[str] = set()  # rate-limit concurrent get_contacts() from PATH_UPDATE
_mc_debug_lock = threading.Lock()
_mc_debug_until: dict[str, float] = {}
_mc_debug_raw_handler = None
_mc_debug_atexit_registered = False

MC_MAX_DM_MSG_BYTES = 160
MC_CHANNEL_NAME_OVERHEAD_BYTES = 2
MC_CHANNEL_SCOPE_HEADROOM_BYTES = 10
MC_MAX_AUTO_SPLIT_PARTS = 6
MC_SPLIT_SEND_DELAY_SEC = 1.8


def _valid_path_hash_mode(value):
    if value in ("", None):
        return None
    try:
        mode = int(value)
    except (TypeError, ValueError):
        raise ValueError("path_hash_mode must be an integer")
    if mode < 0 or mode > 2:
        raise ValueError("path_hash_mode must be between 0 and 2")
    return mode


def _path_hash_mode_attempts(preferred):
    """Try the requested mode first, then fall back from highest to lowest."""
    preferred = _valid_path_hash_mode(preferred)
    attempts = []
    if preferred is not None:
        attempts.append(preferred)
    for mode in (2, 1, 0):
        if mode not in attempts:
            attempts.append(mode)
    return attempts


def _configured_path_hash_mode(config_id):
    for node in CONFIG.get("mc_nodes", []):
        if node.get("id") == config_id:
            return _valid_path_hash_mode(node.get("path_hash_mode", 2))
    return None


def _ensure_mc_tx_allowed(action="MC transmission"):
    if CONFIG.get("silent_mode"):
        raise RuntimeError(f"Silent Running active — {action} blocked")


def _mc_text_bytes(text):
    return len((text or "").encode("utf-8"))


def _mc_channel_msg_limit(config_id):
    with mc_connections_lock:
        state = mc_connections.get(config_id, {}) or {}
        info = state.get("node_info", {}) or {}
        advert_name = info.get("name") or state.get("config", {}).get("name") or ""
    return max(0, MC_MAX_DM_MSG_BYTES - _mc_text_bytes(advert_name) - MC_CHANNEL_NAME_OVERHEAD_BYTES - MC_CHANNEL_SCOPE_HEADROOM_BYTES)


def _mc_take_byte_chunk(text, limit):
    out = ""
    for ch in str(text or ""):
        if _mc_text_bytes(out + ch) > limit:
            break
        out += ch
    return out


def _mc_split_plain_text(text, limit):
    if limit <= 0:
        raise ValueError("MC message limit is too small to send safely")
    chunks = []
    rest = str(text or "").strip()
    while rest:
        if _mc_text_bytes(rest) <= limit:
            chunks.append(rest)
            break
        chunk = _mc_take_byte_chunk(rest, limit)
        split_at = -1
        for idx, ch in enumerate(chunk):
            if ch.isspace():
                split_at = idx
        if split_at > 0 and _mc_text_bytes(chunk[:split_at]) >= int(limit * 0.5):
            chunk = chunk[:split_at].rstrip()
        if not chunk:
            raise ValueError("MC message contains a character that is too large for the available byte limit")
        chunks.append(chunk)
        rest = rest[len(chunk):].lstrip()
        if len(chunks) > MC_MAX_AUTO_SPLIT_PARTS:
            break
    return chunks


def _mc_split_text_by_bytes(text, limit):
    if limit <= 0:
        raise ValueError("MC message limit is too small to send safely")
    if _mc_text_bytes(text) <= limit:
        return [str(text or "").strip()]
    chunks = _mc_split_plain_text(text, limit)
    if len(chunks) <= 1:
        return chunks
    for _ in range(4):
        total = len(chunks)
        if total > MC_MAX_AUTO_SPLIT_PARTS:
            raise ValueError(f"MC message too long to auto-split (max {MC_MAX_AUTO_SPLIT_PARTS} parts)")
        prefix = f"[{total}/{total}] "
        body_limit = limit - _mc_text_bytes(prefix)
        if body_limit < 40:
            raise ValueError("MC message limit is too small to auto-split safely")
        next_chunks = _mc_split_plain_text(text, body_limit)
        if len(next_chunks) == total:
            return [f"[{idx + 1}/{total}] {part}" for idx, part in enumerate(next_chunks)]
        chunks = next_chunks
    raise ValueError("MC message could not be split into safe chunks")


def _remember_rx_log(config_id, payload):
    entries = _mc_recent_rx_logs.setdefault(config_id, [])
    entries.append({
        "stored_at": time.time(),
        **dict(payload or {}),
    })
    if len(entries) > 20:
        del entries[:-20]


def _rx_sender_prefix(entry):
    for key in ("pubkey_pre", "pubkey_prefix", "sender_pubkey_pre", "source_pubkey_pre", "from_pubkey_pre"):
        value = entry.get(key)
        if value:
            return str(value)
    return ""


def _payload_matches_subtype(payload_typename, subtype):
    if not payload_typename:
        return False
    name = str(payload_typename).upper()
    if subtype == "dm":
        return any(token in name for token in ("DM", "CONTACT", "DIRECT", "PRIV", "TEXT_MSG"))
    if subtype == "channel":
        return any(token in name for token in ("CHAN", "CHANNEL", "BROADCAST", "GRP_TXT", "GRP_DATA", "TXT", "MSG", "TEXT"))
    return False


def _mc_path_len_from_hex(path, path_hash_size):
    if not path:
        return None
    try:
        size = int(path_hash_size)
    except (TypeError, ValueError):
        return None
    if size <= 0:
        return None
    return max(0, len(str(path)) // (size * 2))


def _mc_path_hash_size_from_msg(msg, rx=None):
    for source in (rx or {}, msg or {}):
        value = source.get("path_hash_size")
        if value is not None:
            try:
                size = int(value)
                if size > 0:
                    return size
            except (TypeError, ValueError):
                pass
    for source in (rx or {}, msg or {}):
        mode = source.get("path_hash_mode")
        if mode is not None:
            try:
                mode = int(mode)
                if mode >= 0:
                    return mode + 1
            except (TypeError, ValueError):
                pass
    return None


def _mc_message_path_fields(msg, rx=None):
    msg = msg or {}
    rx = rx or {}
    if rx.get("path"):
        rx_size = _mc_path_hash_size_from_msg(msg, rx)
        rx_len = rx.get("path_len")
        if rx_len in (None, -1, 255):
            rx_len = _mc_path_len_from_hex(rx.get("path"), rx_size)
        return {
            "path": rx.get("path"),
            "path_len": rx_len,
            "path_hash_mode": (rx_size - 1) if rx_size else msg.get("path_hash_mode"),
            "path_hash_size": rx_size,
        }
    if msg.get("path"):
        msg_size = _mc_path_hash_size_from_msg(msg, None)
        msg_len = msg.get("path_len")
        if msg_len in (None, -1, 255):
            msg_len = _mc_path_len_from_hex(msg.get("path"), msg_size)
        return {
            "path": msg.get("path"),
            "path_len": msg_len,
            "path_hash_mode": msg.get("path_hash_mode"),
            "path_hash_size": msg_size,
        }
    return {
        "path": None,
        "path_len": msg.get("path_len", rx.get("path_len")),
        "path_hash_mode": msg.get("path_hash_mode"),
        "path_hash_size": _mc_path_hash_size_from_msg(msg, rx),
    }


def _merge_mc_contact_records(old_contact, new_contact):
    merged = dict(old_contact or {})
    for key, value in dict(new_contact or {}).items():
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def _merge_mc_contacts(old_contacts, new_contacts):
    """Merge refreshed MC contacts without dropping previously known nodes.

    Some radios return partial contact sets during startup/path refresh. Those
    sparse reads should not erase already-known repeaters, names, or GPS data.
    Prefer fresh non-empty values from the device, but keep older records when
    they still carry useful identity or routing metadata.
    """
    old_contacts = dict(old_contacts or {})
    new_contacts = dict(new_contacts or {})
    merged = {}
    for pubkey, contact in new_contacts.items():
        merged[pubkey] = _merge_mc_contact_records(old_contacts.get(pubkey), contact)
    for pubkey, contact in old_contacts.items():
        if pubkey in merged:
            continue
        merged[pubkey] = dict(contact or {})
    return merged


def _best_recent_rx_for_message(config_id, subtype, now_ts, sender_prefix=None, msg=None):
    """Best-effort correlation between a message event and a recent RX_LOG_DATA event.

    Prefer entries that match the sender prefix and payload subtype. Fall back
    gradually rather than binding a message to an unrelated recent RX event.
    """
    entries = _mc_recent_rx_logs.get(config_id, [])
    if not entries:
        return None
    cutoff = now_ts - 45.0
    recent = [e for e in entries if e.get("stored_at", 0) >= cutoff]
    if not recent:
        return None

    usable = [
        e for e in recent
        if e.get("payload_typename") not in ("ADVERT", "REQ")
    ]
    if not usable:
        usable = recent

    msg = msg or {}
    sender_key = (sender_prefix or "")[:12]
    msg_text = str(msg.get("text") or "")
    msg_ts = msg.get("sender_timestamp") or msg.get("time")

    def _score(e):
        age = max(0.0, now_ts - float(e.get("stored_at", now_ts) or now_ts))
        score = -age
        if e.get("path"):
            score += 1000
        if _payload_matches_subtype(e.get("payload_typename"), subtype):
            score += 250
        e_sender = _rx_sender_prefix(e)
        if sender_key and e_sender and e_sender.startswith(sender_key):
            score += 500
        if msg_ts is not None and e.get("sender_timestamp") == msg_ts:
            score += 700
        if msg_text and e.get("message") and str(e.get("message")) == msg_text:
            score += 700
        return score

    scored = sorted(usable, key=_score)
    best = scored[-1] if scored else None
    if best and (_payload_matches_subtype(best.get("payload_typename"), subtype) or best.get("path")):
        return best

    return best


def _run_event_loop():
    global _mc_loop
    _mc_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_mc_loop)
    _mc_loop_ready.set()
    _mc_loop.run_forever()


def start_mc_loop():
    """Start the asyncio event loop thread. Call once at startup."""
    t = threading.Thread(target=_run_event_loop, daemon=True, name="mc-asyncio-loop")
    t.start()
    _mc_loop_ready.wait(timeout=5)
    log.info("[MC] Asyncio event loop started")


def run_mc(coro, timeout=10):
    """Submit an async coroutine to the MC event loop from any sync thread.
    Returns the result or raises on timeout / exception.

    asyncio.wait_for wraps the coro inside the loop so a timeout cancels the
    task properly — without this the coroutine keeps running and blocks the
    serial command channel for all subsequent calls.
    """
    if _mc_loop is None or not _mc_loop.is_running():
        raise RuntimeError("MC event loop not running")

    async def _wrapped():
        return await asyncio.wait_for(coro, timeout=timeout)

    future = asyncio.run_coroutine_threadsafe(_wrapped(), _mc_loop)
    try:
        return future.result(timeout=timeout + 5)
    except (concurrent.futures.TimeoutError, asyncio.TimeoutError):
        raise RuntimeError("MC operation timed out")


async def _drain_mc_queue(mc, config_id, name, reason="event", timeout=5, max_messages=25):
    """Drain queued MC messages once.

    Some MC firmware/USB combinations do not consistently emit MESSAGES_WAITING
    for every queued channel message. A guarded periodic drain keeps OM's view in
    sync without stacking concurrent get_msg calls for the same radio.

    If MESSAGES_WAITING fires while a drain is already active, _mc_drain_pending
    records it. After the current drain finishes, one follow-up drain is triggered
    automatically so those messages are not left waiting until the next periodic poll.
    """
    if config_id in _mc_drain_active:
        _mc_drain_pending.add(config_id)   # remember — re-drain after current one finishes
        return 0
    _mc_drain_active.add(config_id)
    _mc_drain_pending.discard(config_id)   # clear any stale pending flag from before this drain
    drained = 0
    try:
        while drained < max_messages:
            result = await asyncio.wait_for(mc.commands.get_msg(), timeout=timeout)
            if result.type in (EventType.NO_MORE_MSGS, EventType.ERROR):
                break
            drained += 1
        if drained:
            log.info(f"[MC:{name}] Queue drained: {drained} message(s) ({reason})")
        return drained
    except Exception as e:
        if drained:
            log.info(f"[MC:{name}] Queue drained: {drained} message(s) ({reason}, partial)")
        else:
            log.debug(f"[MC:{name}] Queue drain skipped/failed ({reason}): {e}")
        return drained
    finally:
        _mc_drain_active.discard(config_id)
        # If MESSAGES_WAITING fired while we were draining, re-drain now rather than
        # waiting up to 8s for the next periodic poll.
        if config_id in _mc_drain_pending:
            _mc_drain_pending.discard(config_id)
            _mc_bg_task(_drain_mc_queue(mc, config_id, name, reason=f"{reason}_requeue"), "drain_requeue")


# ---------------------------------------------------------------------------
# Connect a single MC node
# ---------------------------------------------------------------------------


def _dtr_reset_port(port, name=""):
    """Brief RTS pulse to reset ESP32 serial state without manual replug."""
    import serial as _serial
    try:
        s = _serial.Serial(port, 115200, timeout=0.5)
        s.setRTS(True)
        time.sleep(0.2)
        s.setRTS(False)
        s.close()
        log.info(f"[MC:{name}] DTR reset sent on {port}")
    except Exception as e:
        log.warning(f"[MC:{name}] DTR reset failed on {port}: {e}")


async def _connect_mc_node_async(node_cfg):
    config_id = node_cfg["id"]
    name      = node_cfg.get("name", config_id)
    port      = node_cfg.get("port", "")

    # Always resolve port by USB serial if available (port in config is display-only)
    usb_serial = node_cfg.get("usb_serial", "")
    if usb_serial:
        import serial.tools.list_ports
        resolved = next(
            (p.device for p in serial.tools.list_ports.comports() if p.serial_number == usb_serial),
            None,
        )
        if resolved:
            port = resolved
        else:
            log.warning(f"[MC:{name}] USB serial {usb_serial} not found on any port")
            port = None
    if not port:
        log.warning(f"[MC:{name}] No port found, skipping connect")
        return

    log.info(f"[MC:{name}] Connecting on {port}")
    with mc_connections_lock:
        if config_id not in mc_connections:
            log.warning(f"[MC:{name}] Node removed before connect started, aborting")
            return
        mc_connections[config_id]["status"] = "connecting"

    try:
        # default_timeout=75: send_appstart will wait 75s for a response.
        # TTLP (and similar devices) need ~60s to load flash after USB connect.
        # Holding the port open avoids the HUPCL reset that repeated open/close causes.
        mc = await asyncio.wait_for(MeshCore.create_serial(port, 115200, default_timeout=75.0), timeout=80)
    except Exception as e:
        log.warning(f"[MC:{name}] Connect failed: {e}")
        with mc_connections_lock:
            if config_id in mc_connections:
                mc_connections[config_id]["status"] = "disconnected"
        return

    if mc is None:
        log.warning(f"[MC:{name}] No response to send_appstart on {port} — replug USB to recover")
        with mc_connections_lock:
            if config_id in mc_connections:
                mc_connections[config_id]["status"] = "disconnected"
        return

    # Sync clock (freshly flashed devices default to epoch 0)
    try:
        await mc.commands.set_time(int(datetime.datetime.now().timestamp()))
    except Exception:
        pass

    # Get self info
    try:
        r = await asyncio.wait_for(mc.commands.send_appstart(), timeout=8)
        node_info = r.payload if r.type == EventType.SELF_INFO else {}
    except Exception as e:
        log.warning(f"[MC:{name}] send_appstart failed: {e}")
        node_info = {}

    # Get device info to capture max_channels (and max_contacts) — non-fatal if it fails
    try:
        dev_r = await asyncio.wait_for(mc.commands.send_device_query(), timeout=10)
        if dev_r.type == EventType.DEVICE_INFO:
            dev = dev_r.payload or {}
            if dev.get("max_channels"):
                node_info["max_channels"] = dev["max_channels"]
            if dev.get("max_contacts"):
                node_info["max_contacts"] = dev["max_contacts"]
    except Exception as e:
        log.warning(f"[MC:{name}] send_device_query failed (max_channels defaulting to 8): {e}")

    # radio_bw: reader.py divides raw Hz by 1000 → returns kHz (e.g. 125)
    # No conversion needed; radio_freq uses same formula but raw is kHz → returns MHz

    # Re-apply saved radio params (firmware doesn't persist across power cycles)
    saved_radio = node_cfg.get("radio_params")
    if saved_radio:
        try:
            await mc.commands.set_radio(
                float(saved_radio["freq"]), float(saved_radio["bw"]),
                int(saved_radio["sf"]),   int(saved_radio["cr"]),
            )
            node_info.update({
                "radio_freq": saved_radio["freq"],
                "radio_bw":   saved_radio["bw"],
                "radio_sf":   saved_radio["sf"],
                "radio_cr":   saved_radio["cr"],
            })
            log.info(f"[MC:{name}] Re-applied saved radio params: {saved_radio}")
        except Exception as e:
            log.warning(f"[MC:{name}] Could not re-apply radio params: {e}")

    saved_hash_mode = node_cfg.get("path_hash_mode", 2)
    if saved_hash_mode not in ("", None):
        try:
            result = await _set_path_hash_mode_on_mc(mc, saved_hash_mode)
            node_info["path_hash_mode"] = result["applied"]
            if result["applied"] != _valid_path_hash_mode(saved_hash_mode):
                with CONFIG_LOCK:
                    for node in CONFIG.get("mc_nodes", []):
                        if node.get("id") == config_id:
                            node["path_hash_mode"] = result["applied"]
                            break
                    save_config()
                log.warning(
                    f"[MC:{name}] Requested path hash mode {saved_hash_mode} unavailable; "
                    f"using {result['applied']}"
                )
            else:
                log.info(f"[MC:{name}] Applied path hash mode {result['applied']}")
        except Exception as e:
            log.warning(f"[MC:{name}] Could not apply saved path hash mode {saved_hash_mode}: {e}")
            node_info["path_hash_mode"] = 0
            with CONFIG_LOCK:
                for node in CONFIG.get("mc_nodes", []):
                    if node.get("id") == config_id:
                        node["path_hash_mode"] = 0
                        break
                save_config()

    node_id = node_info.get("public_key", "")[:12]  # 6-byte pubkey prefix as ID

    # Get contacts — retry up to 3 times with 2s gaps if empty.
    # Radio may need a moment to load persistent storage after send_appstart().
    contacts = {}
    for _attempt in range(3):
        try:
            r = await asyncio.wait_for(mc.commands.get_contacts(), timeout=8)
            contacts = r.payload if r.type == EventType.CONTACTS else {}
        except Exception:
            contacts = {}
        if contacts:
            break
        if _attempt < 2:
            await asyncio.sleep(2)

    # Don't downgrade contacts — if initial fetch returned fewer than previously stored,
    # keep old contacts until background retry confirms the authoritative count.
    with mc_connections_lock:
        old_contacts = mc_connections[config_id].get("contacts", {})
        merged_contacts = _merge_mc_contacts(old_contacts, contacts)
        stored_contacts = merged_contacts if len(contacts) >= len(old_contacts) else old_contacts
        mc_connections[config_id].update({
            "mc":        mc,
            "name":      name,
            "status":    "connected",
            "port":      port,
            "node_id":   node_id,
            "node_info": node_info,
            "contacts":  stored_contacts,
        })

    log.info(f"[MC:{name}] Connected — node_id={node_id} freq={node_info.get('radio_freq')} "
             f"contacts={len(contacts)} (stored={len(stored_contacts)})")

    push_to_sse({"type": "mc_status", "radio_id": config_id,
                 "status": "connected", "name": name})

    # Subscribe to live events
    _subscribe_mc_events(mc, config_id, name)

    # Drain any messages queued while OM was offline, and keep polling lightly in
    # case the device queues messages without raising MESSAGES_WAITING.
    _mc_bg_task(_drain_mc_queue(mc, config_id, name, reason="startup", timeout=5), "drain_startup")

    async def _periodic_drain():
        await asyncio.sleep(8)
        while True:
            # Non-blocking acquire: if a Flask route holds the lock, don't stall the
            # asyncio reader loop — assume still connected and attempt the drain anyway.
            if mc_connections_lock.acquire(blocking=False):
                try:
                    state = mc_connections.get(config_id) or {}
                    still_connected = state.get("status") == "connected" and state.get("mc") is mc
                finally:
                    mc_connections_lock.release()
            else:
                still_connected = True
            if not still_connected:
                return
            await _drain_mc_queue(mc, config_id, name, reason="poll")
            await asyncio.sleep(8)
    _mc_bg_task(_periodic_drain(), "periodic_drain")

    # Startup flood advert — announces our identity to the whole mesh.
    # Essential after a reflash or first connection so that remote nodes (repeaters, etc.)
    # can add our pubkey to their NVS. Without this, STATUS_REQUEST (ping) goes out but
    # the remote can't decrypt it (ECDH requires our pubkey) and won't respond.
    async def _startup_advert():
        await asyncio.sleep(8)   # let serial/drain settle first
        if CONFIG.get("silent_mode"):
            log.info(f"[MC:{name}] Silent Running active — startup advert suppressed")
            return
        await _send_advert_async(config_id, flood=True)
        log.info(f"[MC:{name}] Startup flood advert sent — remote nodes will learn our identity")
    _mc_bg_task(_startup_advert(), "startup_advert")

    # Retry if initial fetch looks incomplete (empty or fewer than previously stored).
    # TTLP (and similar devices) can take 15–30s to load flash storage after USB reconnect.
    if len(contacts) < len(old_contacts) or not contacts:
        _mc_bg_task(_retry_contacts_async(mc, config_id, name), "retry_contacts")


async def _retry_contacts_async(mc, config_id, name):
    """Background retry for incomplete contacts fetch.
    TTLP and similar devices can take 15-30s to load flash storage after USB reconnect.
    Runs all intervals and keeps the best (highest count) result — no early exit,
    because partial results at +5s may be lower than the final count at +30s."""
    best_contacts = {}
    elapsed = 0
    for delay in [10, 20, 30, 30]:
        await asyncio.sleep(delay)
        elapsed += delay
        with mc_connections_lock:
            if mc_connections.get(config_id, {}).get("status") != "connected":
                return  # disconnected while waiting
        try:
            r = await asyncio.wait_for(mc.commands.get_contacts(), timeout=8)
            contacts = r.payload if r.type == EventType.CONTACTS else {}
        except Exception:
            contacts = {}
        if len(contacts) > len(best_contacts):
            best_contacts = contacts
        log.info(f"[MC:{name}] Contacts retry at +{elapsed}s: {len(contacts)} (best so far: {len(best_contacts)})")

    if best_contacts:
        with mc_connections_lock:
            if config_id in mc_connections:
                old_contacts = mc_connections[config_id].get("contacts", {})
                mc_connections[config_id]["contacts"] = _merge_mc_contacts(old_contacts, best_contacts)
        log.info(f"[MC:{name}] Contacts retry complete: {len(best_contacts)} contacts stored")
        now = int(time.time())
        for pubkey, c in best_contacts.items():
            _push_mc_node({
                "type":        "mc_node",
                "radio_id":    config_id,
                "radio_name":  name,
                "id":          pubkey[:12],
                "long_name":   c.get("adv_name", pubkey[:8]),
                "short_name":  c.get("adv_name", "?")[:4].upper(),
                "latitude":    c.get("adv_lat") or None,
                "longitude":   c.get("adv_lon") or None,
                "last_heard_ts":      c.get("last_advert", now),
                "out_path_len":       c.get("out_path_len", -1),
                "out_path":           c.get("out_path", ""),
                "out_path_hash_mode": c.get("out_path_hash_mode"),
                "out_path_hash_size": c.get("out_path_hash_size"),
                "contact_type": c.get("type", 0),
                "network":     "mc",
            })
    else:
        log.warning(f"[MC:{name}] Contacts retry exhausted — still empty after 90s")


def _invoke_mc_bot(msg, config_id, subtype):
    """Lazy-import wrapper so bot.py and mesh_mc.py don't create a circular import."""
    try:
        from bot import handle_mc_bot_command
        handle_mc_bot_command(msg, config_id, subtype)
    except Exception as e:
        log.warning(f"[MCBot] invoke error: {e}")


def _subscribe_mc_events(mc, config_id, name):
    """Wire up event callbacks — called from within the asyncio loop thread."""

    def _touch_contact_seen(pubkey_or_prefix, ts=None):
        ts = int(ts or time.time())
        if not pubkey_or_prefix:
            return None

        # Non-blocking acquire: called from asyncio callbacks in the event loop thread.
        # If a Flask route holds mc_connections_lock, blocking here would stall the
        # asyncio serial reader — potentially causing the OS serial buffer to overflow
        # and dropping subsequent incoming packets. Skipping the last_seen update is
        # acceptable; message delivery (push_to_sse) is unaffected.
        if not mc_connections_lock.acquire(blocking=False):
            return None

        try:
            state = mc_connections.get(config_id)
            if not state:
                return None

            contacts = state.setdefault("contacts", {})
            matched_key = None
            for pubkey in contacts.keys():
                if pubkey == pubkey_or_prefix or pubkey.startswith(pubkey_or_prefix) or pubkey_or_prefix.startswith(pubkey):
                    matched_key = pubkey
                    break

            if matched_key is None:
                matched_key = pubkey_or_prefix
                contacts[matched_key] = {"public_key": matched_key}

            existing = contacts.get(matched_key, {"public_key": matched_key})
            existing["last_seen_ts"] = ts
            contacts[matched_key] = existing
            return existing
        finally:
            mc_connections_lock.release()

    def on_advert(event):
        try:
            n = event.payload
            pubkey = n.get("public_key", "")
            # ADVERTISEMENT payload only contains public_key — no name, coords, or type.
            # Merge into the existing contact record (preserving name/coords/type learned
            # via NEW_CONTACT or get_contacts) rather than replacing it with the sparse payload.
            existing = _touch_contact_seen(pubkey)
            if existing is None:
                existing = {"public_key": pubkey}
            existing["last_advert"] = int(time.time())
            with mc_connections_lock:
                if config_id in mc_connections:
                    mc_connections[config_id]["contacts"][pubkey] = existing
            _push_mc_node({
                "type":              "mc_node",
                "radio_id":          config_id,
                "radio_name":        name,
                "id":                pubkey[:12],
                "long_name":         existing.get("adv_name", pubkey[:8]),
                "short_name":        existing.get("adv_name", "?")[:4].upper(),
                "latitude":          existing.get("adv_lat") or None,
                "longitude":         existing.get("adv_lon") or None,
                "last_heard_ts":     int(time.time()),
                "out_path_len":      existing.get("out_path_len", -1),
                "out_path":          existing.get("out_path", ""),
                "out_path_hash_mode": existing.get("out_path_hash_mode"),
                "out_path_hash_size": existing.get("out_path_hash_size"),
                "contact_type":      existing.get("type", 0),
                "network":           "mc",
            })
        except Exception as e:
            log.warning(f"[MC:{name}] on_advert error: {e}")

    def on_chan_msg(event):
        try:
            msg = event.payload
            now = int(time.time())
            _touch_contact_seen(msg.get("pubkey_pre"), now)
            rx = _best_recent_rx_for_message(
                config_id, "channel", time.time(), sender_prefix=msg.get("pubkey_pre"), msg=msg
            )
            path_fields = _mc_message_path_fields(msg, rx)
            sse_msg = {
                "type":       "mc_message",
                "radio_id":   config_id,
                "radio_name": name,
                "network":    "mc",
                "subtype":    "channel",
                "channel":    msg.get("channel_idx", 0),
                "from_id":    msg.get("pubkey_pre", "?"),
                "text":       msg.get("text", ""),
                "ts":         msg.get("time", now),
                "sent":       False,
                "route_type": msg.get("route_typename") or (rx or {}).get("route_typename"),
                "path":       path_fields["path"],
                "path_len":   path_fields["path_len"],
                "path_hash_mode": path_fields["path_hash_mode"],
                "path_hash_size": path_fields["path_hash_size"],
                "rx_rssi":    msg.get("rssi", (rx or {}).get("rssi")),
                "rx_snr":     msg.get("snr", (rx or {}).get("snr")),
            }
            push_to_sse(sse_msg)
            log.info(f"[MC:{name}] Channel msg from {msg.get('pubkey_pre','?')}: {msg.get('text','')[:60]}")
            threading.Thread(
                target=maybe_forward_mc_message, args=(dict(sse_msg),),
                daemon=True,
            ).start()
            # Bot command handling (background thread to avoid blocking asyncio loop)
            threading.Thread(
                target=_invoke_mc_bot, args=(dict(msg), config_id, "channel"),
                daemon=True,
            ).start()
        except Exception as e:
            log.warning(f"[MC:{name}] on_chan_msg error: {e}")

    def on_dm(event):
        try:
            msg = event.payload
            now = int(time.time())
            _touch_contact_seen(msg.get("pubkey_prefix"), now)
            rx = _best_recent_rx_for_message(
                config_id, "dm", time.time(), sender_prefix=msg.get("pubkey_prefix"), msg=msg
            )
            path_fields = _mc_message_path_fields(msg, rx)
            sse_msg = {
                "type":       "mc_message",
                "radio_id":   config_id,
                "radio_name": name,
                "network":    "mc",
                "subtype":    "dm",
                "from_id":    msg.get("pubkey_prefix", "?"),
                "text":       msg.get("text", ""),
                "ts":         msg.get("time", now),
                "sent":       False,
                "route_type": msg.get("route_typename") or (rx or {}).get("route_typename"),
                "path":       path_fields["path"],
                "path_len":   path_fields["path_len"],
                "path_hash_mode": path_fields["path_hash_mode"],
                "path_hash_size": path_fields["path_hash_size"],
                "rx_rssi":    msg.get("rssi", (rx or {}).get("rssi")),
                "rx_snr":     msg.get("snr", (rx or {}).get("snr")),
            }
            push_to_sse(sse_msg)
            log.info(f"[MC:{name}] DM from {msg.get('pubkey_prefix','?')}: {msg.get('text','')[:60]}"
                     f" | path={repr(sse_msg.get('path'))[:40]} path_len={sse_msg.get('path_len')} hash_size={sse_msg.get('path_hash_size')}")
            # Bot command handling (background thread to avoid blocking asyncio loop)
            threading.Thread(
                target=_invoke_mc_bot, args=(dict(msg), config_id, "dm"),
                daemon=True,
            ).start()
        except Exception as e:
            log.warning(f"[MC:{name}] on_dm error: {e}")

    def on_new_contact(event):
        try:
            c = event.payload
            pubkey = c.get("public_key", "")
            c["last_seen_ts"] = int(time.time())
            with mc_connections_lock:
                if config_id in mc_connections:
                    mc_connections[config_id]["contacts"][pubkey] = c
            # Emit as mc_node so the frontend renders it the same way as an advertisement
            _push_mc_node({
                "type":              "mc_node",
                "radio_id":          config_id,
                "radio_name":        name,
                "id":                pubkey[:12],
                "long_name":         c.get("adv_name", pubkey[:8]),
                "short_name":        c.get("adv_name", "?")[:4].upper(),
                "latitude":          c.get("adv_lat") or None,
                "longitude":         c.get("adv_lon") or None,
                "last_heard_ts":     int(time.time()),
                "out_path_len":      c.get("out_path_len", -1),
                "out_path":          c.get("out_path", ""),
                "out_path_hash_mode": c.get("out_path_hash_mode"),
                "out_path_hash_size": c.get("out_path_hash_size"),
                "contact_type":      c.get("type", 0),
                "network":           "mc",
            })
        except Exception as e:
            log.warning(f"[MC:{name}] on_new_contact error: {e}")

    def on_disconnected(event):
        try:
            log.warning(f"[MC:{name}] Disconnected event received")
            with mc_connections_lock:
                if config_id in mc_connections:
                    mc_connections[config_id]["status"] = "disconnected"
                    mc_connections[config_id]["mc"]     = None
            push_to_sse({"type": "mc_status", "radio_id": config_id,
                         "status": "disconnected", "name": name})
        except Exception as e:
            log.warning(f"[MC:{name}] on_disconnected error: {e}")

    def on_status_response(event):
        try:
            p = event.payload
            _touch_contact_seen(p.get("pubkey_pre"), int(time.time()))
            log.info(f"[MC:{name}] STATUS_RESPONSE from {p.get('pubkey_pre', '?')}")
            push_to_sse({
                "type":         "mc_status_response",
                "radio_id":     config_id,
                "pubkey_pre":   p.get("pubkey_pre", ""),
                "bat":          p.get("bat"),
                "last_rssi":    p.get("last_rssi"),
                "last_snr":     p.get("last_snr"),
                "noise_floor":  p.get("noise_floor"),
                "uptime":       p.get("uptime"),
                "nb_recv":      p.get("nb_recv"),
                "nb_sent":      p.get("nb_sent"),
                "tx_queue_len": p.get("tx_queue_len"),
                "airtime":      p.get("airtime"),
                "rx_airtime":   p.get("rx_airtime"),
                "sent_flood":   p.get("sent_flood"),
                "sent_direct":  p.get("sent_direct"),
                "recv_flood":   p.get("recv_flood"),
                "recv_direct":  p.get("recv_direct"),
                "flood_dups":   p.get("flood_dups"),
                "direct_dups":  p.get("direct_dups"),
            })
        except Exception as e:
            log.warning(f"[MC:{name}] on_status_response error: {e}")

    def on_path_update(event):
        """PATH_UPDATE signals the contact's routing path changed.
        The library marks contacts dirty but does NOT update our cache in-place.
        Refresh contacts first so we push the new path data, not stale data.

        Rate-limited: if a refresh is already in-flight for this radio, skip the
        new one. A busy mesh can fire PATH_UPDATE rapidly; stacking get_contacts()
        calls occupies the serial command channel and delays message drains."""
        pubkey = event.payload.get("public_key", "")
        if not pubkey:
            return

        if config_id in _mc_path_refresh_active:
            log.debug(f"[MC:{name}] on_path_update: refresh already in-flight, skipping for {pubkey[:12]}")
            return

        async def _refresh_and_push():
            _mc_path_refresh_active.add(config_id)
            try:
                r = await mc.commands.get_contacts()
                if r.type == EventType.CONTACTS:
                    with mc_connections_lock:
                        if config_id in mc_connections:
                            new_contacts = r.payload or {}
                            old_contacts = mc_connections[config_id].get("contacts", {})
                            if new_contacts or not old_contacts:
                                mc_connections[config_id]["contacts"] = _merge_mc_contacts(old_contacts, new_contacts)
                with mc_connections_lock:
                    c = mc_connections.get(config_id, {}).get("contacts", {}).get(pubkey, {})
                if not c:
                    return
                _push_mc_node({
                    "type":              "mc_node",
                    "radio_id":          config_id,
                    "radio_name":        name,
                    "id":                pubkey[:12],
                    "long_name":         c.get("adv_name", pubkey[:8]),
                    "short_name":        c.get("adv_name", "?")[:4].upper(),
                    "latitude":          c.get("adv_lat") or None,
                    "longitude":         c.get("adv_lon") or None,
                    "last_heard_ts":     int(time.time()),
                    "out_path_len":      c.get("out_path_len", -1),
                    "out_path":          c.get("out_path", ""),
                    "out_path_hash_mode": c.get("out_path_hash_mode"),
                    "out_path_hash_size": c.get("out_path_hash_size"),
                    "network":           "mc",
                })
                log.info(f"[MC:{name}] on_path_update: pushed updated path for {pubkey[:12]}")
            except Exception as e:
                log.warning(f"[MC:{name}] on_path_update error: {e}")
            finally:
                _mc_path_refresh_active.discard(config_id)

        _mc_bg_task(_refresh_and_push(), "path_refresh")

    def on_messages_waiting(event):
        """Firmware has queued messages — drain with repeated get_msg calls.
        The library dispatches CONTACT_MSG_RECV / CHANNEL_MSG_RECV for each
        message retrieved, so on_dm / on_chan_msg fire automatically."""
        log.info(f"[MC:{name}] MESSAGES_WAITING — draining queue")
        _mc_bg_task(_drain_mc_queue(mc, config_id, name, reason="event", timeout=5), "drain_event")

    def on_trace_data(event):
        try:
            p = event.payload
            path = p.get("path", [])
            log.info(f"[MC:{name}] TRACE_DATA tag={p.get('tag')} hops={p.get('path_len',0)} "
                     f"path={[{'hash':n.get('hash','?'),'snr':n.get('snr')} for n in path]}")
            push_to_sse({
                "type":     "mc_trace_data",
                "radio_id": config_id,
                "tag":      p.get("tag"),
                "path_len": p.get("path_len", 0),
                "path_hash_size": p.get("path_hash_size"),
                "path":     path,   # [{hash, snr}, ...], last entry has only snr (our radio)
                "flags":    p.get("flags", 0),
                "auth_hex": p.get("auth", 0).to_bytes(4, 'little').hex(),  # responding node's 4-byte hash (wire byte order)
            })
        except Exception as e:
            log.warning(f"[MC:{name}] on_trace_data error: {e}")

    def on_rx_log_data(event):
        try:
            _remember_rx_log(config_id, event.payload)
        except Exception as e:
            log.warning(f"[MC:{name}] on_rx_log_data error: {e}")

    mc.subscribe(EventType.ADVERTISEMENT,     on_advert)
    mc.subscribe(EventType.CHANNEL_MSG_RECV,  on_chan_msg)
    mc.subscribe(EventType.CONTACT_MSG_RECV,  on_dm)
    mc.subscribe(EventType.NEW_CONTACT,       on_new_contact)
    mc.subscribe(EventType.DISCONNECTED,      on_disconnected)
    mc.subscribe(EventType.STATUS_RESPONSE,   on_status_response)
    mc.subscribe(EventType.PATH_UPDATE,       on_path_update)
    mc.subscribe(EventType.MESSAGES_WAITING,  on_messages_waiting)
    mc.subscribe(EventType.TRACE_DATA,        on_trace_data)
    mc.subscribe(EventType.RX_LOG_DATA,       on_rx_log_data)

    def on_binary_response(event):
        try:
            tag = event.attributes.get("tag", "?")
            log.info(f"[MC:{name}] BINARY_RESPONSE: tag={tag} data_len={len(event.payload.get('data',''))//2}B")
        except Exception as e:
            log.warning(f"[MC:{name}] on_binary_response error: {e}")
    mc.subscribe(EventType.BINARY_RESPONSE, on_binary_response)

    def on_debug_event(event):
        if config_id in _mc_debug_flags:
            log.info(f"[MC:{name}] DBG_EVENT: {event.type.name} | {event.payload}")
    mc.subscribe(None, on_debug_event)   # None = catch ALL event types


def connect_mc_node(node_cfg):
    """Sync wrapper — submit connect coroutine to the MC loop."""
    config_id = node_cfg["id"]
    with mc_connections_lock:
        mc_connections.setdefault(config_id, {
            "mc": None, "status": "disconnected",
            "config": node_cfg, "node_id": "",
            "node_info": {}, "contacts": {},
        })
    try:
        run_mc(_connect_mc_node_async(node_cfg), timeout=90)
    except Exception as e:
        log.warning(f"[MC:{node_cfg.get('name', config_id)}] connect_mc_node failed: {e}")
        with mc_connections_lock:
            mc_connections[config_id]["status"] = "disconnected"


# ---------------------------------------------------------------------------
# Reconnect loop
# ---------------------------------------------------------------------------

def reconnect_mc_loop():
    """Background thread — retries disconnected MC nodes every 15 seconds."""
    while True:
        time.sleep(15)
        with mc_connections_lock:
            items = [(cid, v["config"]) for cid, v in mc_connections.items()
                     if v.get("status") not in ("connected", "connecting")
                     and v.get("config", {}).get("enabled", True)]
        for config_id, node_cfg in items:
            log.info(f"[MC:{node_cfg.get('name', config_id)}] Reconnecting...")
            threading.Thread(target=connect_mc_node, args=(node_cfg,), daemon=True).start()


def mc_watchdog_loop():
    """Background thread — detects USB unplug for connected MC nodes within ~3 s.

    The meshcore library relies on asyncio I/O events to detect serial
    disconnection; if no data is flowing at unplug time the event can be
    delayed by tens of seconds.  This watchdog checks the physical USB port
    every 3 s and forces an immediate disconnect + SSE push when it's gone,
    mirroring the fast indicator behaviour of the MT side.
    """
    import serial.tools.list_ports as list_ports
    import os

    while True:
        time.sleep(3)
        with mc_connections_lock:
            items = [
                (cid, v.get("port"), v.get("config", {}), v.get("config", {}).get("name", cid))
                for cid, v in mc_connections.items()
                if v.get("status") == "connected"
            ]

        for config_id, port, config, name in items:
            usb_serial = config.get("usb_serial", "")
            gone = False

            if usb_serial:
                # Prefer USB serial number check — survives port renaming
                gone = not any(p.serial_number == usb_serial for p in list_ports.comports())
            elif port:
                gone = not os.path.exists(port)

            if gone:
                log.warning(f"[MC:{name}] USB device gone — marking disconnected")
                with mc_connections_lock:
                    if mc_connections.get(config_id, {}).get("status") == "connected":
                        mc_connections[config_id]["status"] = "disconnected"
                        mc_connections[config_id]["mc"]     = None
                push_to_sse({"type": "mc_status", "radio_id": config_id,
                             "status": "disconnected", "name": name})


# ---------------------------------------------------------------------------
# Send helpers (called from Flask routes via run_mc())
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Internal async helpers
# ---------------------------------------------------------------------------

def _get_mc(config_id):
    """Return (mc, state) under lock, or raise RuntimeError if not connected."""
    with mc_connections_lock:
        state = mc_connections.get(config_id, {})
        mc = state.get("mc")
    if mc is None:
        raise RuntimeError("Not connected")
    return mc, state


async def _set_path_hash_mode_on_mc(mc, preferred_mode):
    last_error = None
    for mode in _path_hash_mode_attempts(preferred_mode):
        try:
            await mc.commands.set_path_hash_mode(mode)
            return {
                "requested": _valid_path_hash_mode(preferred_mode),
                "applied": mode,
                "fallback": mode != _valid_path_hash_mode(preferred_mode),
            }
        except Exception as e:
            last_error = e
    raise last_error or RuntimeError("Could not set MC path hash mode")


async def _set_path_hash_mode_async(config_id, preferred_mode):
    mc, _ = _get_mc(config_id)
    result = await _set_path_hash_mode_on_mc(mc, preferred_mode)
    with mc_connections_lock:
        if config_id in mc_connections:
            info = mc_connections[config_id].setdefault("node_info", {})
            info["path_hash_mode"] = result["applied"]
            cfg = mc_connections[config_id].setdefault("config", {})
            cfg["path_hash_mode"] = result["applied"]
    return result


async def _send_chan_msg_async(config_id, chan_idx, text):
    mc, _ = _get_mc(config_id)
    return await mc.commands.send_chan_msg(chan_idx, text)


async def _send_dm_async(config_id, pubkey_prefix, text):
    mc, _ = _get_mc(config_id)
    with mc_connections_lock:
        contacts = dict(mc_connections.get(config_id, {}).get("contacts", {}))
    full_key = next((k for k in contacts if k.startswith(pubkey_prefix)), None)
    if full_key is None:
        raise ValueError(f"Contact {pubkey_prefix} not found")
    return await mc.commands.send_msg(contacts[full_key], text)


def _resolve_mc_contact(contacts, pubkey_prefix):
    if not pubkey_prefix:
        raise ValueError("Contact id is required")
    prefix = str(pubkey_prefix).strip().lower()
    matches = [k for k in contacts.keys() if str(k).lower().startswith(prefix)]
    if not matches:
        raise ValueError(f"Contact {pubkey_prefix} not found")
    if len(matches) > 1:
        raise ValueError(f"Contact prefix {pubkey_prefix} is ambiguous")
    full_key = matches[0]
    contact = dict(contacts[full_key] or {})
    contact["public_key"] = full_key
    return full_key, contact


async def _set_contact_path_async(config_id, pubkey_prefix, hop_prefixes=None, clear=False, path_hash_mode=None):
    mc, _ = _get_mc(config_id)
    with mc_connections_lock:
        contacts = dict(mc_connections.get(config_id, {}).get("contacts", {}))

    full_key, contact = _resolve_mc_contact(contacts, pubkey_prefix)

    if clear:
        result = await mc.commands.reset_path(full_key)
    else:
        hop_prefixes = list(hop_prefixes or [])
        if path_hash_mode is None:
            path_hash_mode = _configured_path_hash_mode(config_id)
            if path_hash_mode is None:
                path_hash_mode = await mc.commands.get_path_hash_mode()

        result = None
        last_error = None
        for mode in _path_hash_mode_attempts(path_hash_mode):
            try:
                hash_chars = (mode + 1) * 2
                path_hex_parts = []
                for hop_prefix in hop_prefixes:
                    hop_full_key, _ = _resolve_mc_contact(contacts, hop_prefix)
                    path_hex_parts.append(hop_full_key[:hash_chars])
                result = await mc.commands.change_contact_path(
                    dict(contact),
                    "".join(path_hex_parts),
                    path_hash_mode=mode,
                )
                path_hash_mode = mode
                break
            except Exception as e:
                last_error = e
        if result is None:
            raise last_error or RuntimeError("Could not update contact path")

    refreshed = await _get_contacts_async(config_id)
    if refreshed is None:
        raise RuntimeError("Contact path updated but refresh timed out")

    with mc_connections_lock:
        updated_contacts = dict(mc_connections.get(config_id, {}).get("contacts", {}))
    _, updated_contact = _resolve_mc_contact(updated_contacts, full_key)
    return updated_contact, result


# Tracks in-flight background advert tasks per radio — prevents stacking.
_advert_tasks: dict = {}
_statusreq_tasks: dict = {}


async def _send_advert_async(config_id, flood=False):
    """Schedule advert as a background fire-and-forget task.

    On a dense mesh (60+ contacts, narrow BW, high CR), the ESP32 must wait
    for an airtime window before TX completes and returns OK.  That can easily
    exceed 10 s, causing our run_mc timeout to fire.  Each timeout leaves the
    command queued in the ESP32; subsequent commands pile up behind it and also
    time out — a cascading failure.

    Fix: submit the advert to the asyncio loop as a detached background task.
    We return immediately, while the task waits up to the library's 75 s
    default_timeout for the OK response.  If an advert is already in-flight
    for this radio we skip the new one (no stacking).
    """
    mc, _ = _get_mc(config_id)

    # Skip if an advert is already in-flight for this radio
    existing = _advert_tasks.get(config_id)
    if existing is not None and not existing.done():
        log.info(f"[MC:{config_id}] advert already in-flight, skipping duplicate")
        return

    async def _do_advert():
        try:
            result = await mc.commands.send_advert(flood=flood)
            if result.type.name == "OK":
                log.info(f"[MC:{config_id}] advert TX confirmed (flood={flood})")
            else:
                log.warning(f"[MC:{config_id}] advert returned non-OK: {result.type} {result.payload}")
        except Exception as e:
            log.warning(f"[MC:{config_id}] background advert failed: {e}")
        finally:
            _advert_tasks.pop(config_id, None)

    task = _mc_bg_task(_do_advert(), f"advert:{config_id}")
    _advert_tasks[config_id] = task


async def _remove_contact_async(config_id, pubkey_prefix):
    mc, _ = _get_mc(config_id)
    with mc_connections_lock:
        contacts = dict(mc_connections.get(config_id, {}).get("contacts", {}))
    full_key = next((k for k in contacts if k.startswith(pubkey_prefix)), None)
    if full_key is None:
        raise ValueError(f"Contact {pubkey_prefix} not found")
    result = await mc.commands.remove_contact(full_key)
    if result and result.type.name == "OK":
        with mc_connections_lock:
            mc_connections[config_id]["contacts"].pop(full_key, None)
    return result


def remove_mc_contact(config_id, pubkey_prefix, timeout=10):
    return run_mc(_remove_contact_async(config_id, pubkey_prefix), timeout=timeout)


def enable_mc_debug(config_id, duration=60):
    """Enable verbose event logging for one MC radio for `duration` seconds.
    Logs EVERY event dispatched from the serial reader at INFO level so it appears
    in the regular app log — use to diagnose missing STATUS_RESPONSE / ping issues.
    Also writes raw library-level debug bytes (serial framing, packet parsing) to
    /tmp/overmesh-mc-raw.log so we can see if packets arrive at all."""
    meshcore_log = logging.getLogger("meshcore")
    deadline = time.time() + max(1, int(duration))
    with _mc_debug_lock:
        _mc_debug_flags.add(config_id)
        _mc_debug_until[config_id] = max(deadline, _mc_debug_until.get(config_id, 0))
        _mc_ensure_debug_logging_locked(meshcore_log)
    log.info(f"[MC:{config_id}] Debug event logging ON for {duration}s — raw bytes → /tmp/overmesh-mc-raw.log")

    def _stop():
        time.sleep(duration)
        still_enabled = False
        with _mc_debug_lock:
            expires_at = _mc_debug_until.get(config_id, 0)
            if expires_at and time.time() < expires_at:
                still_enabled = True
            else:
                _mc_debug_until.pop(config_id, None)
                _mc_debug_flags.discard(config_id)
                if not _mc_debug_until:
                    _mc_disable_debug_logging_locked(meshcore_log)
        if still_enabled:
            remaining = max(1, int(round(expires_at - time.time())))
            log.info(f"[MC:{config_id}] Debug event logging extended — {remaining}s remaining.")
        else:
            log.info(f"[MC:{config_id}] Debug event logging OFF.")

    threading.Thread(target=_stop, daemon=True).start()


async def _import_contact_async(config_id, card_data):
    """Import a contact from raw card bytes (decoded from a meshcore:// share link)."""
    mc, _ = _get_mc(config_id)
    result = await mc.commands.import_contact(card_data)
    if result is None or result.type.name not in ("OK",):
        raise RuntimeError(f"import_contact returned: {result.type if result else 'None'} {getattr(result, 'payload', '')}")
    # Refresh contacts so the new entry shows up in our in-memory list
    await _get_contacts_async(config_id)
    # Flood advert so the newly imported node learns our identity.
    # MeshCore STATUS_RESPONSE requires ECDH: remote must have our pubkey in its NVS
    # to decrypt the request and route a response back.  A flood advert propagates
    # our public key to all mesh nodes including the one we just imported.
    await _send_advert_async(config_id, flood=True)
    log.info(f"[MC:{config_id}] import_contact: flood advert scheduled — remote will learn our identity and can respond to pings")
    return result


def import_mc_contact(config_id, share_link_or_hex, timeout=15):
    """Parse a meshcore:// share link (or raw hex) and import it into the radio's contact list."""
    _ensure_mc_tx_allowed("MC contact import")
    raw = share_link_or_hex.strip()
    if raw.lower().startswith("meshcore://"):
        raw = raw[len("meshcore://"):]
    try:
        card_data = bytes.fromhex(raw)
    except ValueError:
        raise ValueError("Invalid share link — could not hex-decode the payload")
    if len(card_data) < 33:
        raise ValueError("Share link payload too short to be a valid contact card")
    return run_mc(_import_contact_async(config_id, card_data), timeout=timeout)


async def _send_statusreq_async(config_id, pubkey_prefix):
    """Schedule status request as a background fire-and-forget task.

    Same issue as advert: on a dense mesh (60+ contacts, narrow BW, high CR)
    the ESP32 must wait for airtime before MSG_SENT, easily exceeding run_mc's
    10s timeout.  That causes the route to return 400, the frontend clears
    _mcStatusReqPending, and any later STATUS_RESPONSE via SSE is ignored.

    Fix: validate the contact exists immediately (raises ValueError if not found),
    then submit the actual send as a background asyncio task and return right away.
    Before sending the STATUS request, flood-advertise our identity once more so
    the remote can refresh our pubkey in NVS. In the field this is the common
    failure mode: the request is transmitted successfully, but the far end does
    not return STATUS_RESPONSE because it cannot decrypt / route the reply.

    The STATUS_RESPONSE still arrives via the normal SSE subscription.
    """
    mc, _ = _get_mc(config_id)
    with mc_connections_lock:
        contacts = dict(mc_connections.get(config_id, {}).get("contacts", {}))
    full_key = next((k for k in contacts if k.startswith(pubkey_prefix)), None)
    if full_key is None:
        raise ValueError(f"Contact {pubkey_prefix} not found")

    # Build a safe copy of the contact with public_key guaranteed
    contact = dict(contacts[full_key])
    contact["public_key"] = full_key

    task_key = f"{config_id}:{pubkey_prefix[:12]}"
    existing = _statusreq_tasks.get(task_key)
    if existing is not None and not existing.done():
        log.info(f"[MC:{config_id}] statusreq to {pubkey_prefix} already in-flight, skipping")
        return

    log.info(f"[MC:{config_id}] statusreq scheduling background task for {pubkey_prefix[:12]}")

    async def _do_statusreq():
        try:
            log.info(f"[MC:{config_id}] statusreq background task started for {pubkey_prefix[:12]}")
            # Refresh our identity on the mesh right before the ping. Startup advert
            # can be minutes or hours old; if the repeater rebooted since then it may
            # no longer have our pubkey and won't be able to return STATUS_RESPONSE.
            advert_result = await mc.commands.send_advert(flood=True)
            if advert_result.type.name == "OK":
                log.info(f"[MC:{config_id}] statusreq pre-advert TX confirmed for {pubkey_prefix[:12]}")
            else:
                log.warning(
                    f"[MC:{config_id}] statusreq pre-advert returned {advert_result.type.name} "
                    f"before pinging {pubkey_prefix[:12]}"
                )
            # Give the far side a brief window to process the advert before we send
            # the binary status request.
            await asyncio.sleep(1.2)
            # Binary send_binary_req (opcode 0x32) — fire-and-forget send.
            # Response arrives asynchronously as BINARY_RESPONSE (0x8C) → reader dispatches
            # STATUS_RESPONSE event with tag → caught by on_status_response.
            # 0x1b legacy tested: same result (MSG_SENT but no response from ERA-2), confirming
            # the issue is ERA-2's NVS not containing EDC-3's pubkey, not the protocol.
            result = await mc.commands.send_binary_req(contact, BinaryReqType.STATUS)
            if result.type == EventType.MSG_SENT:
                log.info(f"[MC:{config_id}] statusreq to {pubkey_prefix[:12]}: MSG_SENT, awaiting async response")
            else:
                log.warning(f"[MC:{config_id}] statusreq to {pubkey_prefix[:12]}: send failed ({result.type.name})")
        except Exception as e:
            log.warning(f"[MC:{config_id}] background statusreq failed: {e}")
        finally:
            _statusreq_tasks.pop(task_key, None)

    task = _mc_bg_task(_do_statusreq(), f"statusreq:{task_key}")
    _statusreq_tasks[task_key] = task


async def _get_contacts_async(config_id):
    mc, _ = _get_mc(config_id)
    r = await mc.commands.get_contacts()
    if r.type == EventType.CONTACTS:
        with mc_connections_lock:
            if config_id in mc_connections:
                new_contacts = r.payload or {}
                old_contacts = mc_connections[config_id].get("contacts", {})
                # Don't overwrite a non-empty cache with an empty response —
                # firmware occasionally returns 0 contacts on a stale/in-progress read.
                if new_contacts or not old_contacts:
                    mc_connections[config_id]["contacts"] = _merge_mc_contacts(old_contacts, new_contacts)
    return r


# ---------------------------------------------------------------------------
# Public sync API (called from Flask routes via run_mc)
# ---------------------------------------------------------------------------

def send_chan_msg(config_id, chan_idx, text, timeout=10):
    _ensure_mc_tx_allowed("MC channel message")
    chunks = _mc_split_text_by_bytes(text, _mc_channel_msg_limit(config_id))
    result = None
    for idx, chunk in enumerate(chunks):
        result = run_mc(_send_chan_msg_async(config_id, chan_idx, chunk), timeout=timeout)
        if len(chunks) > 1 and idx < len(chunks) - 1:
            time.sleep(MC_SPLIT_SEND_DELAY_SEC)
    return result


def send_dm(config_id, pubkey_prefix, text, timeout=10):
    _ensure_mc_tx_allowed("MC direct message")
    chunks = _mc_split_text_by_bytes(text, MC_MAX_DM_MSG_BYTES)
    result = None
    for idx, chunk in enumerate(chunks):
        result = run_mc(_send_dm_async(config_id, pubkey_prefix, chunk), timeout=timeout)
        if len(chunks) > 1 and idx < len(chunks) - 1:
            time.sleep(MC_SPLIT_SEND_DELAY_SEC)
    return result


def set_path_hash_mode(config_id, preferred_mode, timeout=10):
    return run_mc(_set_path_hash_mode_async(config_id, preferred_mode), timeout=timeout)


def send_statusreq(config_id, pubkey_prefix, timeout=10):
    _ensure_mc_tx_allowed("MC status request")
    return run_mc(_send_statusreq_async(config_id, pubkey_prefix), timeout=timeout)


def send_advert(config_id, flood=False, timeout=5):
    """Fire-and-forget advert — schedules background TX and returns immediately."""
    if CONFIG.get("silent_mode"):
        log.info(f"[MC:{config_id}] Silent Running active — advert suppressed")
        return None
    return run_mc(_send_advert_async(config_id, flood=flood), timeout=timeout)


async def _send_trace_async(config_id, tag=None):
    """Broadcast trace packet — no specific target or path.
    TRACE_DATA response (0x89) is a push notification (same mechanism as ADVERTISEMENT),
    so it should work even on older firmware that doesn't support binary STATUS_RESPONSE.
    Returns the tag used so the caller can correlate with mc_trace_data SSE events."""
    import random as _random
    mc, _ = _get_mc(config_id)
    sent_tag = tag if tag is not None else _random.randint(1, 0xFFFFFFFF)
    result = await mc.commands.send_trace(tag=sent_tag)
    if result.type == EventType.MSG_SENT:
        log.info(f"[MC:{config_id}] trace sent (tag={sent_tag}), awaiting TRACE_DATA push")
        return sent_tag
    raise RuntimeError(f"trace send failed: {result.type.name}")


def send_trace_broadcast(config_id, tag=None, timeout=10):
    _ensure_mc_tx_allowed("MC trace")
    return run_mc(_send_trace_async(config_id, tag=tag), timeout=timeout)


def refresh_contacts(config_id, timeout=10):
    return run_mc(_get_contacts_async(config_id), timeout=timeout)


def set_contact_path(config_id, pubkey_prefix, hop_prefixes=None, clear=False, path_hash_mode=None, timeout=15):
    return run_mc(
        _set_contact_path_async(
            config_id,
            pubkey_prefix,
            hop_prefixes=hop_prefixes,
            clear=clear,
            path_hash_mode=path_hash_mode,
        ),
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Device info, radio params, device actions, channel management
# ---------------------------------------------------------------------------

async def _get_device_info_async(config_id):
    mc, _ = _get_mc(config_id)
    # 10s each = 20s max combined, within the 25s run_mc timeout.
    # TTLP can be slow to respond right after connect — 10s gives it breathing room.
    dev_r = await asyncio.wait_for(mc.commands.send_device_query(), timeout=10)
    bat_r = await asyncio.wait_for(mc.commands.get_bat(), timeout=10)
    dev = dev_r.payload if dev_r.type == EventType.DEVICE_INFO else {}
    bat = bat_r.payload if bat_r.type == EventType.BATTERY else {}
    return {"device": dev, "battery": bat}


async def _set_radio_async(config_id, freq, bw, sf, cr):
    mc, _ = _get_mc(config_id)
    r = await mc.commands.set_radio(float(freq), float(bw), int(sf), int(cr))
    if r.type == EventType.ERROR:
        raise RuntimeError(f"Device rejected radio params: {getattr(r, 'payload', r)}")
    return r


async def _set_tx_power_async(config_id, val):
    mc, _ = _get_mc(config_id)
    r = await mc.commands.set_tx_power(int(val))
    if r.type == EventType.ERROR:
        raise RuntimeError(f"Device rejected TX power: {getattr(r, 'payload', r)}")
    return r


async def _set_name_async(config_id, name):
    mc, _ = _get_mc(config_id)
    r = await mc.commands.set_name(name)
    if r.type == EventType.ERROR:
        raise RuntimeError(f"Device rejected name change: {getattr(r, 'payload', r)}")
    with mc_connections_lock:
        if config_id in mc_connections and mc_connections[config_id].get("node_info") is not None:
            mc_connections[config_id]["node_info"]["name"] = name
    return r


async def _set_coords_async(config_id, lat, lon):
    mc, _ = _get_mc(config_id)
    r = await mc.commands.set_coords(float(lat), float(lon))
    if r.type == EventType.ERROR:
        raise RuntimeError(f"Device rejected coords: {getattr(r, 'payload', r)}")
    # Persist to in-memory node_info so /api/mc/status returns the new coords immediately
    with mc_connections_lock:
        if config_id in mc_connections and mc_connections[config_id].get("node_info") is not None:
            mc_connections[config_id]["node_info"]["adv_lat"] = float(lat)
            mc_connections[config_id]["node_info"]["adv_lon"] = float(lon)
    push_to_sse({"type": "mc_coords_updated", "radio_id": config_id,
                 "lat": float(lat), "lon": float(lon)})
    return r


async def _reboot_async(config_id):
    mc, _ = _get_mc(config_id)
    return await mc.commands.reboot()


async def _get_channels_async(config_id, count):
    mc, _ = _get_mc(config_id)
    channels = []
    for i in range(count):
        try:
            r = await asyncio.wait_for(mc.commands.get_channel(i), timeout=5)
            if r.type == EventType.CHANNEL_INFO:
                channels.append(r.payload)
            else:
                break  # ERROR = no more channels at this index
        except asyncio.TimeoutError:
            continue  # transient timeout — skip this slot, try next
        except Exception:
            break  # connection error — abort
    return channels


async def _set_channel_async(config_id, idx, name, key_hex=None):
    mc, _ = _get_mc(config_id)
    secret = None
    if key_hex:
        try:
            secret = bytes.fromhex(key_hex)
        except ValueError:
            raise ValueError(f"Invalid key hex: must be valid hex string")
        if len(secret) != 16:
            raise ValueError(f"Key must be 16 bytes (32 hex chars), got {len(secret)}")
    r = await mc.commands.set_channel(int(idx), name, secret)
    if r.type == EventType.ERROR:
        raise RuntimeError(f"Device rejected channel set: {getattr(r, 'payload', r)}")
    return r


def _build_reachability_fallback(config_id, pubkey_prefix, full_key, phase, rx_event):
    """Convert a nearby RX_LOG_DATA event into a pragmatic ping fallback."""
    if rx_event is None:
        return None
    payload = dict(getattr(rx_event, "payload", {}) or {})
    return {
        "mode": "reachability",
        "reachable": True,
        "pubkey_pre": full_key[:12],
        "target_pubkey_pre": full_key[:12],
        "target_hint": pubkey_prefix[:12],
        "phase": phase,
        "note": (
            "Observed RF activity after ping, but firmware returned no "
            "STATUS_RESPONSE. Showing local RX observations only."
        ),
        "observed_path": payload.get("path", ""),
        "observed_path_len": payload.get("path_len"),
        "observed_path_hash_size": payload.get("path_hash_size"),
        "observed_rssi": payload.get("rssi"),
        "observed_snr": payload.get("snr"),
        "observed_payload_type": payload.get("payload_typename"),
        "observed_route_type": payload.get("route_typename"),
        "observed_recv_time": payload.get("recv_time"),
        "source": f"{phase}_rx_log",
        "radio_id": config_id,
    }


async def _req_status_async(config_id, pubkey_prefix):
    mc, _ = _get_mc(config_id)
    with mc_connections_lock:
        contacts = dict(mc_connections.get(config_id, {}).get("contacts", {}))
    full_key = next((k for k in contacts if k.startswith(pubkey_prefix)), None)
    if full_key is None:
        raise ValueError(f"Contact {pubkey_prefix} not found")
    contact = dict(contacts[full_key])   # copy — don't mutate shared mc_connections state
    contact["public_key"] = full_key
    log.info(
        f"[MC:{config_id}] req_status_sync starting for {pubkey_prefix[:12]} "
        f"(full_key={full_key[:16]}..., out_path_len={contact.get('out_path_len')}, "
        f"out_path={contact.get('out_path', '')!r}, type={contact.get('type')})"
    )
    # Flood-advertise our identity before pinging. The remote node needs our pubkey
    # in its NVS to encrypt and route STATUS_RESPONSE back. Without a recent advert,
    # multi-hop paths (e.g. EDC-3 → ERA-2 → Krvavec) will receive our ping but
    # cannot reply. Give the mesh 2s to propagate before sending the request.
    try:
        advert_result = await mc.commands.send_advert(flood=True)
        log.info(f"[MC:{config_id}] req_status pre-advert for {pubkey_prefix[:12]}: {advert_result.type.name}")
    except Exception as e:
        log.warning(f"[MC:{config_id}] req_status pre-advert failed for {pubkey_prefix[:12]}: {e}")
    await asyncio.sleep(2.0)
    # First try the legacy companion protocol status request (CMD 27). The
    # official protocol still documents it separately from binary requests, and
    # some companion firmware builds may implement only this path reliably.
    try:
        legacy_req = b"\x1b" + bytes.fromhex(full_key)
        sent = await mc.commands.send(legacy_req, [EventType.MSG_SENT, EventType.ERROR])
        log.info(
            f"[MC:{config_id}] legacy statusreq send result for {pubkey_prefix[:12]}: "
            f"{sent.type.name}"
        )
        if sent.type == EventType.MSG_SENT and mc.commands.dispatcher is not None:
            legacy_timeout = max(sent.payload.get("suggested_timeout", 4000) / 800.0, 5.0)
            legacy_rx_task = asyncio.create_task(
                mc.commands.dispatcher.wait_for_event(
                    EventType.RX_LOG_DATA,
                    timeout=legacy_timeout,
                )
            )
            legacy_event = await mc.commands.dispatcher.wait_for_event(
                EventType.STATUS_RESPONSE,
                attribute_filters={"pubkey_prefix": full_key[:12]},
                timeout=legacy_timeout,
            )
            legacy_rx = None
            try:
                legacy_rx = await legacy_rx_task
            except Exception:
                legacy_rx = None
            if legacy_event is not None:
                log.info(
                    f"[MC:{config_id}] legacy statusreq returned payload for {pubkey_prefix[:12]} "
                    f"(pubkey_pre={legacy_event.payload.get('pubkey_pre', '?')}, "
                    f"last_rssi={legacy_event.payload.get('last_rssi')}, "
                    f"last_snr={legacy_event.payload.get('last_snr')})"
                )
                return legacy_event.payload
            log.warning(f"[MC:{config_id}] legacy statusreq timed out for {pubkey_prefix[:12]}")
            if legacy_rx is not None:
                log.info(
                    f"[MC:{config_id}] legacy statusreq observed RX_LOG_DATA for {pubkey_prefix[:12]} "
                    f"(path={legacy_rx.payload.get('path', '')!r}, "
                    f"rssi={legacy_rx.payload.get('rssi')}, snr={legacy_rx.payload.get('snr')})"
                )
        elif sent.type == EventType.ERROR:
            log.warning(f"[MC:{config_id}] legacy statusreq error for {pubkey_prefix[:12]}: {sent.payload}")
    except Exception as e:
        log.warning(f"[MC:{config_id}] legacy statusreq failed for {pubkey_prefix[:12]}: {e}")

    log.info(f"[MC:{config_id}] falling back to binary req_status_sync for {pubkey_prefix[:12]}")
    # Prime routing first for repeaters/contacts that only have a known RF path
    # after a recent trace. This is the last app-side variable left: we can send
    # STATUS requests over flood, but some USB companion setups appear to need a
    # concrete contact path before the remote returns a usable status reply.
    try:
        trace_tag = int(time.time() * 1000) & 0xFFFFFFFF
        trace_res = await mc.commands.send_trace(tag=trace_tag)
        log.info(
            f"[MC:{config_id}] req_status_sync priming trace for {pubkey_prefix[:12]} "
            f"(tag={trace_tag}, result={trace_res.type.name})"
        )
    except Exception as e:
        log.warning(f"[MC:{config_id}] req_status_sync trace prime failed for {pubkey_prefix[:12]}: {e}")

    await asyncio.sleep(1.5)

    try:
        refreshed = await mc.commands.get_contacts()
        if refreshed.type == EventType.CONTACTS:
            refreshed_contacts = refreshed.payload or {}
            with mc_connections_lock:
                if config_id in mc_connections:
                    old_contacts = mc_connections[config_id].get("contacts", {})
                    if refreshed_contacts or not old_contacts:
                        refreshed_contacts = _merge_mc_contacts(old_contacts, refreshed_contacts)
                        mc_connections[config_id]["contacts"] = refreshed_contacts
                    else:
                        refreshed_contacts = old_contacts
            new_contact = dict(refreshed_contacts.get(full_key, contact))
            new_contact["public_key"] = full_key
            contact = new_contact
            log.info(
                f"[MC:{config_id}] req_status_sync after trace refresh for {pubkey_prefix[:12]} "
                f"(out_path_len={contact.get('out_path_len')}, out_path={contact.get('out_path', '')!r})"
            )
    except Exception as e:
        log.warning(f"[MC:{config_id}] req_status_sync contact refresh failed for {pubkey_prefix[:12]}: {e}")

    rx_task = None
    if mc.commands.dispatcher is not None:
        rx_task = asyncio.create_task(
            mc.commands.dispatcher.wait_for_event(
                EventType.RX_LOG_DATA,
                timeout=8.0,
            )
        )
    result = await mc.commands.req_status_sync(contact)
    rx_event = None
    if rx_task is not None:
        try:
            rx_event = await rx_task
        except Exception:
            rx_event = None
    if result is None:
        log.warning(f"[MC:{config_id}] req_status_sync returned None for {pubkey_prefix[:12]}")
        fallback = _build_reachability_fallback(
            config_id, pubkey_prefix, full_key, "binary", rx_event
        )
        if fallback is not None:
            log.info(
                f"[MC:{config_id}] reachability fallback for {pubkey_prefix[:12]} "
                f"(path={fallback.get('observed_path')!r}, "
                f"rssi={fallback.get('observed_rssi')}, snr={fallback.get('observed_snr')})"
            )
            return fallback
    else:
        log.info(
            f"[MC:{config_id}] req_status_sync returned payload for {pubkey_prefix[:12]} "
            f"(pubkey_pre={result.get('pubkey_pre', '?')}, "
            f"last_rssi={result.get('last_rssi')}, last_snr={result.get('last_snr')})"
        )
    return result


async def _get_stats_async(config_id):
    mc, _ = _get_mc(config_id)
    # 8s each = 24s max combined — fits within the 30s run_mc timeout
    core_r   = await asyncio.wait_for(mc.commands.get_stats_core(),    timeout=8)
    radio_r  = await asyncio.wait_for(mc.commands.get_stats_radio(),   timeout=8)
    pkts_r   = await asyncio.wait_for(mc.commands.get_stats_packets(), timeout=8)
    core  = core_r.payload  if core_r.type  == EventType.STATS_CORE    else {}
    radio = radio_r.payload if radio_r.type == EventType.STATS_RADIO   else {}
    pkts  = pkts_r.payload  if pkts_r.type  == EventType.STATS_PACKETS else {}
    return {"core": core, "radio": radio, "packets": pkts}


# Public sync API
def get_device_info(config_id, timeout=25):
    return run_mc(_get_device_info_async(config_id), timeout=timeout)


def set_radio_params(config_id, freq, bw, sf, cr, timeout=10):
    return run_mc(_set_radio_async(config_id, freq, bw, sf, cr), timeout=timeout)


def set_tx_power(config_id, val, timeout=10):
    return run_mc(_set_tx_power_async(config_id, val), timeout=timeout)


def set_device_name(config_id, name, timeout=10):
    return run_mc(_set_name_async(config_id, name), timeout=timeout)


def set_device_coords(config_id, lat, lon, timeout=10):
    return run_mc(_set_coords_async(config_id, lat, lon), timeout=timeout)


def reboot_device(config_id, timeout=10):
    return run_mc(_reboot_async(config_id), timeout=timeout)


def req_node_status(config_id, pubkey_prefix, timeout=30):
    _ensure_mc_tx_allowed("MC status request")
    return run_mc(_req_status_async(config_id, pubkey_prefix), timeout=timeout)


def get_stats(config_id, timeout=30):
    return run_mc(_get_stats_async(config_id), timeout=timeout)


def reboot_device_dtr(config_id):
    """Hardware reset via RTS toggle (ESP32 EN pin). Disconnects MC first, reconnect loop picks it back up."""
    import serial as _serial
    with mc_connections_lock:
        state   = mc_connections.get(config_id, {})
        port    = state.get("port") or state.get("config", {}).get("port")
        mc_obj  = state.get("mc")
    if not port:
        raise RuntimeError("No port configured for this MC node")
    # Disconnect MC gracefully to free the serial port
    if mc_obj:
        try:
            run_mc(mc_obj.disconnect(), timeout=5)
        except Exception:
            pass
    # Mark disconnected explicitly — on_disconnected may not fire on graceful disconnect,
    # so the reconnect loop would never re-trigger without this.
    with mc_connections_lock:
        if config_id in mc_connections:
            mc_connections[config_id]["status"] = "disconnected"
            mc_connections[config_id]["mc"]     = None
    time.sleep(0.3)
    # Toggle RTS — pulls ESP32 EN low → reset
    try:
        s = _serial.Serial(port, timeout=1)
        s.setRTS(True)
        time.sleep(0.2)
        s.setRTS(False)
        s.close()
    except Exception as e:
        raise RuntimeError(f"RTS reset failed on {port}: {e}")


def disconnect_all_mc():
    """Gracefully disconnect all connected MC nodes. Call before process exit."""
    with mc_connections_lock:
        items = [(cid, v.get("mc")) for cid, v in mc_connections.items() if v.get("mc")]
    for config_id, mc_obj in items:
        try:
            run_mc(mc_obj.disconnect(), timeout=5)
            log.info(f"[MC:{config_id}] disconnected cleanly on shutdown")
        except Exception as e:
            log.warning(f"[MC:{config_id}] disconnect on shutdown failed: {e}")


def get_channels(config_id, count=8, timeout=30):
    return run_mc(_get_channels_async(config_id, count), timeout=timeout)


def set_channel(config_id, idx, name, key_hex=None, timeout=10):
    return run_mc(_set_channel_async(config_id, idx, name, key_hex), timeout=timeout)
