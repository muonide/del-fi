"""Config loading and validation for Del-Fi.

Loads a single YAML file, validates it, merges defaults and resolves paths.
load_config() prints a human-readable error and exits on bad config — the
one place where crashing is correct. read_config() raises ConfigError
instead, for callers (the GUI) that must not exit.
"""

import logging
import os
import re
import sys
from pathlib import Path

import yaml

log = logging.getLogger("del_fi.config")

DEFAULTS: dict = {
    "model": "gemma4:4b",
    "personality": "You are a helpful and concise community assistant.",
    "fallback_message": "",              # empty: suggest known topics instead
    "welcome_footer": "",                # first-contact footer; empty = built-in
    "knowledge_folder": "./knowledge",
    # --- Wiki ---
    "wiki_folder": "./wiki",
    "wiki_builder_model": None,          # falls back to model if unset
    "wiki_rebuild_on_start": False,
    "wiki_stale_after_days": 30,
    "wiki_watch_enabled": True,
    "wiki_watch_interval_seconds": 60,
    "wiki_patch_model": "",              # watcher rebuilds; falls back to model
    "time_sensitive_files": ["weather-station.md", "trail-camera-log.md"],
    # --- Retrieval ---
    "max_response_bytes": 230,
    "similarity_threshold": 0.28,
    "rag_top_k": 4,
    "max_context_tokens": None,
    "small_model_prompt": False,
    "reorder_context": False,
    # --- Mesh ---
    "mesh_protocol": "meshtastic",
    "radio_connection": "serial",
    "radio_port": "/dev/ttyUSB0",
    "rate_limit_seconds": 30,
    "rate_limit_notice": True,           # one "slow down" reply per window
    "query_queue_size": 10,              # questions waiting for the LLM
    "want_ack": True,                    # Meshtastic: firmware retries DMs
    # --- Ollama ---
    "ollama_host": "http://localhost:11434",
    "ollama_timeout": 120,
    "wiki_build_timeout": 600,  # seconds per page; large models need more time
    "embedding_model": "nomic-embed-text",
    "num_ctx": None,
    "num_predict": 300,
    # --- Response cache ---
    "persistent_cache": True,
    "response_cache_ttl": 300,
    "auto_send_chunks": 3,
    "busy_notice": True,
    # --- Memory ---
    "memory_max_turns": 0,
    "memory_ttl": 3600,
    "persistent_memory": False,
    # --- Board ---
    "board_enabled": False,
    "board_max_posts": 50,
    "board_post_ttl": 86400,
    "board_show_count": 5,
    "board_persist": True,
    "board_rate_limit": 3,
    "board_rate_window": 3600,
    "board_blocked_patterns": [],
    # --- Facts / FactStore ---
    "fact_feed_file": "",
    "fact_watch_interval_seconds": 30,
    "fact_query_keywords": [
        "temperature", "temp", "humidity", "wind", "pressure",
        "barometer", "snow", "conditions", "current", "right now", "latest",
        "camera", "detected", "detection", "spotted", "sighted",
        "last seen", "cam-1", "cam-2", "cam-3", "cam1", "cam2", "cam3",
    ],
    # --- Logging ---
    "log_level": "info",
    "log_file": "",                      # also log to this file (rotated)
}

# Oracle profiles: per-model default overrides applied automatically
# when the configured model name contains the profile key (substring, case-insensitive).
# Keys NOT explicitly set in config.yaml take the profile value.
ORACLE_PROFILES: dict[str, dict] = {
    "gemma4:2b": {
        "similarity_threshold": 0.35,
        "rag_top_k": 2,
        "max_context_tokens": 512,
        "small_model_prompt": True,
        "reorder_context": True,
    },
    "gemma4:4b": {
        "similarity_threshold": 0.28,
        "rag_top_k": 4,
    },
    "gemma4:12b": {
        "similarity_threshold": 0.25,
        "rag_top_k": 5,
        "max_context_tokens": 3000,
    },
    "gemma3:1b": {
        "similarity_threshold": 0.35,
        "rag_top_k": 2,
        "max_context_tokens": 512,
        "small_model_prompt": True,
        "reorder_context": True,
    },
    "llama3.2:1b": {
        "similarity_threshold": 0.35,
        "rag_top_k": 2,
        "max_context_tokens": 512,
        "small_model_prompt": True,
        "reorder_context": True,
    },
    "gemma3:4b": {
        "similarity_threshold": 0.28,
        "rag_top_k": 4,
    },
    "qwen2.5:3b": {
        "similarity_threshold": 0.28,
        "rag_top_k": 4,
    },
}

# Protocol-specific defaults merged when mesh_protocol is set
MESHCORE_DEFAULTS: dict = {
    "port": "/dev/ttyUSB0",
    "connection": "serial",
    "baud_rate": 115200,
}

SUPPORTED_PROTOCOLS = ("meshtastic", "meshcore")

MESH_DEFAULTS: dict = {
    "gossip": {
        "enabled": False,            # announce + listen for DEL-FI: announcements
        "announce_interval": 14400,  # seconds between broadcasts (min 900)
        "directory_ttl": 86400,      # forget nodes not heard from for this long
        "channel": 0,                # channel index for announcements
    },
    "peers": [],                     # [{node_id: "!a1b2c3d4", name: "..."}]
    "sync": {                        # Tier 2 Q&A sync: reserved, not implemented yet
        "enabled": False,
        "window_start": "02:00",
        "window_end": "05:00",
        "max_cache_age": "7d",
        "max_cache_entries": 500,
    },
    "serve_to_peers": False,
    "tag_responses": True,
    "reject_contradictions": True,
}

MIN_ANNOUNCE_INTERVAL = 900
_NODE_ID = re.compile(r"^![0-9a-fA-F]{8}$")
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$")
_DURATION_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def _match_profile(model: str) -> dict | None:
    """Return oracle profile overrides for a known model, or None."""
    model_lower = model.lower()
    for profile_key, profile_vals in ORACLE_PROFILES.items():
        if profile_key in model_lower:
            return profile_vals
    return None


class ConfigError(Exception):
    """A configuration problem, with a message meant for the operator."""


# Keys accepted at the top level besides DEFAULTS (warned about otherwise).
_EXTRA_KEYS = frozenset({
    "node_name", "mesh_knowledge", "meshcore", "oracle_type",
    # v0.2 names, mapped into mesh_knowledge with their own warning
    "trusted_peers", "peer_cache_ttl", "max_cache_entries", "gossip_announce_interval",
})
_LOG_LEVELS = ("debug", "info", "warning", "error", "critical")


def default_config_path() -> str:
    """config.yaml next to main.py if present, else ~/del-fi/config.yaml."""
    local_config = Path(__file__).resolve().parent.parent / "config.yaml"
    if local_config.exists():
        return str(local_config)
    return os.path.expanduser("~/del-fi/config.yaml")


def load_config(config_path: str | None = None) -> dict:
    """Load, validate, and return config dict. Prints the problem and exits
    on bad config."""
    try:
        return read_config(config_path)
    except ConfigError as e:
        print(f"[del-fi] Config error: {e}", file=sys.stderr)
        sys.exit(1)


def read_config(config_path: str | None = None) -> dict:
    """Load, validate, and return config dict. Raises ConfigError."""
    path = Path(config_path or default_config_path())
    if not path.exists():
        _die(
            f"Config file not found: {path}\n"
            "  Copy config.example.yaml to that location and edit it."
        )

    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        _die(f"Invalid YAML in {path}:\n  {e}")
    if not isinstance(raw, dict):
        _die(f"{path} must be a YAML mapping of 'key: value' lines")

    if "node_name" not in raw or not str(raw["node_name"] or "").strip():
        _die(f"Missing required config field: 'node_name'\n  Add it to {path}")
    raw["node_name"] = str(raw["node_name"]).strip()

    unknown = sorted(str(k) for k in raw if k not in DEFAULTS and k not in _EXTRA_KEYS)
    if unknown:
        log.warning(f"config: unknown key(s) ignored: {', '.join(unknown)} — check spelling")

    # Merge defaults
    cfg: dict = {**DEFAULTS, **raw}
    if not isinstance(cfg.get("model"), str) or not cfg["model"].strip():
        _die(f"model must be an Ollama model name like 'gemma3:4b' (got {cfg.get('model')!r})")

    # Apply oracle profile for known small models
    profile = _match_profile(cfg.get("model", ""))
    if profile:
        for key, val in profile.items():
            if key not in raw:
                cfg[key] = val
        log.debug(f"oracle profile applied for model '{cfg['model']}'")

    cfg["log_level"] = str(cfg["log_level"]).lower()

    # Resolve paths relative to the config file's directory
    config_dir = str(path.resolve().parent)

    # wiki_folder
    wiki_raw = cfg["wiki_folder"]
    wiki_raw = os.path.expanduser(wiki_raw)
    if not os.path.isabs(wiki_raw):
        wiki_raw = os.path.join(config_dir, wiki_raw)
    cfg["wiki_folder"] = wiki_raw

    # knowledge_folder
    knowledge_raw = os.path.expanduser(str(cfg.get("knowledge_folder") or "./knowledge"))
    if not os.path.isabs(knowledge_raw):
        knowledge_raw = os.path.join(config_dir, knowledge_raw)
    cfg["knowledge_folder"] = knowledge_raw

    # log_file (optional)
    if cfg.get("log_file"):
        log_raw = os.path.expanduser(str(cfg["log_file"]))
        cfg["log_file"] = log_raw if os.path.isabs(log_raw) else os.path.join(config_dir, log_raw)

    # Derived runtime paths (all relative to config dir)
    cfg["_config_path"] = str(path.resolve())
    cfg["_config_dir"] = config_dir
    cfg["_vectorstore_dir"] = os.path.join(config_dir, "vectorstore")
    cfg["_cache_dir"] = os.path.join(config_dir, "cache")
    cfg["_gossip_dir"] = os.path.join(config_dir, "gossip")
    cfg["_seen_senders_file"] = os.path.join(config_dir, "seen_senders.txt")

    # Mesh protocol normalization
    if not isinstance(cfg["mesh_protocol"], str):
        _die(f"mesh_protocol must be one of: {', '.join(SUPPORTED_PROTOCOLS)}")
    cfg["mesh_protocol"] = cfg["mesh_protocol"].lower()
    if cfg["mesh_protocol"] == "meshcore":
        mc_raw = raw.get("meshcore", {})
        cfg["meshcore"] = {
            **MESHCORE_DEFAULTS,
            **(mc_raw if isinstance(mc_raw, dict) else {}),
        }

    cfg["mesh_knowledge"] = _merge_mesh_knowledge(raw)

    _validate(cfg)
    return cfg


def parse_duration(value) -> float | None:
    """'30s', '15m', '12h', '7d' or a number of seconds -> seconds (None if invalid)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    m = _DURATION.match(str(value))
    if not m:
        return None
    return float(m.group(1)) * _DURATION_UNITS[m.group(2)]


def _merge_mesh_knowledge(raw: dict) -> dict:
    """The mesh_knowledge block with defaults filled in.

    v0.2's config.example.yaml used top-level keys instead (trusted_peers,
    peer_cache_ttl, max_cache_entries, gossip_announce_interval); those are
    mapped in with a warning.
    """
    mk = raw.get("mesh_knowledge") or {}
    if not isinstance(mk, dict):
        log.warning(f"mesh_knowledge must be a mapping, got {type(mk).__name__!r} — ignoring")
        mk = {}

    merged: dict = {}
    for key, default_val in MESH_DEFAULTS.items():
        if isinstance(default_val, dict):
            given = mk.get(key)
            merged[key] = {**default_val, **(given if isinstance(given, dict) else {})}
        else:
            merged[key] = mk.get(key, default_val)

    legacy = {
        "gossip_announce_interval": ("gossip", "announce_interval"),
        "peer_cache_ttl": ("sync", "max_cache_age"),
        "max_cache_entries": ("sync", "max_cache_entries"),
    }
    for old_key, (section, new_key) in legacy.items():
        if old_key in raw:
            log.warning(f"config: '{old_key}' is deprecated — use mesh_knowledge.{section}.{new_key}")
            merged[section][new_key] = raw[old_key]

    if "trusted_peers" in raw:
        log.warning(
            "config: 'trusted_peers' is deprecated — use mesh_knowledge.peers "
            "with hardware node IDs (display names are not authenticated)"
        )
        for entry in raw.get("trusted_peers") or []:
            entry = str(entry).strip()
            if _NODE_ID.match(entry):
                merged["peers"].append({"node_id": entry})
            else:
                log.warning(f"config: ignoring trusted peer {entry!r} — not a node ID like !a1b2c3d4")
    return merged


def _validate_mesh_knowledge(mk: dict) -> None:
    gossip = mk["gossip"]
    if not isinstance(gossip.get("enabled"), bool):
        _die("mesh_knowledge.gossip.enabled must be true or false")
    interval = parse_duration(gossip.get("announce_interval"))
    if interval is None or interval < MIN_ANNOUNCE_INTERVAL:
        _die(
            f"mesh_knowledge.gossip.announce_interval must be at least "
            f"{MIN_ANNOUNCE_INTERVAL} seconds (got {gossip.get('announce_interval')!r}).\n"
            "  Announcements share airtime with everyone on the channel."
        )
    gossip["announce_interval"] = interval
    ttl = parse_duration(gossip.get("directory_ttl"))
    if ttl is None or ttl <= 0:
        _die(f"mesh_knowledge.gossip.directory_ttl must be a positive duration "
             f"(got {gossip.get('directory_ttl')!r})")
    gossip["directory_ttl"] = ttl
    channel = gossip.get("channel")
    if not isinstance(channel, int) or isinstance(channel, bool) or not 0 <= channel <= 7:
        _die(f"mesh_knowledge.gossip.channel must be a channel index 0–7 (got {channel!r})")

    peers = mk.get("peers")
    if not isinstance(peers, list):
        _die("mesh_knowledge.peers must be a list of {node_id, name} entries")
    for peer in peers:
        node_id = peer.get("node_id") if isinstance(peer, dict) else None
        if not isinstance(node_id, str) or not _NODE_ID.match(node_id):
            _die(
                f"mesh_knowledge.peers entry {peer!r} needs a node_id like \"!a1b2c3d4\"\n"
                "  Peering is by hardware node ID; display names are not authenticated."
            )

    sync = mk["sync"]
    age = parse_duration(sync.get("max_cache_age"))
    if age is None or age <= 0:
        _die(f"mesh_knowledge.sync.max_cache_age must be a positive duration like 7d "
             f"(got {sync.get('max_cache_age')!r})")
    sync["max_cache_age"] = age
    entries = sync.get("max_cache_entries")
    if not isinstance(entries, int) or isinstance(entries, bool) or entries < 1:
        _die(f"mesh_knowledge.sync.max_cache_entries must be a positive integer (got {entries!r})")


def _check_int(cfg: dict, key: str, minimum: int, optional: bool = False) -> None:
    value = cfg.get(key)
    if optional and value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        _die(f"{key} must be an integer ≥ {minimum}{' or empty' if optional else ''} (got {value!r})")


def _validate(cfg: dict) -> None:
    """Validate config values. Raises ConfigError."""
    if cfg["radio_connection"] not in ("serial", "tcp", "ble"):
        _die(f"radio_connection must be serial, tcp or ble (got {cfg['radio_connection']!r})")

    if cfg["log_level"] not in _LOG_LEVELS:
        _die(f"log_level must be one of: {', '.join(_LOG_LEVELS)} (got {cfg['log_level']!r})")

    _check_int(cfg, "auto_send_chunks", minimum=1)
    _check_int(cfg, "num_predict", minimum=16)
    _check_int(cfg, "num_ctx", minimum=512, optional=True)
    _check_int(cfg, "max_context_tokens", minimum=64, optional=True)
    _check_int(cfg, "memory_max_turns", minimum=0)

    timeout = cfg.get("ollama_timeout")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        _die(f"ollama_timeout must be a positive number of seconds (got {timeout!r})")

    if cfg["mesh_protocol"] not in SUPPORTED_PROTOCOLS:
        _die(
            f"Invalid mesh_protocol: '{cfg['mesh_protocol']}'\n"
            f"  Supported: {', '.join(SUPPORTED_PROTOCOLS)}"
        )

    max_bytes = cfg.get("max_response_bytes", 230)
    if not isinstance(max_bytes, int) or max_bytes < 50 or max_bytes > 256:
        _die(
            f"max_response_bytes must be an integer 50–256 (got {max_bytes!r}).\n"
            "  LoRa practical limit is 230 bytes."
        )

    rate = cfg.get("rate_limit_seconds", 30)
    if not isinstance(rate, (int, float)) or rate < 0:
        _die(f"rate_limit_seconds must be a non-negative number (got {rate!r})")

    qsize = cfg.get("query_queue_size", 10)
    if not isinstance(qsize, int) or isinstance(qsize, bool) or qsize < 1:
        _die(f"query_queue_size must be a positive integer (got {qsize!r})")

    ttl = cfg.get("response_cache_ttl", 300)
    if not isinstance(ttl, (int, float)) or ttl < 0:
        _die(f"response_cache_ttl must be a non-negative number (got {ttl!r})")

    _validate_mesh_knowledge(cfg["mesh_knowledge"])


def _die(message: str) -> None:
    raise ConfigError(message)
