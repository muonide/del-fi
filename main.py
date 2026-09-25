"""Del-Fi daemon entry point (v0.2).

Usage:
  python main.py [--config PATH] [--simulator]
  python main.py --build-wiki [--config PATH]
  python main.py --lint-wiki  [--config PATH]
"""

import argparse
import logging
import os
import queue
import signal
import sys
import threading
import time

from del_fi.config import load_config
from del_fi.core.dispatcher import Dispatcher
from del_fi.core.facts import FactStore
from del_fi.core.knowledge import WikiEngine
from del_fi.core.peers import GossipDirectory, PeerCache
from del_fi.core.router import Router
from del_fi.mesh import create_interface

VERSION = "0.2"

log = logging.getLogger("del_fi")


# ─────────────────────────── Logging ──────────────────────────────────────


class _DelFiFormatter(logging.Formatter):
    def format(self, record):
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        text = f"[{ts}] {record.getMessage()}"
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        if record.stack_info:
            text += "\n" + self.formatStack(record.stack_info)
        return text


def setup_logging(level: str, simulator: bool = False):
    numeric = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric)
    if simulator:
        handler = logging.FileHandler("del_fi.log", mode="a", encoding="utf-8")
    else:
        handler = logging.StreamHandler()
    handler.setFormatter(_DelFiFormatter())
    root.addHandler(handler)


# ─────────────────────────── Banner ───────────────────────────────────────


def print_banner(cfg: dict, wiki: WikiEngine, mesh_iface, gossip_dir: GossipDirectory):
    name = cfg["node_name"]
    model = cfg["model"]
    pages = wiki.page_count
    status = "ready" if wiki.available else "waiting for ollama"
    protocol = getattr(mesh_iface, "protocol_name", cfg.get("mesh_protocol", "meshtastic"))

    if protocol == "Simulator":
        radio_str = "simulator"
    elif mesh_iface.connected:
        conn = cfg.get("radio_connection", "")
        port = cfg.get("radio_port", "")
        radio_str = f"+ {protocol} · {conn}:{port}"
    else:
        radio_str = f"- {protocol} (reconnecting)"

    lines = [
        f"  ·· DEL-FI ··  v{VERSION}",
        f"  node: {name}",
        f"  model: {model} · {pages} wiki pages · {status}",
        f"  radio: {radio_str}",
    ]

    peer_names = [p["node_name"] for p in gossip_dir.list_peers()]
    if peer_names:
        lines.append(f"  peers: {' · '.join(peer_names)}")

    w = max(len(line) for line in lines) + 2
    print(f"\u2554{'═' * w}\u2557")
    for line in lines:
        print(f"\u2551{line:<{w}}\u2551")
    print(f"\u255a{'═' * w}\u255d")


# ─────────────────────────── Background threads ───────────────────────────


def ollama_health_check(wiki: WikiEngine, stop: threading.Event):
    while not stop.is_set():
        if not wiki.available:
            wiki.check_ollama()
        stop.wait(30)


def maintenance_worker(router: Router, stop: threading.Event):
    """Once a minute: flush the response cache to disk (batched to reduce
    SD card wear) and drop expired conversation memory."""
    while not stop.wait(60):
        try:
            router.flush_cache()
            if router.memory:
                router.memory.cleanup()
        except Exception:
            log.exception("maintenance error")


# ─────────────────────────── Non-daemon modes ─────────────────────────────


def run_build_wiki(cfg: dict):
    """--build-wiki: compile knowledge/ → wiki/ then exit."""
    wiki = WikiEngine(cfg)
    if not wiki.available:
        print("ERROR: Ollama is not available. Start Ollama and try again.")
        sys.exit(1)
    print(f"Building wiki from {cfg['knowledge_folder']} ...")
    count = wiki.build()
    if count:
        print(f"Done. Built {count} wiki page(s) in {cfg['wiki_folder']}")
    else:
        print("No new or changed source files found.")
    sys.exit(0)


def run_lint_wiki(cfg: dict):
    """--lint-wiki: check wiki health then exit."""
    wiki = WikiEngine(cfg)
    issues = wiki.lint()
    if not issues:
        print("Wiki is clean.")
    else:
        print(f"Found {len(issues)} issue(s):")
        for issue in issues:
            print(f"  * {issue}")
    sys.exit(0 if not issues else 1)


# ─────────────────────────── Daemon ───────────────────────────────────────


def run_daemon(cfg: dict, simulator: bool):
    # Ensure runtime directories exist
    for d in (
        cfg["knowledge_folder"],
        cfg["_cache_dir"],
        cfg["_gossip_dir"],
        cfg["wiki_folder"],
    ):
        os.makedirs(d, exist_ok=True)

    # WikiEngine (replaces RAGEngine)
    wiki = WikiEngine(cfg)

    if not wiki.available:
        log.warning("ollama not available at startup — commands work, queries wait")

    # Optional: rebuild wiki at startup
    if cfg.get("wiki_rebuild_on_start") and wiki.available:
        log.info("wiki_rebuild_on_start is set — rebuilding...")
        count = wiki.build()
        log.info(f"wiki rebuild: {count} page(s) written")

    if not wiki.wiki_available:
        log.warning(
            "wiki/ is empty — run 'python main.py --build-wiki' to compile knowledge"
        )

    # FactStore
    fact_store = FactStore(cfg)

    # PeerCache + GossipDirectory
    peer_cache = PeerCache(cfg)
    gossip_dir = GossipDirectory(cfg)

    # Router
    router = Router(cfg, wiki, peer_cache, gossip_dir, fact_store=fact_store)

    # Mesh adapter + dispatcher
    inbox: queue.Queue = queue.Queue()
    mesh_iface = create_interface(cfg, simulator, inbox)
    dispatcher = Dispatcher(cfg, router, mesh_iface.send_dm)

    if simulator:
        print_banner(cfg, wiki, mesh_iface, gossip_dir)
        mesh_iface.connect()  # starts the stdin chat prompt
    else:
        if not mesh_iface.connect():
            log.warning("radio not connected — will keep retrying")
        # Supervisor: reconnects whenever the link drops, for the daemon's life.
        threading.Thread(
            target=mesh_iface.reconnect_loop, name="radio-supervisor", daemon=True
        ).start()
        print_banner(cfg, wiki, mesh_iface, gossip_dir)

    # Stop event for all background threads
    stop_event = threading.Event()

    # Background: wiki watcher (re-builds on knowledge/ changes)
    wiki_watch_interval = cfg.get("wiki_watch_interval_seconds", 60)
    wiki.watch(wiki_watch_interval, stop_event)

    # Background: Ollama health check
    threading.Thread(
        target=ollama_health_check, args=(wiki, stop_event), daemon=True
    ).start()

    # Background: cache flush + memory cleanup
    threading.Thread(
        target=maintenance_worker, args=(router, stop_event), daemon=True
    ).start()

    # Background: sensor feed watcher
    fact_store.watch(stop_event)

    # Signal handling: stop the loops; cleanup runs below, on the main thread.
    def shutdown(sig, frame):
        log.info("shutting down...")
        stop_event.set()
        dispatcher.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    dispatcher.start()
    log.info("listening...")
    dispatcher.run(inbox)  # returns after shutdown()

    router.flush_cache()
    mesh_iface.close()


# ─────────────────────────── Entry point ──────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Del-Fi — offline AI oracle for LoRa mesh networks"
    )
    parser.add_argument(
        "--config", "-c",
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--simulator", "-s",
        action="store_true",
        help="Run in simulator mode (stdin/stdout, no radio required)",
    )
    parser.add_argument(
        "--build-wiki",
        action="store_true",
        help="Compile knowledge/ into wiki/ pages and exit",
    )
    parser.add_argument(
        "--lint-wiki",
        action="store_true",
        help="Check wiki health and exit",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Open the web-based configuration and management GUI",
    )
    parser.add_argument(
        "--gui-port",
        type=int,
        default=5174,
        metavar="PORT",
        help="Port for the GUI server (default: 5174)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Start GUI without opening a browser window",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg["log_level"], simulator=args.simulator)
    log.info(f"del-fi v{VERSION} starting")

    if args.build_wiki:
        run_build_wiki(cfg)

    if args.lint_wiki:
        run_lint_wiki(cfg)

    if args.gui:
        from del_fi.gui import launch
        launch(cfg, args.config or "config.yaml",
               port=args.gui_port, open_browser=not args.no_browser)
        return

    run_daemon(cfg, simulator=args.simulator)


if __name__ == "__main__":
    main()
