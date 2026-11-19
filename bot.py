import json
import logging
import math
import os
import random
import re
import tempfile
import threading
import time
from datetime import datetime

from config import CONFIG, DATA_DIR
from db import save_message, save_mc_message
from helpers import (
    _next_msg_id, push_to_sse,
    _radio_id_for_iface,
    get_node_name, get_node_short_name,
    get_node_data,
)
from state import (
    connections, connections_lock,
    chat_messages, chat_lock,
    bot_activity, bot_activity_lock,
    _bot_config_cache, _bot_config_cache_lock,
    _motd_last_sent_per_radio, _motd_event,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BOT_CONFIG_PATH = os.path.join(DATA_DIR, "bot_config.json")

BOT_CONFIG_DEFAULTS = {
    "enabled": False,
    "bot_label": "OM Bot Ljubljana",
    "listen_channels": [0],
    "commands": {
        "ping":   {"enabled": True,  "respond_via": "same"},
        "ack":    {"enabled": True,  "respond_via": "same"},
        "test":   {"enabled": True,  "respond_via": "same"},
        "sitrep": {"enabled": True,  "respond_via": "same"},
        "cmd":    {"enabled": True,  "respond_via": "same"},
        "motd":   {"enabled": False, "respond_via": "same"},
        "joke":   {"enabled": True,  "respond_via": "same"},
        "dot":    {"enabled": True,  "respond_via": "same"},
        "relay":  {"enabled": True,  "respond_via": "dm", "relay_mode": "dm", "relay_channel": 0},
    },
    "motd": {
        "enabled": False,
        "mode": "interval",
        "interval_hours": 4,
        "fixed_time": "08:00",
        "message": "",
        "channels": [],
    },
}

BOT_JOKES = [
    "Why do ham radio operators make great detectives? They always find the source.",
    "I tried to make a joke about LoRa... but it got lost in transmission.",
    "What do you call a Meshtastic node with no friends? A standalone router.",
    "Why did the antenna go to therapy? Too many bad connections.",
    "What's a ham operator's favorite exercise? Running a net.",
    "Why don't mesh nodes play poker? They always broadcast their hand.",
    "How do ham radio operators say goodbye? '73', because 'later' wastes bandwidth.",
    "What do you call a broken SMA connector? An ex-SMA.",
    "Why was the SDR dongle always happy? Outstanding reception.",
    "What did the capacitor say to the battery? 'You charge me up.'",
    "Why did the Meshtastic node cross the road? To extend the mesh coverage.",
    "What do you call a GPS with attitude? A sassy-tellite.",
    "Why did the RF engineer stay calm at the party? Good signal management.",
    "What's a ham operator's least favorite game? Frequency freeze. Nobody moves.",
    "What do you call a LoRa module that tells stories? A long-range narrator.",
    "Why do antennas make bad dancers? They always stick to one frequency.",
    "What did the oscilloscope say at the party? 'I see exactly what you're putting out.'",
    "Why did the mesh node apply for a promotion? Wanted to improve its hops.",
    "What do you call a ham operator who can't find a clear frequency? Static Phil.",
    "Why did the T-Beam go to the gym? To build better range.",
    "Why do Meshtastic nodes make bad gossips? They repeat everything to everyone.",
    "What did one resistor say to the other? 'You're too Ohm-work.'",
    "Why did the dipole break up with the Yagi? Too directional.",
    "What do you call a mesh network party? Hop till you drop.",
    "Why did the ham operator fail his driving test? Kept calling CQ at stop signs.",
    "What's an SDR's favorite music genre? Heavy metal. Maximum bandwidth.",
    "Why don't LoRa nodes hurry? Built for range, not speed.",
    "What did the power supply say to the MCU? 'I've got you covered.'",
    "Why did the Heltec module overheat? Too many hot takes.",
    "What do you call a Meshtastic node on a mountaintop? A peak performer.",
    "Why did the spectrum analyzer go to the party? To see what was on the air.",
    "What's a ham operator's favorite book? 'War and Frequencies.'",
    "Why did the node change its long name? Too many traceroutes. It needed privacy.",
    "What do you call two ham operators chatting? A QSO. Three? A net. A hundred? A pile-up.",
    "Why was the mesh router always tired? Hopping all night.",
    "What did the soldering iron say? 'Let me make the connection.'",
    "Why do mesh nodes never get lonely? Always surrounded by peers.",
    "What's a Raspberry Pi's biggest fear? SD card corruption at 3 AM.",
    "Why did the GPS fail in the forest? Too many trees. Nature's Faraday cage.",
    "What do you call a Meshtastic node in airplane mode? A very sad brick.",
    "Why did the battery only last two days? Too many overhead packets.",
    "Why did the repeater go to school? To improve its output.",
    "What do you call a node that only listens? An SWL with Meshtastic hardware.",
    "What did the multimeter say to the broken circuit? 'You've got no resistance left.'",
    "Why do ham operators love camping? Best antenna heights, no neighbors to complain.",
    "What's a mesh network's worst nightmare? A partition. Nobody talks to anyone.",
    "Why did the packet arrive late? TTL issues and construction on the RF path.",
    "What do you call a Meshtastic node in space? Very out of range.",
    "What's a ham operator's favorite chord? The 40-meter dipole.",
    "Why did the node broadcast its GPS? It had nothing to hide. Or forgot to check the config.",
    "What's LoRa's dating profile? 'Long range, slow but steady. Low maintenance.'",
    "Why did the Yagi go on a diet? Too much gain.",
    "What do you call a mesh network in a storm? A very committed flood fill.",
    "Why don't ham operators get lost? They always know their heading and their frequency.",
    "What did the inductor say to the capacitor? 'You complete my circuit.'",
    "Why did the Faraday cage get no texts? It blocked everything.",
    "What do you call a node that keeps dropping packets? An influencer.",
    "Why was the battery always the center of attention? Everyone depended on it.",
    "Why did the mesh network win the race? Every node routed for each other.",
    "What do you call a signal that arrives perfectly? A miracle. Probably line of sight.",
    "Why do Meshtastic nodes make bad politicians? They relay every message, never filter.",
    "What's a ham operator's favorite movie? 'The Silence of the Bands.'",
    "Why did the LiPo battery go to court? It swelled up and became a problem.",
    "What do you call a ham operator at the beach? A saltwater antenna tester.",
    "Why did the mesh node get a promotion? Best SNR in the network.",
    "What do you call a node with 0% battery? A doorstop.",
    "Why did the coax break up with the antenna? Bad match. Too much standing wave.",
    "What's the difference between a ham operator and a pizza? The pizza can feed a family of four.",
    "Why did the SDR crash? Too many tabs open. Even software has limits.",
    "What do you call a Meshtastic node that's always right? A GPS-enabled contrarian.",
    "Why did the packet take the long route? Hop-timism.",
    "What's an antenna's least favorite weather? Ice storms. Explains itself.",
    "Why did the ham operator stay up all night? DX conditions were perfect and sleep is overrated.",
    "What do you call a radio channel with no traffic? Peaceful. Briefly.",
    "Why was the BMS always cranky? Constantly under pressure.",
    "What do you call a ham station with too many radios? A shack. Obviously.",
    "Why did the Meshtastic firmware update fail? Didn't read the changelog.",
    "What's a node's favorite snack? Packets. Obviously.",
    "Why did the ham operator move to the mountains? Lower noise floor, no HOA antenna restrictions.",
    "What do you call a mesh node that never sleeps? Power-hungry.",
    "Why did the coax go to the gym? To reduce its loss.",
    "What do you call a ham operator who talks too much? An AM station. Full carrier, constant output.",
    "Why did the GPS receiver need glasses? Poor satellite geometry.",
    "What's a LoRa module's life motto? 'Slow and steady wins the range.'",
    "Why did the mesh node refuse to be a repeater? Didn't want the extra hops.",
    "What do you call a node with perfect timing? Crystal-controlled.",
    "Why did the ham operator cry? QRM on his favorite frequency. Again.",
    "Why do Meshtastic nodes make the best friends? They relay your message, never judge your SNR, and never drop you, unless the battery dies.",
    "Why did the SMA connector go to therapy? Attachment issues.",
    "What do you call a ham operator who only talks about equipment? An Elmer at full power.",
    "Why did the RF shield win the award? It kept everything in check.",
    "What do you call a band plan meeting? A spectrum summit.",
    "What do you call a node that arrived before anyone else? First-hop advantage.",
    "Why did the mesh node ignore the ping? It was on do-not-disturb.",
    "What do you call a ham operator with no antenna? An SWL waiting to happen.",
    "Why did the APRS beacon always seem happy? It always knew exactly where it stood.",
    "What do you call a ham operator who never logs contacts? A mystery. Much like their antenna.",
    "Why did the packet get rejected? Wrong channel. Story of its life.",
    "Why don't Faraday cages make good friends? They keep everything to themselves.",
    "What did one mesh node say to another? 'I've got you covered, literally, with RF.'",
    "Why did the LoRa chip win the marathon? Lowest power, longest range. Story of its life.",
]

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

_RADIO_ID_RE = re.compile(r'^[a-zA-Z0-9_\-]{1,64}$')


def _bot_config_path(radio_id):
    if not radio_id:
        return BOT_CONFIG_PATH
    if not _RADIO_ID_RE.match(str(radio_id)):
        return BOT_CONFIG_PATH
    return os.path.join(DATA_DIR, f"bot_config_{radio_id}.json")


def load_bot_config(radio_id=None):
    with _bot_config_cache_lock:
        if radio_id in _bot_config_cache:
            return json.loads(json.dumps(_bot_config_cache[radio_id]))
        path = _bot_config_path(radio_id)
        try:
            with open(path) as f:
                cfg = json.load(f)
        except FileNotFoundError:
            if radio_id and os.path.exists(BOT_CONFIG_PATH):
                try:
                    with open(BOT_CONFIG_PATH) as f:
                        cfg = json.load(f)
                except Exception:
                    cfg = {}
            else:
                cfg = {}
        except (json.JSONDecodeError, Exception) as e:
            log.warning(f"bot_config load error ({e}), falling back to defaults")
            cfg = {}
        merged = json.loads(json.dumps(BOT_CONFIG_DEFAULTS))
        merged.update({k: v for k, v in cfg.items() if k not in ("commands", "motd")})
        motd_defaults = json.loads(json.dumps(BOT_CONFIG_DEFAULTS["motd"]))
        motd_defaults.update(cfg.get("motd", {}) if isinstance(cfg.get("motd", {}), dict) else {})
        merged["motd"] = motd_defaults
        for cmd, defaults in BOT_CONFIG_DEFAULTS["commands"].items():
            if cmd not in cfg.get("commands", {}):
                merged["commands"][cmd] = json.loads(json.dumps(defaults))
            else:
                d = json.loads(json.dumps(defaults))
                d.update(cfg["commands"][cmd])
                merged["commands"][cmd] = d
        _bot_config_cache[radio_id] = merged
        return json.loads(json.dumps(merged))


def save_bot_config(cfg, radio_id=None):
    path = _bot_config_path(radio_id)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=DATA_DIR, delete=False, suffix=".tmp") as tf:
            json.dump(cfg, tf, indent=2)
            tmp = tf.name
        os.replace(tmp, path)
        with _bot_config_cache_lock:
            _bot_config_cache[radio_id] = json.loads(json.dumps(cfg))
    except Exception as e:
        log.error(f"save_bot_config failed: {e}")
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def motd_target_channels(cfg):
    motd_cfg = cfg.get("motd", {}) if isinstance(cfg.get("motd", {}), dict) else {}
    channels = motd_cfg.get("channels")
    if isinstance(channels, list):
        cleaned = []
        for ch in channels:
            try:
                cleaned.append(int(ch))
            except (TypeError, ValueError):
                continue
        if cleaned:
            return cleaned
    listen = cfg.get("listen_channels", [0]) or [0]
    return [int(ch) for ch in listen]

# ---------------------------------------------------------------------------
# Activity log + SSE
# ---------------------------------------------------------------------------

def log_bot_activity(from_name, command, response, channel):
    entry = {
        "ts":        int(time.time()),
        "from_name": from_name,
        "command":   command,
        "response":  response,
        "channel":   channel,
    }
    with bot_activity_lock:
        bot_activity.append(entry)
        if len(bot_activity) > 100:
            bot_activity.pop(0)
    push_to_sse(json.dumps({"type": "bot_activity", "entry": entry}))


def push_bot_chat_message(text, channel, radio_id, dest_id=None, to_name=None):
    """Surface a bot reply in the chat tab (same SSE stream as regular messages)."""
    msg = {
        "id":        _next_msg_id(),
        "from_id":   "bot",
        "from_name": "Bot",
        "to_id":     dest_id,
        "to_name":   to_name,
        "channel":   channel,
        "text":      text,
        "ts":        int(time.time()),
        "sent":      False,
        "is_dm":     bool(dest_id),
        "status":    "delivered",
        "radio_id":  radio_id,
    }
    with chat_lock:
        chat_messages.append(msg)
        if len(chat_messages) > 500:
            chat_messages.pop(0)
    save_message(msg)
    push_to_sse(json.dumps(msg))

# ---------------------------------------------------------------------------
# Sitrep + MOTD builders
# ---------------------------------------------------------------------------

def build_sitrep():
    nodes  = get_node_data()
    remote = [n for n in nodes if not n.get("is_local") and n.get("id")]
    total  = len(remote)

    def sname(n):
        return n.get("short_name") or n.get("long_name") or n.get("id", "?")

    if not remote:
        return "👀 In Mesh: 0"

    recent = sorted(remote, key=lambda n: n.get("last_heard_ts", 0), reverse=True)
    seen_lines = "; ".join(
        f"{sname(n)}, {n['last_heard']}" for n in recent[:3] if n.get("last_heard")
    )

    cutoff     = int(time.time()) - 900
    online     = sum(1 for n in remote if (n.get("last_heard_ts") or 0) > cutoff)
    local_node = next((n for n in nodes if n.get("is_local")), None)
    air_util   = local_node.get("air_util") if local_node else None

    parts = []
    if seen_lines:
        parts.append(f"Last Seen:\n{seen_lines}")
    summary = f"👀 In Mesh: {total} | 🟢 Online: {online}"
    if air_util is not None:
        summary += f" | 📶 Air: {air_util:.1f}%"
    parts.append(summary)
    return "\n\n".join(parts)


def build_motd_text(cfg):
    nodes  = get_node_data()
    remote = [n for n in nodes if not n.get("is_local") and n.get("id")]
    cutoff = int(time.time()) - 900
    online = sum(1 for n in remote if (n.get("last_heard_ts") or 0) > cutoff)
    custom = cfg["motd"].get("message", "")
    return f"Bot active | {online} nodes online" + (f" | {custom}" if custom else "")

# ---------------------------------------------------------------------------
# Send helpers
# ---------------------------------------------------------------------------

def send_bot_response(iface, text, channel_index, dest_id=None):
    if CONFIG.get("silent_mode"):
        log.info("[bot] Silent Running active — MT bot response suppressed")
        return False
    try:
        if dest_id:
            iface.sendText(text, destinationId=dest_id, channelIndex=channel_index, wantAck=True)
        else:
            iface.sendText(text, channelIndex=channel_index, wantAck=True)
        return True
    except Exception as e:
        log.warning(f"Bot send error: {e}")
        return False


def send_and_record_bot_response(iface, text, channel_index, radio_id, dest_id=None, to_name=None):
    if send_bot_response(iface, text, channel_index, dest_id):
        push_bot_chat_message(text, channel_index, radio_id, dest_id, to_name)

# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------

def handle_bot_command(packet, interface):
    try:
        radio_id = _radio_id_for_iface(interface)
        cfg = load_bot_config(radio_id)
        if not cfg.get("enabled"):
            return
        # Silent Running: the command is ignored on purpose and it is LOGGED. A suppression nobody can see is
        # indistinguishable from a broken bot — Filip proved the silence live and asked for the line (2026-10-16).
        if CONFIG.get("silent_mode"):
            log.info(f"[bot] Silent Running active — MT bot command from {packet.get('fromId', '?')} ignored, no reply sent")
            return

        prefix  = f"[{cfg.get('bot_label', 'OM Bot')}]"
        decoded = packet.get("decoded", {})
        if decoded.get("portnum") != "TEXT_MESSAGE_APP":
            return

        from_id   = packet.get("fromId", "")
        to_id     = packet.get("toId", "^all")
        channel   = packet.get("channel", 0)
        text      = (decoded.get("text") or "").strip()
        is_dm     = (to_id != "^all")

        if not is_dm and channel not in cfg.get("listen_channels", [0]):
            return
        if not text:
            return

        from_name = get_node_name(from_id)

        # --- Multi-word: relay <TARGET> <message> ---
        if text.lower().startswith("relay "):
            if not cfg["commands"].get("relay", {}).get("enabled"):
                return
            parts = text.split(" ", 2)
            if len(parts) < 3:
                response = f"{prefix} Usage: relay <SHORT_NAME> <message>"
            else:
                target_name = parts[1]
                relay_msg   = parts[2]
                target_id   = None
                with connections_lock:
                    for state in connections.values():
                        iface_r = state.get("iface")
                        if iface_r and iface_r.nodes:
                            for node in iface_r.nodes.values():
                                user_n = node.get("user", {})
                                if user_n.get("shortName", "").lower() == target_name.lower():
                                    target_id = user_n.get("id")
                                    break
                        if target_id:
                            break
                if not target_id:
                    response = f"{prefix} Node '{target_name}' not found on mesh."
                else:
                    relay_cfg  = cfg["commands"].get("relay", {})
                    relay_mode = relay_cfg.get("relay_mode", "dm")
                    relay_ch   = int(relay_cfg.get("relay_channel", 0))
                    from_short = get_node_short_name(from_id)
                    relay_text = f"[{cfg.get('bot_label', 'OM Bot')} relay from {from_short}]: {relay_msg}"
                    log.info(f"Relay: {from_id} → {target_id} ({target_name}) mode={relay_mode} ch={channel}: {relay_msg!r}")
                    try:
                        if relay_mode == "broadcast":
                            interface.sendText(relay_text, channelIndex=relay_ch)
                        else:
                            interface.sendText(relay_text, destinationId=target_id, channelIndex=relay_ch)
                        response = f"{prefix} Relayed to {target_name}."
                        log_bot_activity(from_name, "relay", f"→ {target_name}: {relay_msg}", channel)
                    except Exception as e:
                        response = f"{prefix} Failed to relay to {target_name}: {e}"
                        log_bot_activity(from_name, "relay", f"FAILED → {target_name}: {e}", channel)
                    threading.Thread(
                        target=send_bot_response,
                        args=(interface, response, channel, from_id),
                        daemon=True,
                    ).start()
                    return
            threading.Thread(
                target=send_bot_response,
                args=(interface, response, channel, from_id),
                daemon=True,
            ).start()
            log_bot_activity(from_name, "relay", response, channel)
            return

        # --- Single-word commands ---
        cmd_key = "dot" if text == "." else text.lower().split()[0].rstrip("!?.,;")
        if not cmd_key:
            return

        cmd_cfg = cfg["commands"].get(cmd_key)
        if not cmd_cfg or not cmd_cfg.get("enabled"):
            return

        cmd    = cmd_key
        snr    = packet.get("rxSnr")
        rssi   = packet.get("rxRssi")
        snr_s  = f"SNR:{snr:.1f}" if snr is not None else "SNR:?"
        rs_s   = f" RSSI:{rssi}" if rssi is not None else ""
        rf_info = f"[RF] {snr_s}{rs_s}"

        if cmd == "ping":
            response = f"🏓PONG | {random.choice(['Still here, unfortunately.', 'Not dead yet.', 'Present and accounted for.', 'You rang?', 'Did someone say ping?', 'Responding as trained.', 'Loud and proud.', 'Alive and well.', 'Mesh is alive!', 'Roger that, I exist.', 'Beep boop, I am a bot.', 'Oh, you noticed me!', 'Here!', 'Indeed.', 'Obviously.'])} {rf_info}"
        elif cmd == "ack":
            response = random.choice([f"✋Ack to you! {rf_info}", f"✋Copy that! {rf_info}", f"✋Acknowledged {rf_info}", f"✋Received! {rf_info}", f"✋Loud and clear {rf_info}", f"✋Message received, filing it away {rf_info}", f"✋Got it, doing nothing about it {rf_info}", f"✋Confirmed. Mostly. {rf_info}", f"✋10-4, good buddy {rf_info}", f"✋Wilco {rf_info}"])
        elif cmd == "test":
            response = random.choice([f"🎙Roger that! {rf_info}", f"🎙Testing 1,2,3 {rf_info}", f"🎙Testing, testing {rf_info}", f"🎙Read you loud and clear {rf_info}", f"🎙Signal received {rf_info}", f"🎙Loud and clear {rf_info}", f"🎙You are coming through loud and hot {rf_info}", f"🎙Heard you the first time {rf_info}", f"🎙Five by five {rf_info}", f"🎙Is this thing on? Yes, yes it is {rf_info}", f"🎙Transmission received, sanity intact {rf_info}", f"🎙Strength 5, readability 5 {rf_info}", f"🎙Clear as a bell {rf_info}", f"🎙Your signal is better than my day {rf_info}", f"🎙Mesh works, miracles do happen {rf_info}"])
        elif cmd == "sitrep":
            response = build_sitrep()
        elif cmd == "cmd":
            enabled = sorted([c for c, v in cfg["commands"].items() if v.get("enabled") and c != "dot"])
            parts   = [c + (' (relay "NAME" msg)' if c == "relay" else "") for c in enabled]
            response = "Commands: " + " | ".join(parts)
        elif cmd == "motd":
            response = build_motd_text(cfg)
        elif cmd == "joke":
            response = random.choice(BOT_JOKES)
        elif cmd == "dot":
            response = random.choice(["👀 Yes, I can see your . there ;)", "👀 Ah, a lone dot. Minimalist.", "👀 Received one (1) dot. Processing...", "👀 A dot! The mesh carried that with dignity.", "👀 Your . arrived safely. Was it worth it? ;)", "👀 10-4, dot received.", "👀 One dot, no context. Noted.", "👀 That's the whole message? Bold choice.", "👀 The mesh works! Proven by a dot.", "👀 Dot acknowledged. Over and out.", "👀 I see your . and raise you a response.", "👀 Short, sweet, and 100% LoRa."])
        else:
            return

        response    = f"{prefix} {response}"
        respond_via = cmd_cfg.get("respond_via", "same")

        if respond_via == "dm" or is_dm:
            dest         = from_id
            resp_channel = channel
        else:
            dest         = None
            resp_channel = channel

        log_bot_activity(from_name, cmd, response, channel)
        threading.Thread(
            target=send_and_record_bot_response,
            args=(interface, response, resp_channel, radio_id, dest, from_name if dest else None),
            daemon=True,
        ).start()

    except Exception as e:
        log.warning(f"Bot command error: {e}")

# ---------------------------------------------------------------------------
# MC Bot
# ---------------------------------------------------------------------------

def _mc_contact_name(config_id, pubkey_pre):
    """Look up MC contact display name by pubkey prefix."""
    from state import mc_connections, mc_connections_lock
    with mc_connections_lock:
        contacts = dict(mc_connections.get(config_id, {}).get("contacts", {}))
    for pubkey, c in contacts.items():
        if pubkey.startswith(pubkey_pre[:min(len(pubkey_pre), len(pubkey))]):
            return c.get("adv_name", pubkey_pre[:8])
    return pubkey_pre[:8]


def _format_delta(delta_s):
    if delta_s < 0:
        delta_s = 0
    if delta_s < 60:     return f"{int(delta_s)}s ago"
    if delta_s < 3600:   return f"{int(delta_s) // 60}m ago"
    if delta_s < 86400:  return f"{int(delta_s) // 3600}h ago"
    return f"{int(delta_s) // 86400}d ago"


def _mc_contact_last_seen_ts(contact, now=None):
    now = int(time.time()) if now is None else int(now)
    max_future = now + 300
    for key in ("last_heard_ts", "last_seen_ts", "last_advert", "lastmod"):
        raw = (contact or {}).get(key)
        if not raw:
            continue
        try:
            ts = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 < ts <= max_future:
            return ts
    return 0


def _mc_contact_seen_ts(contact, now=None):
    now = int(time.time()) if now is None else int(now)
    return _mc_contact_last_seen_ts(contact, now=now)


def build_mc_sitrep(config_id):
    from state import mc_connections, mc_connections_lock
    with mc_connections_lock:
        contacts = dict(mc_connections.get(config_id, {}).get("contacts", {}))
    total = len(contacts)
    if not total:
        return "👀 MC: 0 contacts"
    now = int(time.time())
    cutoff = now - 3600
    recent_count = sum(1 for c in contacts.values() if _mc_contact_seen_ts(c, now) > cutoff)
    recent = sorted(contacts.values(), key=lambda c: _mc_contact_seen_ts(c, now), reverse=True)
    seen_lines = "; ".join(
        f"{c.get('adv_name', '?')[:10]}, {_format_delta(now - _mc_contact_seen_ts(c, now))}"
        for c in recent[:3] if _mc_contact_seen_ts(c, now)
    )
    parts = []
    if seen_lines:
        parts.append(f"Last Seen:\n{seen_lines}")
    parts.append(f"👀 MC Contacts: {total} | 🟢 Heard Recently: {recent_count}")
    return "\n\n".join(parts)


def build_mc_motd_text(cfg, config_id):
    from state import mc_connections, mc_connections_lock
    with mc_connections_lock:
        contacts = dict(mc_connections.get(config_id, {}).get("contacts", {}))
    now = int(time.time())
    cutoff = now - 3600
    recent_count = sum(1 for c in contacts.values() if _mc_contact_seen_ts(c, now) > cutoff)
    total = len(contacts)
    custom = cfg["motd"].get("message", "")
    return (
        f"Bot active | MC: {recent_count} heard recently | {total} known contacts"
        + (f" | {custom}" if custom else "")
    )


def _build_mc_rf_info(msg):
    parts = []
    snr = msg.get("snr") if msg.get("snr") is not None else msg.get("SNR")
    rssi = msg.get("rssi") if msg.get("rssi") is not None else msg.get("RSSI")
    path_len = msg.get("path_len")
    path_hash_mode = msg.get("path_hash_mode")

    if snr is not None:
        try:
            parts.append(f"SNR:{float(snr):.1f} dB")
        except (TypeError, ValueError):
            pass
    if rssi is not None:
        try:
            parts.append(f"RSSI:{int(rssi)} dBm")
        except (TypeError, ValueError):
            pass
    try:
        hop_len = int(path_len)
    except (TypeError, ValueError):
        hop_len = None
    if hop_len is not None and hop_len >= 0:
        hop_label = "direct" if hop_len == 0 else f"{hop_len} hop{'s' if hop_len != 1 else ''}"
        try:
            hash_mode = int(path_hash_mode)
            if hash_mode >= 0:
                hop_label += f" · {hash_mode + 1}B/hop"
        except (TypeError, ValueError):
            pass
        parts.append(hop_label)

    return f" [RF] {' | '.join(parts)}" if parts else ""


def _mc_msg_path_hash_size(msg, path_hex="", hop_len=None, default=1):
    for key in ("path_hash_size", "observed_path_hash_size"):
        value = msg.get(key)
        if value is not None:
            try:
                size = int(value)
                if size > 0:
                    return max(1, min(3, size))
            except (TypeError, ValueError):
                pass
    for key in ("path_hash_mode", "observed_path_hash_mode"):
        value = msg.get(key)
        if value is not None:
            try:
                mode = int(value)
                if mode >= 0:
                    return max(1, min(3, mode + 1))
            except (TypeError, ValueError):
                pass
    if path_hex and hop_len:
        try:
            chars_per_hop = len(path_hex) // int(hop_len)
            if chars_per_hop >= 2:
                return max(1, min(3, chars_per_hop // 2))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    return default


def _mc_configured_path_hash_size(config_id):
    from state import mc_connections, mc_connections_lock
    with mc_connections_lock:
        conn = mc_connections.get(config_id, {})
        for source in (conn.get("node_info") or {}, conn.get("config") or {}):
            mode = source.get("path_hash_mode")
            if mode is not None:
                try:
                    return max(1, min(3, int(mode) + 1))
                except (TypeError, ValueError):
                    pass
    for node in CONFIG.get("mc_nodes", []) or []:
        if str(node.get("id") or "") != str(config_id):
            continue
        mode = node.get("path_hash_mode")
        if mode is not None:
            try:
                return max(1, min(3, int(mode) + 1))
            except (TypeError, ValueError):
                pass
    return 3


def _mc_msg_path_byte_suffix(msg, hashes=None, hop_len=None, config_id=None):
    path = msg.get("path") if msg.get("path") is not None else msg.get("observed_path")
    path_hex = "" if isinstance(path, list) else re.sub(r"[^0-9a-fA-F]", "", str(path or "")).lower()
    size = _mc_msg_path_hash_size(msg, path_hex, hop_len, default=None)
    if size is None and hashes:
        size = max(1, min(3, (len(str(hashes[0])) + 1) // 2))
    if size is None:
        size = _mc_configured_path_hash_size(config_id)
    return f" | {size}byte" if size else ""


def _mc_msg_path_hashes(msg):
    path = msg.get("path") if msg.get("path") is not None else msg.get("observed_path")
    try:
        hop_len = int(msg.get("path_len") if msg.get("path_len") is not None else msg.get("observed_path_len"))
    except (TypeError, ValueError):
        hop_len = None

    if hop_len == 0:
        return [], 0
    if isinstance(path, list):
        hashes = []
        limit = hop_len if hop_len and hop_len > 0 else len(path)
        for node in path[:limit]:
            hop_hash = node.get("hash") if isinstance(node, dict) else node
            if hop_hash:
                hashes.append(str(hop_hash).lower())
        return hashes, hop_len

    path_hex = re.sub(r"[^0-9a-fA-F]", "", str(path or "")).lower()
    if not path_hex:
        return [], hop_len
    hash_size = _mc_msg_path_hash_size(msg, path_hex, hop_len, default=1)
    hash_chars = max(2, hash_size * 2)
    count = hop_len if hop_len and hop_len > 0 else max(1, len(path_hex) // hash_chars)
    hashes = [
        path_hex[i * hash_chars:(i + 1) * hash_chars]
        for i in range(count)
        if path_hex[i * hash_chars:(i + 1) * hash_chars]
    ]
    return hashes, hop_len


def _mc_contact_key(contact, fallback=""):
    for key in ("public_key", "full_key", "id"):
        value = (contact or {}).get(key)
        if value:
            return str(value)
    return str(fallback or "")


def _mc_hop_short_label(contact, fallback_hash):
    clean_id = re.sub(r"[^A-Za-z0-9]", "", _mc_contact_key(contact, fallback_hash)).upper()
    fallback = re.sub(r"[^A-Za-z0-9]", "", str(fallback_hash or "")).upper()
    return (clean_id or fallback or "?")[:3]


def _mc_contacts_by_prefix(config_id, prefix):
    if not prefix:
        return []
    lp = str(prefix).lower()
    from state import mc_connections, mc_connections_lock
    with mc_connections_lock:
        conn = mc_connections.get(config_id, {})
        sources = [dict(conn.get("contacts", {})), dict(conn.get("live_contacts", {}))]
    seen = set()
    matches = []
    for contacts in sources:
        for key, contact in contacts.items():
            full_key = _mc_contact_key(contact, key).lower()
            if not full_key.startswith(lp):
                continue
            dedupe_key = full_key or str(key).lower()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            matches.append(contact)
    return matches


# Whole-route path resolution (S514, EST-460). A 2-byte hash can name two
# repeaters 150 km apart (86a8: Ljubljana + Pordenone). Resolving each hop on
# its own, by recency, is how Dioniso ended up in a bot reply on the mesh.
# The same rule lives in static/js/app.js (_mcResolveWholePath) so the map and
# the bot cannot disagree: a small Viterbi over the whole path, cost = leg km +
# a strong penalty per leg over the maximum believable LoRa link + anchors to
# the sender and to our radio. Real SLO mountain links in the measured data
# reach 60-80 km, hence 80 km; 500 km-equivalent makes an over-long leg a last
# resort, never a tie-break. Keep the two constants in step with app.js.
MC_PATH_MAX_LINK_KM = 80.0
MC_PATH_LINK_PENALTY_KM = 500.0
MC_PATH_AMBIGUOUS_MARGIN_KM = 12.0


def _mc_contact_latlon(contact):
    """(lat, lon) from a stored contact, or (None, None). 0/0 means no fix."""
    for lat_key, lon_key in (("adv_lat", "adv_lon"), ("latitude", "longitude")):
        lat = (contact or {}).get(lat_key)
        lon = (contact or {}).get(lon_key)
        if lat is None or lon is None:
            continue
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        if lat == 0.0 and lon == 0.0:
            continue
        return lat, lon
    return None, None


def _mc_distance_km(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _mc_own_position(config_id):
    from state import mc_connections, mc_connections_lock
    with mc_connections_lock:
        conn = mc_connections.get(config_id, {})
        node = dict(conn.get("node_info") or {})
    return _mc_contact_latlon(node)


def _mc_sender_position(config_id, msg):
    prefix = str((msg or {}).get("pubkey_prefix") or (msg or {}).get("pubkey_pre") or "").lower()
    if not prefix:
        return None, None
    matches = _mc_contacts_by_prefix(config_id, prefix)
    matches = sorted(matches, key=lambda c: _mc_contact_seen_ts(c), reverse=True)
    for contact in matches:
        lat, lon = _mc_contact_latlon(contact)
        if lat is not None:
            return lat, lon
    return None, None


def _mc_hop_candidates(config_id, hop_hash):
    """(all matches, positioned candidates, repeaters-first)."""
    matches = _mc_contacts_by_prefix(config_id, hop_hash)
    repeaters = []
    for contact in matches:
        try:
            contact_type = int(contact.get("type") or 0)
        except (TypeError, ValueError):
            contact_type = 0
        if contact_type == 2:
            repeaters.append(contact)
    pool = repeaters or matches
    positioned = []
    seen = set()
    for contact in pool:
        lat, lon = _mc_contact_latlon(contact)
        if lat is None:
            continue
        key = _mc_contact_key(contact).lower()
        if key in seen:
            continue
        seen.add(key)
        positioned.append(contact)
    return matches, positioned


def _mc_link_cost_km(a, b):
    """Distance plus the over-long-leg penalty; a and b are (lat, lon)."""
    if not a or not b:
        return 0.0
    km = _mc_distance_km(a[0], a[1], b[0], b[1])
    if km is None:
        return 0.0
    if km > MC_PATH_MAX_LINK_KM:
        return km + MC_PATH_LINK_PENALTY_KM + (km - MC_PATH_MAX_LINK_KM) * 4.0
    return km


def _mc_resolve_path_contacts(config_id, hop_hashes, start_pos, end_pos):
    """Resolve the WHOLE path with the shared Viterbi rule.

    hop_hashes are in travel order: hop 0 is next to start_pos (the sender in
    an as-received MC path), the last hop is next to end_pos (our radio).
    Returns one dict per hop, or None when any hop has no positioned candidate
    (the caller then falls back to the old per-hop behaviour, exactly as
    before). Hops with one candidate are fixed cost-only points.
    """
    if not hop_hashes:
        return None
    per_hop = []
    meta = []
    for hop_hash in hop_hashes:
        matches, positioned = _mc_hop_candidates(config_id, hop_hash)
        if not positioned:
            return None
        per_hop.append(positioned)
        meta.append((len(matches), len(positioned)))
    n = len(per_hop)
    pos = [[_mc_contact_latlon(c) for c in cands] for cands in per_hop]

    costs = []          # costs[k][j] = cheapest cost from start through hop k=j
    prevs = []          # prevs[k][j] = index at hop k-1
    first = []
    for j in range(len(per_hop[0])):
        leg = _mc_link_cost_km(start_pos, pos[0][j]) if start_pos and start_pos[0] is not None else 0.0
        first.append(leg)
    costs.append(first)
    prevs.append([None] * len(first))
    for k in range(1, n):
        row = []
        prow = []
        for j in range(len(per_hop[k])):
            best = None
            best_i = None
            for i in range(len(per_hop[k - 1])):
                leg = _mc_link_cost_km(pos[k - 1][i], pos[k][j])
                total = costs[k - 1][i] + leg
                if best is None or total < best:
                    best = total
                    best_i = i
            row.append(best)
            prow.append(best_i)
        costs.append(row)
        prevs.append(prow)

    suffix = [[0.0] * len(per_hop[k]) for k in range(n)]
    last_anchor = []
    for j in range(len(per_hop[n - 1])):
        leg = _mc_link_cost_km(pos[n - 1][j], end_pos) if end_pos and end_pos[0] is not None else 0.0
        last_anchor.append(leg)
        suffix[n - 1][j] = leg
    for k in range(n - 2, -1, -1):
        for j in range(len(per_hop[k])):
            best = None
            for m in range(len(per_hop[k + 1])):
                leg = _mc_link_cost_km(pos[k][j], pos[k + 1][m])
                total = leg + suffix[k + 1][m]
                if best is None or total < best:
                    best = total
            suffix[k][j] = best

    totals = [costs[n - 1][j] + suffix[n - 1][j] for j in range(len(per_hop[n - 1]))]
    best_j = min(range(len(totals)), key=lambda j: totals[j])
    best_total = totals[best_j]
    chosen = [None] * n
    chosen[n - 1] = best_j
    for k in range(n - 1, 0, -1):
        chosen[k - 1] = prevs[k][chosen[k]]

    out = []
    for k in range(n):
        alt_gap = None
        if len(per_hop[k]) > 1:
            for a in range(len(per_hop[k])):
                if a == chosen[k]:
                    continue
                total = costs[k][a] + suffix[k][a]
                gap = total - best_total
                if alt_gap is None or gap < alt_gap:
                    alt_gap = gap
        match_count, selected_count = meta[k]
        out.append({
            "contact": per_hop[k][chosen[k]],
            "ambiguous": alt_gap is not None and alt_gap < MC_PATH_AMBIGUOUS_MARGIN_KM,
            "gap_km": alt_gap,
            "match_count": match_count,
            "selected_count": selected_count,
        })
    return out


def _mc_resolve_hop_contact(config_id, hop_hash):
    """Single-hop resolver, kept for callers that only have one hash.

    Distance to our radio wins when both sides have a position; recency is
    only the last tie-break (and the whole rule when nothing is positioned).
    """
    matches, positioned = _mc_hop_candidates(config_id, hop_hash)
    if not matches:
        return None
    repeaters = []
    for contact in matches:
        try:
            contact_type = int(contact.get("type") or 0)
        except (TypeError, ValueError):
            contact_type = 0
        if contact_type == 2:
            repeaters.append(contact)
    pool = repeaters or matches
    own = _mc_own_position(config_id)
    positioned = [c for c in positioned if c in pool]
    if own[0] is not None and positioned:
        def sort_key(contact):
            lat, lon = _mc_contact_latlon(contact)
            km = _mc_distance_km(own[0], own[1], lat, lon)
            return (km if km is not None else float("inf"), -_mc_contact_seen_ts(contact))
        return sorted(positioned, key=sort_key)[0]
    return sorted(pool, key=lambda c: _mc_contact_seen_ts(c), reverse=True)[0]


def _build_mc_test_hops_info(msg, config_id):
    hashes, hop_len = _mc_msg_path_hashes(msg)
    display_hops = hop_len if hop_len is not None else len(hashes) if hashes else "?"
    byte_suffix = _mc_msg_path_byte_suffix(msg, hashes, hop_len, config_id)
    if hop_len == 0:
        return f" | Hops(0): direct{byte_suffix}"
    if hashes:
        start_pos = _mc_sender_position(config_id, msg)
        end_pos = _mc_own_position(config_id)
        resolutions = _mc_resolve_path_contacts(config_id, hashes, start_pos, end_pos)
        labels = []
        for idx, hop_hash in enumerate(hashes):
            contact = resolutions[idx]["contact"] if resolutions else _mc_resolve_hop_contact(config_id, hop_hash)
            labels.append(_mc_hop_short_label(contact, hop_hash))
        return f" | Hops({display_hops}): {', '.join(labels)}{byte_suffix}"
    if hop_len and hop_len > 0:
        return f" | Hops({hop_len}): unknown{byte_suffix}"
    return f" | Hops(?): unknown{byte_suffix}"


# A reply to an incoming flood message waits a random few seconds first. Fired
# instantly it lands on top of the repeaters rebroadcasting that very message,
# and the asker loses both (S470: EDC-3 in the next room got none of CD's
# replies; the one PONG that arrived answered the only ping no repeater
# relayed). Random, so two bots answering the same command do not collide.
MC_BOT_REPLY_DELAY_S = (3.0, 5.0)


def _mc_bot_reply_wait():
    time.sleep(random.uniform(*MC_BOT_REPLY_DELAY_S))


def send_mc_bot_response(config_id, text, chan_idx, dest_pre=None, reply_delay=False):
    """Send bot reply via MC. Runs in a background thread — blocks until sent or timeout.
    reply_delay=True for answers to an incoming message (see MC_BOT_REPLY_DELAY_S);
    Silent Running is checked AFTER the wait, so switching it on meanwhile still holds."""
    if reply_delay:
        _mc_bot_reply_wait()
    if CONFIG.get("silent_mode"):
        log.info("[bot] Silent Running active — MC bot response suppressed")
        return False
    from mesh_mc import send_chan_msg, send_dm
    try:
        if dest_pre:
            send_dm(config_id, dest_pre, text, timeout=15)
        else:
            send_chan_msg(config_id, chan_idx, text, timeout=15)
        return True
    except Exception as e:
        log.warning(f"[MCBot:{config_id}] send failed: {e}")
        return False


def _mc_radio_name(config_id):
    for node in CONFIG.get("mc_nodes", []) or []:
        if node.get("id") == config_id:
            return node.get("name") or config_id
    return config_id


def store_mc_bot_reply(config_id, text, chan_idx, dest_pre=None, to_name=None):
    """Persist a bot reply in the MC message archive, so a reload shows it.

    Until S504 (2026-11-15) the reply left the server only as an SSE event: it
    lived in the browser that happened to be listening and nowhere else. A
    client that was off when the answer was sent never saw it, and any reload
    dropped it. The row is written with the same shape the send routes use, so
    `/api/mc/<radio>/messages` returns it on a fresh load.
    """
    msg = {
        "type":       "mc_message",
        "radio_id":   config_id,
        "radio_name": _mc_radio_name(config_id),
        "network":    "mc",
        "subtype":    "dm" if dest_pre else "channel",
        "channel":    chan_idx if chan_idx is not None else 0,
        "from_id":    "bot",
        "from_name":  "Bot",
        "text":       text,
        "ts":         int(time.time()),
        "sent":       True,
        "status":     "delivered",
    }
    if dest_pre:
        msg["to_id"] = dest_pre
        msg["to_name"] = to_name
    try:
        save_mc_message(msg)
    except Exception as e:
        log.warning(f"[MCBot:{config_id}] could not store bot reply: {e}")
    return msg


def send_and_record_mc_bot_response(config_id, text, chan_idx, dest_pre=None, to_name=None, cmd=""):
    if send_mc_bot_response(config_id, text, chan_idx, dest_pre, reply_delay=True):
        store_mc_bot_reply(config_id, text, chan_idx, dest_pre, to_name)
        push_to_sse({
            "type": "mc_bot_reply",
            "radio_id": config_id,
            "to_id": dest_pre,
            "to_name": to_name,
            "cmd": cmd,
            "text": text,
            "channel": chan_idx,
        })


def handle_mc_bot_command(msg, config_id, subtype):
    """Handle a bot command arriving on an MC channel or DM."""
    try:
        cfg = load_bot_config(config_id)
        if not cfg.get("enabled"):
            return
        # Same reasoning as the MT side: say it out loud, so "no reply" in the log is never ambiguous
        # (Filip, 2026-10-16).
        if CONFIG.get("silent_mode"):
            who = msg.get("pubkey_prefix") or msg.get("pubkey_pre") or "?"
            log.info(f"[bot] Silent Running active — MC bot command from {who} on {config_id} ({subtype}) ignored, no reply sent")
            return

        prefix  = f"[{cfg.get('bot_label', 'OM Bot')}]"
        text    = (msg.get("text") or "").strip()
        if not text:
            return

        is_dm    = (subtype == "dm")
        channel  = msg.get("channel_idx", 0)
        from_pre = msg.get("pubkey_prefix", "?") if is_dm else msg.get("pubkey_pre", "?")

        if not is_dm and channel not in cfg.get("listen_channels", [0]):
            return

        from_name = _mc_contact_name(config_id, from_pre)

        # MC channel messages embed sender name as "Name: actual_text" — strip it
        if not is_dm:
            colon_idx = text.find(': ')
            if 0 < colon_idx < 50:
                text = text[colon_idx + 2:].strip()

        if not text:
            return

        # --- relay <TARGET> <message> ---
        if text.lower().startswith("relay "):
            if not cfg["commands"].get("relay", {}).get("enabled"):
                return
            parts = text.split(" ", 2)
            if len(parts) < 3:
                response = f"{prefix} Usage: relay <NAME> <message>"
            else:
                target_name = parts[1]
                relay_msg   = parts[2]
                target_pre  = None
                from state import mc_connections, mc_connections_lock
                with mc_connections_lock:
                    contacts = dict(mc_connections.get(config_id, {}).get("contacts", {}))
                for pubkey, c in contacts.items():
                    if c.get("adv_name", "").lower() == target_name.lower():
                        target_pre = pubkey[:12]
                        break
                if not target_pre:
                    response = f"{prefix} Contact '{target_name}' not found on MC mesh."
                else:
                    relay_cfg  = cfg["commands"].get("relay", {})
                    relay_mode = relay_cfg.get("relay_mode", "dm")
                    relay_ch   = int(relay_cfg.get("relay_channel", 0))
                    relay_text = f"[{cfg.get('bot_label', 'OM Bot')} relay from {from_name[:10]}]: {relay_msg}"
                    try:
                        dest = None if relay_mode == "broadcast" else target_pre
                        threading.Thread(
                            target=send_mc_bot_response,
                            args=(config_id, relay_text, relay_ch, dest),
                            kwargs={"reply_delay": True},
                            daemon=True,
                        ).start()
                        response = f"{prefix} Relayed to {target_name}."
                        log_bot_activity(from_name, "relay", f"→ {target_name}: {relay_msg}", channel)
                    except Exception as e:
                        response = f"{prefix} Failed to relay: {e}"
                        log_bot_activity(from_name, "relay", f"FAILED: {e}", channel)
                    resp_dest = from_pre if is_dm else None
                    threading.Thread(
                        target=send_and_record_mc_bot_response,
                        args=(config_id, response, channel, resp_dest, from_name, "relay"),
                        daemon=True,
                    ).start()
                    return

            resp_dest = from_pre if is_dm else None
            threading.Thread(
                target=send_and_record_mc_bot_response,
                args=(config_id, response, channel, resp_dest, from_name, "relay"),
                daemon=True,
            ).start()
            log_bot_activity(from_name, "relay", response, channel)
            return

        # --- Single-word commands ---
        cmd_key = "dot" if text == "." else text.lower().split()[0].rstrip("!?.,;")
        if not cmd_key:
            return

        cmd_cfg = cfg["commands"].get(cmd_key)
        if not cmd_cfg or not cmd_cfg.get("enabled"):
            return

        cmd = cmd_key
        rf_info = _build_mc_rf_info(msg)
        if cmd == "ping":
            response = f"🏓PONG | {random.choice(['Still here, unfortunately.', 'Not dead yet.', 'Present and accounted for.', 'You rang?', 'Did someone say ping?', 'Responding as trained.', 'Loud and proud.', 'Alive and well.', 'Mesh is alive!', 'Roger that, I exist.', 'Beep boop, I am a bot.', 'Oh, you noticed me!', 'Here!', 'Indeed.', 'Obviously.'])}"
        elif cmd == "ack":
            response = random.choice(["✋Ack to you!", "✋Copy that!", "✋Acknowledged", "✋Received!", "✋Loud and clear", "✋Message received, filing it away", "✋Got it, doing nothing about it", "✋Confirmed. Mostly.", "✋10-4, good buddy", "✋Wilco"]) + rf_info
        elif cmd == "test":
            response = random.choice(["🎙Roger that!", "🎙Testing 1,2,3", "🎙Testing, testing", "🎙Read you loud and clear", "🎙Signal received", "🎙Loud and clear", "🎙You are coming through loud and hot", "🎙Heard you the first time", "🎙Five by five", "🎙Is this thing on? Yes, yes it is", "🎙Transmission received, sanity intact", "🎙Strength 5, readability 5", "🎙Clear as a bell", "🎙Your signal is better than my day", "🎙Mesh works, miracles do happen"]) + _build_mc_test_hops_info(msg, config_id)
        elif cmd == "sitrep":
            response = build_mc_sitrep(config_id)
        elif cmd == "cmd":
            enabled = sorted([c for c, v in cfg["commands"].items() if v.get("enabled") and c != "dot"])
            parts_l = [c + (' (relay "NAME" msg)' if c == "relay" else "") for c in enabled]
            response = "Commands: " + " | ".join(parts_l)
        elif cmd == "motd":
            response = build_mc_motd_text(cfg, config_id)
        elif cmd == "joke":
            response = random.choice(BOT_JOKES).replace("Meshtastic", "MeshCore")
        elif cmd == "dot":
            response = random.choice(["👀 Yes, I can see your . there ;)", "👀 Ah, a lone dot. Minimalist.", "👀 Received one (1) dot. Processing...", "👀 A dot! The mesh carried that with dignity.", "👀 Your . arrived safely. Was it worth it? ;)", "👀 10-4, dot received.", "👀 One dot, no context. Noted.", "👀 That's the whole message? Bold choice.", "👀 The mesh works! Proven by a dot.", "👀 Dot acknowledged. Over and out.", "👀 I see your . and raise you a response.", "👀 Short, sweet, and 100% LoRa."])
        else:
            return

        response    = f"{prefix} {response}"
        respond_via = cmd_cfg.get("respond_via", "same")

        if respond_via == "dm" or is_dm:
            dest_pre = from_pre
            resp_ch  = 0
        else:
            dest_pre = None
            resp_ch  = channel

        log_bot_activity(from_name, cmd, response, channel)
        threading.Thread(
            target=send_and_record_mc_bot_response,
            args=(config_id, response, resp_ch, dest_pre, from_name, cmd),
            daemon=True,
        ).start()

    except Exception as e:
        log.warning(f"[MCBot:{config_id}] command error: {e}")


# ---------------------------------------------------------------------------
# MOTD scheduler
# ---------------------------------------------------------------------------

def motd_scheduler_loop():
    """Wakes every 60s (or early on config change) and fires MOTD for each radio that needs it."""
    while True:
        try:
            _motd_event.wait(timeout=60)
            _motd_event.clear()

            with connections_lock:
                connected_ids = [
                    rid for rid, state in connections.items()
                    if state.get("status") == "connected" and state.get("iface")
                ]

            now = int(time.time())
            for radio_id in connected_ids:
                with connections_lock:
                    state = connections.get(radio_id, {})
                    if state.get("status") != "connected":
                        continue
                    iface = state.get("iface")
                if not iface:
                    continue
                try:
                    cfg = load_bot_config(radio_id)
                    if not cfg.get("enabled") or not cfg.get("motd", {}).get("enabled") or CONFIG.get("silent_mode"):
                        continue
                    last_sent = _motd_last_sent_per_radio.get(radio_id, 0)
                    mode      = cfg["motd"].get("mode", "interval")
                    should_fire = False
                    if mode == "fixed":
                        fixed_time = cfg["motd"].get("fixed_time", "08:00")
                        try:
                            h, m = map(int, fixed_time.split(":"))
                            if not (0 <= h <= 23 and 0 <= m <= 59):
                                raise ValueError("out of range")
                        except ValueError:
                            h, m = 8, 0
                        now_dt  = datetime.now()
                        fire_dt = now_dt.replace(hour=h, minute=m, second=0, microsecond=0)
                        if abs((now_dt - fire_dt).total_seconds()) < 90 and last_sent < fire_dt.timestamp():
                            should_fire = True
                    else:
                        interval_secs = cfg["motd"].get("interval_hours", 4) * 3600
                        should_fire   = (now - last_sent) >= interval_secs
                    if not should_fire:
                        continue
                    text     = f"[{cfg.get('bot_label', 'OM Bot')}] {build_motd_text(cfg)}"
                    channels = motd_target_channels(cfg)
                    for ch in channels:
                        send_bot_response(iface, text, ch)
                        push_bot_chat_message(text, ch, radio_id)
                    _motd_last_sent_per_radio[radio_id] = int(fire_dt.timestamp()) if mode == "fixed" else now
                    log_bot_activity("Bot", "motd_scheduled", text, channels[0])
                except Exception as e:
                    log.warning(f"MOTD error for {radio_id}: {e}")

            # --- MC radios ---
            from state import mc_connections, mc_connections_lock
            with mc_connections_lock:
                mc_connected_ids = [
                    rid for rid, state in mc_connections.items()
                    if state.get("status") == "connected"
                ]
            for radio_id in mc_connected_ids:
                try:
                    cfg = load_bot_config(radio_id)
                    if not cfg.get("enabled") or not cfg.get("motd", {}).get("enabled") or CONFIG.get("silent_mode"):
                        continue
                    last_sent = _motd_last_sent_per_radio.get(radio_id, 0)
                    mode      = cfg["motd"].get("mode", "interval")
                    should_fire = False
                    fire_ts     = now
                    if mode == "fixed":
                        fixed_time = cfg["motd"].get("fixed_time", "08:00")
                        try:
                            h, m = map(int, fixed_time.split(":"))
                            if not (0 <= h <= 23 and 0 <= m <= 59):
                                raise ValueError("out of range")
                        except ValueError:
                            h, m = 8, 0
                        now_dt  = datetime.now()
                        fire_dt = now_dt.replace(hour=h, minute=m, second=0, microsecond=0)
                        if abs((now_dt - fire_dt).total_seconds()) < 90 and last_sent < fire_dt.timestamp():
                            should_fire = True
                            fire_ts     = int(fire_dt.timestamp())
                    else:
                        interval_secs = cfg["motd"].get("interval_hours", 4) * 3600
                        should_fire   = (now - last_sent) >= interval_secs
                    if not should_fire:
                        continue
                    text     = f"[{cfg.get('bot_label', 'OM Bot')}] {build_mc_motd_text(cfg, radio_id)}"
                    channels = motd_target_channels(cfg)
                    for ch in channels:
                        threading.Thread(
                            target=send_mc_bot_response,
                            args=(radio_id, text, ch),
                            daemon=True,
                        ).start()
                    _motd_last_sent_per_radio[radio_id] = fire_ts
                    log_bot_activity("Bot", "motd_scheduled_mc", text, channels[0])
                except Exception as e:
                    log.warning(f"MOTD MC error for {radio_id}: {e}")

        except Exception as e:
            log.warning(f"MOTD scheduler error: {e}")
