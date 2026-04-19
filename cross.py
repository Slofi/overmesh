import json
import logging
import re
import threading
import time
import uuid

from config import CONFIG, CONFIG_LOCK
from db import save_message
from helpers import _next_msg_id, push_to_sse
from state import chat_lock, chat_messages, mc_connections, mc_connections_lock

log = logging.getLogger(__name__)

CROSS_SYSTEM_ENABLED = False

_RELAY_TAG_RE = re.compile(r'^\[(MT|MC)->(MT|MC)\b', re.IGNORECASE)
_RECENT_RELAY_WINDOW = 45
_recent_relays = {}
_recent_relays_lock = threading.Lock()

CROSS_DEFAULTS = {
    "rules": [],
}

RULE_DEFAULTS = {
    "id": "",
    "enabled": True,
    "mode": "mirror",
    "source_network": "mt",
    "source_radio_id": "",
    "source_channel": 0,
    "target_radio_id": "",
    "target_channel": 0,
    "bidirectional": False,
    "sender_filter_mode": "any",
    "sender_filters": "",
}


def _normalize_rule(raw):
    rule = dict(RULE_DEFAULTS)
    rule.update(raw or {})
    rule["id"] = str(rule.get("id") or f"cross_{uuid.uuid4().hex[:10]}")
    try:
        rule["source_channel"] = max(0, min(7, int(rule.get("source_channel", 0))))
    except (TypeError, ValueError):
        rule["source_channel"] = 0
    try:
        rule["target_channel"] = max(0, min(7, int(rule.get("target_channel", 0))))
    except (TypeError, ValueError):
        rule["target_channel"] = 0
    rule["mode"] = "bridge" if rule.get("mode") == "bridge" else "mirror"
    rule["enabled"] = bool(rule.get("enabled", True))
    rule["bidirectional"] = (rule["mode"] == "bridge")
    rule["source_network"] = "mc" if rule.get("source_network") == "mc" else "mt"
    if rule.get("sender_filter_mode") not in ("any", "allow", "block"):
        rule["sender_filter_mode"] = "any"
    rule["source_radio_id"] = str(rule.get("source_radio_id") or "")
    rule["target_radio_id"] = str(rule.get("target_radio_id") or "")
    rule["sender_filters"] = str(rule.get("sender_filters") or "")
    return rule


def get_cross_config():
    with CONFIG_LOCK:
        raw = CONFIG.get("cross") or {}
    cfg = dict(CROSS_DEFAULTS)
    if isinstance(raw, dict) and isinstance(raw.get("rules"), list):
        cfg["rules"] = [_normalize_rule(r) for r in raw.get("rules", [])]
    elif isinstance(raw, dict) and ("source_network" in raw or "source_radio_id" in raw):
        # Backward compatibility: old single-rule config
        cfg["rules"] = [_normalize_rule(raw)]
    else:
        cfg["rules"] = []
    return cfg


def get_cross_rules():
    if not CROSS_SYSTEM_ENABLED:
        return []
    return [r for r in get_cross_config().get("rules", []) if r.get("enabled")]


def _filter_tokens(cfg):
    return {x.strip().lower() for x in (cfg.get("sender_filters") or "").split(",") if x.strip()}


def _relay_tagged(text):
    return bool(_RELAY_TAG_RE.match((text or "").strip()))


def _dedupe_key(net, radio_id, channel, sender_id, text):
    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    return f"{net}|{radio_id}|{channel}|{sender_id}|{norm}"


def _seen_recently(key):
    now = time.time()
    with _recent_relays_lock:
        stale = [k for k, ts in _recent_relays.items() if now - ts > _RECENT_RELAY_WINDOW]
        for k in stale:
            _recent_relays.pop(k, None)
        prev = _recent_relays.get(key)
        if prev and now - prev <= _RECENT_RELAY_WINDOW:
            return True
        _recent_relays[key] = now
        return False


def _mc_contact_name(radio_id, pre):
    if not pre:
        return "?"
    with mc_connections_lock:
        contacts = dict((mc_connections.get(radio_id) or {}).get("contacts", {}))
    if pre in contacts:
        c = contacts[pre]
        return c.get("adv_name") or pre[:8]
    for pubkey, c in contacts.items():
        if pubkey.startswith(pre):
            return c.get("adv_name") or pre[:8]
    return pre[:8]


def _mc_radio_name(radio_id):
    with mc_connections_lock:
        state = mc_connections.get(radio_id) or {}
    return state.get("config", {}).get("name") or radio_id


def _sender_allowed(cfg, sender_id, sender_name):
    mode = cfg.get("sender_filter_mode", "any")
    if mode == "any":
        return True
    tokens = _filter_tokens(cfg)
    if not tokens:
        return True
    candidates = {str(sender_id or "").lower(), str(sender_name or "").lower()}
    match = any(c and c in tokens for c in candidates)
    return match if mode == "allow" else not match


def _format_forward_text(src_net, dst_net, sender_name, channel, text):
    sender = (sender_name or "?").strip()[:64]
    body = (text or "").strip()
    return f"[{src_net.upper()}->{dst_net.upper()} {sender} / CH{channel}] {body}"


def _matches_source(rule, net, radio_id, channel):
    return (
        rule.get("source_network") == net
        and str(rule.get("source_radio_id") or "") == str(radio_id or "")
        and int(rule.get("source_channel", 0)) == int(channel or 0)
    )


def _matches_reverse(rule, net, radio_id, channel):
    if not rule.get("bidirectional"):
        return False
    reverse_source = "mc" if rule.get("source_network") == "mt" else "mt"
    return (
        reverse_source == net
        and str(rule.get("target_radio_id") or "") == str(radio_id or "")
        and int(rule.get("target_channel", 0)) == int(channel or 0)
    )


def _rule_target(rule, incoming_net, radio_id, channel):
    if _matches_source(rule, incoming_net, radio_id, channel):
        target_net = "mc" if rule.get("source_network") == "mt" else "mt"
        return target_net, str(rule.get("target_radio_id") or ""), int(rule.get("target_channel", 0))
    if _matches_reverse(rule, incoming_net, radio_id, channel):
        target_net = rule.get("source_network")
        return target_net, str(rule.get("source_radio_id") or ""), int(rule.get("source_channel", 0))
    return None, "", 0


def _send_mt_forward(radio_id, channel, text):
    from mesh import get_iface_by_radio
    iface = get_iface_by_radio(radio_id)
    if not iface:
        raise RuntimeError("Target MT radio not connected")
    iface.sendText(text, channelIndex=channel, wantAck=True)
    msg = {
        "id": _next_msg_id(),
        "from_id": "bridge",
        "from_name": "Bridge",
        "to_id": None,
        "to_name": "All",
        "channel": channel,
        "text": text,
        "ts": int(time.time()),
        "sent": True,
        "is_dm": False,
        "status": "delivered",
        "radio_id": radio_id,
    }
    with chat_lock:
        chat_messages.append(msg)
        if len(chat_messages) > 500:
            chat_messages.pop(0)
    save_message(msg)
    push_to_sse(json.dumps(msg))
    log.info(f"Cross forward delivered: MC->MT target={radio_id} ch={channel}")


def _send_mc_forward(radio_id, channel, text):
    from mesh_mc import send_chan_msg
    send_chan_msg(radio_id, channel, text)
    push_to_sse({
        "type": "mc_message",
        "id": f"bridge-{int(time.time() * 1000)}",
        "radio_id": radio_id,
        "radio_name": _mc_radio_name(radio_id),
        "network": "mc",
        "subtype": "channel",
        "channel": channel,
        "from_id": "bridge",
        "text": text,
        "ts": int(time.time()),
        "sent": True,
        "status": "delivered",
    })
    log.info(f"Cross forward delivered: MT->MC target={radio_id} ch={channel}")


def maybe_forward_mt_message(msg):
    try:
        if CONFIG.get("silent_mode"):
            return
        rules = get_cross_rules()
        if not rules:
            return
        if msg.get("is_dm"):
            return
        text = (msg.get("text") or "").strip()
        if not text or _relay_tagged(text):
            return
        radio_id = msg.get("radio_id")
        channel = int(msg.get("channel", 0))
        dedupe_key = _dedupe_key("mt", radio_id, channel, msg.get("from_id"), text)
        if _seen_recently(dedupe_key):
            return
        sent_targets = set()
        for rule in rules:
            target_net, target_radio_id, target_channel = _rule_target(rule, "mt", radio_id, channel)
            if target_net != "mc" or not target_radio_id:
                continue
            if not _sender_allowed(rule, msg.get("from_id"), msg.get("from_name")):
                continue
            target_key = (target_net, target_radio_id, int(target_channel))
            if target_key in sent_targets:
                continue
            forwarded = _format_forward_text("mt", "mc", msg.get("from_name"), channel, text)
            _send_mc_forward(target_radio_id, target_channel, forwarded)
            log.info(
                f"Cross rule {rule.get('id')} matched: MT {radio_id} ch={channel} -> "
                f"MC {target_radio_id} ch={target_channel}"
            )
            sent_targets.add(target_key)
    except Exception as e:
        log.warning(f"Cross MT->MC forward failed: {e}")


def maybe_forward_mc_message(msg):
    try:
        if CONFIG.get("silent_mode"):
            return
        rules = get_cross_rules()
        if not rules:
            return
        if msg.get("subtype") != "channel":
            return
        text = (msg.get("text") or "").strip()
        if not text or _relay_tagged(text):
            return
        radio_id = msg.get("radio_id")
        channel = int(msg.get("channel", 0))
        sender_id = msg.get("from_id")
        sender_name = msg.get("from_name") or _mc_contact_name(radio_id, sender_id)
        dedupe_key = _dedupe_key("mc", radio_id, channel, sender_id, text)
        if _seen_recently(dedupe_key):
            return
        sent_targets = set()
        for rule in rules:
            target_net, target_radio_id, target_channel = _rule_target(rule, "mc", radio_id, channel)
            if target_net != "mt" or not target_radio_id:
                continue
            if not _sender_allowed(rule, sender_id, sender_name):
                continue
            target_key = (target_net, target_radio_id, int(target_channel))
            if target_key in sent_targets:
                continue
            forwarded = _format_forward_text("mc", "mt", sender_name, channel, text)
            _send_mt_forward(target_radio_id, target_channel, forwarded)
            log.info(
                f"Cross rule {rule.get('id')} matched: MC {radio_id} ch={channel} -> "
                f"MT {target_radio_id} ch={target_channel}"
            )
            sent_targets.add(target_key)
    except Exception as e:
        log.warning(f"Cross MC->MT forward failed: {e}")
