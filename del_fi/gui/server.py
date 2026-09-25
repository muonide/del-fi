"""Del-Fi GUI — Flask web server for oracle configuration and management.

Provides a browser-based control panel for configuring, testing, and
managing a Del-Fi oracle deployment.

Launch via:
    python main.py --gui [--config PATH] [--gui-port 5174]

Security: the server binds to 127.0.0.1 only. Every request must carry a
loopback Host header (defeats DNS rebinding), and state-changing requests
must be JSON from a same-origin page (defeats cross-site requests from
other tabs in the operator's browser).
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

import yaml

from del_fi import __version__
from del_fi.config import ConfigError, read_config
from del_fi.core.fsutil import write_atomic

log = logging.getLogger("del_fi.gui")

# Project root is three levels up: del_fi/gui/server.py → del_fi/gui → del_fi → project root
_PROJECT_ROOT = Path(__file__).parent.parent.parent

_LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")
_MAX_SIM_TEXT = 500
_MAX_BUILD_OUTPUT = 20000


def allowed_hosts(port: int) -> set[str]:
    """Host header values the GUI accepts: loopback names, with or without port."""
    return {h for name in _LOOPBACK_NAMES for h in (name, f"{name}:{port}")}


def simulator_config(cfg: dict) -> dict:
    """Config for the GUI's chat simulator: same knowledge, sandboxed state.

    Answers come from the real wiki, but the simulator's response cache,
    board, memory and seen-senders live under cache/gui-simulator/, so
    testing from the GUI never leaks into what radio users receive.
    """
    sim = dict(cfg)
    sim_dir = os.path.join(cfg["_cache_dir"], "gui-simulator")
    sim["_cache_dir"] = sim_dir
    sim["_gossip_dir"] = os.path.join(sim_dir, "gossip")
    sim["_seen_senders_file"] = os.path.join(sim_dir, "seen_senders.txt")
    sim["fact_feed_file"] = cfg.get("fact_feed_file") or os.path.join(
        cfg["_cache_dir"], "sensor_feed.json"
    )
    sim["wiki_watch_enabled"] = False
    return sim


def create_app(cfg: dict, config_path: str, port: int = 5174):
    """Build and return the Flask application."""
    try:
        from flask import Flask, abort, jsonify, render_template, request
    except ImportError:
        print(
            "\n[del-fi] flask is required for --gui.\n"
            "  pip install flask\n",
            file=sys.stderr,
        )
        sys.exit(1)

    template_dir = str(Path(__file__).parent / "templates")
    app = Flask(__name__, template_folder=template_dir)
    app.config["JSON_SORT_KEYS"] = False
    hosts = allowed_hosts(port)

    _state: dict = {
        "cfg": cfg,
        "config_path": str(config_path),
        "start_time": time.time(),
        "_router": None,
        "_router_lock": threading.Lock(),
        "_router_stale": False,
    }
    _build: dict = {"proc": None, "output": "", "started": 0.0, "returncode": None}
    _build_lock = threading.Lock()

    # ── Request guard ─────────────────────────────────────────────────────

    @app.before_request
    def _guard():
        if request.host not in hosts:
            abort(403)  # DNS rebinding: another domain resolving to 127.0.0.1
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc not in hosts:
            abort(403)
        if not request.is_json:
            # A JSON body forces a CORS preflight from any other origin,
            # which this server never approves.
            abort(415)
        return None

    # ── Lazy Router for Simulator ──────────────────────────────────────────

    def _get_router():
        with _state["_router_lock"]:
            if _state["_router"] is None or _state["_router_stale"]:
                from del_fi.core.facts import FactStore
                from del_fi.core.knowledge import WikiEngine
                from del_fi.core.peers import GossipDirectory, PeerCache
                from del_fi.core.router import Router
                c = simulator_config(_state["cfg"])
                for d in (c["_cache_dir"], c["_gossip_dir"], c["wiki_folder"]):
                    os.makedirs(d, exist_ok=True)
                facts = FactStore(c)
                facts._poll_feed_file()
                _state["_router"] = Router(
                    c, WikiEngine(c), PeerCache(c), GossipDirectory(c), fact_store=facts,
                )
                _state["_router_stale"] = False
        return _state["_router"]

    # ── Helper: run main.py subcommand ────────────────────────────────────

    def _main_cmd(*args) -> list[str]:
        return [sys.executable, str(_PROJECT_ROOT / "main.py"), *args,
                "--config", _state["config_path"]]

    def _run_main(*args, timeout: int = 30) -> dict:
        try:
            r = subprocess.run(
                _main_cmd(*args),
                capture_output=True, text=True, timeout=timeout,
                cwd=str(_PROJECT_ROOT),
            )
            return {
                "ok": r.returncode == 0,
                "stdout": r.stdout,
                "stderr": r.stderr,
                "returncode": r.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "timed out"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _collect_build_output(proc: subprocess.Popen):
        for line in proc.stdout:
            with _build_lock:
                _build["output"] = (_build["output"] + line)[-_MAX_BUILD_OUTPUT:]
        proc.wait()
        with _build_lock:
            _build["returncode"] = proc.returncode

    # ── Routes ────────────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/status")
    def api_status():
        c = _state["cfg"]
        ollama_ok = False
        ollama_models: list = []
        try:
            from ollama import Client
            cl = Client(host=c["ollama_host"], timeout=5)
            resp = cl.list()
            ollama_ok = True
            ollama_models = [m.model for m in (resp.models or [])]
        except Exception:
            pass

        wiki_dir = Path(c["wiki_folder"])
        wiki_pages = sorted(
            f.stem for f in wiki_dir.glob("*.md")
            if f.name not in ("index.md", "log.md")
        ) if wiki_dir.exists() else []

        knowledge_dir = Path(c.get("knowledge_folder", "./knowledge"))
        knowledge_files = sorted(
            f.name for f in knowledge_dir.iterdir()
            if f.is_file() and f.suffix in (".md", ".txt")
        ) if knowledge_dir.exists() else []

        return jsonify({
            "node_name": c.get("node_name", "UNNAMED"),
            "model": c.get("model", ""),
            "wiki_builder_model": c.get("wiki_builder_model") or "",
            "oracle_type": c.get("oracle_type", ""),
            "ollama_host": c.get("ollama_host", ""),
            "ollama_ok": ollama_ok,
            "ollama_models": ollama_models,
            "wiki_pages": wiki_pages,
            "wiki_page_count": len(wiki_pages),
            "knowledge_files": knowledge_files,
            "knowledge_file_count": len(knowledge_files),
            "wiki_folder": str(wiki_dir),
            "knowledge_folder": str(knowledge_dir),
            "uptime_s": int(time.time() - _state["start_time"]),
            "version": __version__,
            "config_path": _state["config_path"],
        })

    @app.route("/api/config", methods=["GET"])
    def api_config_get():
        try:
            with open(_state["config_path"], "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            return jsonify({"ok": True, "config": raw if isinstance(raw, dict) else {}})
        except FileNotFoundError:
            return jsonify({"ok": True, "config": {}})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/config", methods=["POST"])
    def api_config_post():
        """Merge the form's values into config.yaml, validate, then save.

        Keys the form manages are replaced (or removed when cleared); every
        other key in the file — mesh_knowledge, meshcore, board filters — is
        kept. The previous file is saved as config.yaml.bak. Nothing is
        written unless the result passes the same validation as the daemon.
        """
        body = request.get_json(silent=True) or {}
        posted = body.get("config")
        if not isinstance(posted, dict):
            return jsonify({"ok": False, "error": "request needs a config object"}), 400
        managed = body.get("managed_keys")
        managed = {str(k) for k in managed} if isinstance(managed, list) else set(posted)
        posted = {str(k): v for k, v in posted.items() if not str(k).startswith("_")}
        if not str(posted.get("node_name", "")).strip():
            return jsonify({"ok": False, "error": "node_name is required"}), 400
        if not str(posted.get("model", "")).strip():
            return jsonify({"ok": False, "error": "model is required"}), 400

        path = Path(_state["config_path"])
        current: dict = {}
        if path.exists():
            try:
                loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                current = loaded if isinstance(loaded, dict) else {}
            except yaml.YAMLError:
                current = {}  # unreadable: replaced, but kept in the .bak
        merged = {k: v for k, v in current.items() if k not in managed}
        merged.update(posted)
        text = yaml.safe_dump(
            merged, default_flow_style=False, allow_unicode=True, sort_keys=False, width=80,
        )

        # Validate a copy next to the real file, so relative paths resolve
        # exactly as they will for the daemon.
        check = path.with_name(f".{path.name}.gui-check")
        try:
            check.write_text(text, encoding="utf-8")
            new_cfg = read_config(str(check))
        except ConfigError as e:
            return jsonify({"ok": False, "error": str(e).replace(str(check), str(path))}), 400
        finally:
            try:
                check.unlink()
            except OSError:
                pass

        backup = None
        if path.exists():
            backup = path.with_name(path.name + ".bak")
            shutil.copy2(path, backup)
        if not write_atomic(str(path), text):
            return jsonify({"ok": False, "error": f"could not write {path}"}), 500
        new_cfg["_config_path"] = str(path.resolve())
        _state["cfg"] = new_cfg
        _state["_router_stale"] = True
        return jsonify({"ok": True, "backup": str(backup) if backup else None})

    @app.route("/api/wiki/pages")
    def api_wiki_pages():
        import re
        wiki_dir = Path(_state["cfg"]["wiki_folder"])
        pages = []
        if wiki_dir.exists():
            for f in sorted(wiki_dir.glob("*.md")):
                if f.name in ("index.md", "log.md"):
                    continue
                meta: dict = {
                    "slug": f.stem,
                    "title": f.stem.replace("-", " ").title(),
                    "tags": "", "last_ingested": "", "size": 0,
                }
                try:
                    text = f.read_text(encoding="utf-8")
                    meta["size"] = len(text)
                    m = re.search(r"^title:\s*(.+)$", text, re.MULTILINE)
                    if m:
                        meta["title"] = m.group(1).strip()
                    m = re.search(r"^tags:\s*\[(.+)\]$", text, re.MULTILINE)
                    if m:
                        meta["tags"] = m.group(1).strip()
                    m = re.search(r"^last_ingested:\s*(.+)$", text, re.MULTILINE)
                    if m:
                        meta["last_ingested"] = m.group(1).strip()
                except Exception:
                    pass
                pages.append(meta)
        return jsonify({"ok": True, "pages": pages})

    @app.route("/api/wiki/page/<slug>")
    def api_wiki_page(slug: str):
        import re
        slug = re.sub(r"[^a-zA-Z0-9_-]", "", slug)
        page_path = Path(_state["cfg"]["wiki_folder"]) / f"{slug}.md"
        if not page_path.exists():
            return jsonify({"ok": False, "error": "page not found"}), 404
        return jsonify({
            "ok": True,
            "content": page_path.read_text(encoding="utf-8"),
            "slug": slug,
        })

    @app.route("/api/wiki/log")
    def api_wiki_log():
        p = Path(_state["cfg"]["wiki_folder"]) / "log.md"
        content = p.read_text(encoding="utf-8") if p.exists() else ""
        return jsonify({"ok": True, "content": content})

    @app.route("/api/wiki/build", methods=["POST"])
    def api_wiki_build():
        """Start --build-wiki in the background; poll /api/wiki/build/status.

        A build can take many minutes with a large model, far longer than a
        request should stay open, and a timed-out request used to kill it.
        """
        with _build_lock:
            if _build["proc"] is not None and _build["returncode"] is None:
                return jsonify({"ok": True, "running": True, "already_running": True})
            try:
                proc = subprocess.Popen(
                    _main_cmd("--build-wiki"),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, cwd=str(_PROJECT_ROOT),
                )
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 500
            _build.update(proc=proc, output="", started=time.time(), returncode=None)
        threading.Thread(target=_collect_build_output, args=(proc,), daemon=True).start()
        return jsonify({"ok": True, "running": True})

    @app.route("/api/wiki/build/status")
    def api_wiki_build_status():
        with _build_lock:
            started = _build["proc"] is not None
            running = started and _build["returncode"] is None
            return jsonify({
                "ok": True,
                "started": started,
                "running": running,
                "returncode": _build["returncode"],
                "output": _build["output"],
                "elapsed_s": int(time.time() - _build["started"]) if started else 0,
            })

    @app.route("/api/wiki/lint", methods=["POST"])
    def api_wiki_lint():
        result = _run_main("--lint-wiki", timeout=60)
        return jsonify(result)

    @app.route("/api/knowledge/files")
    def api_knowledge_files():
        kd = Path(_state["cfg"].get("knowledge_folder", "./knowledge"))
        files = []
        if kd.exists():
            for f in sorted(kd.iterdir()):
                if f.is_file() and f.suffix in (".md", ".txt"):
                    files.append({
                        "name": f.name,
                        "size": f.stat().st_size,
                        "modified": int(f.stat().st_mtime),
                    })
        return jsonify({"ok": True, "files": files, "folder": str(kd)})

    @app.route("/api/simulate", methods=["POST"])
    def api_simulate():
        body = request.get_json(silent=True) or {}
        sender = str(body.get("sender", "!gui00000")).strip()[:20] or "!gui00000"
        text = str(body.get("text", "")).strip()[:_MAX_SIM_TEXT]
        if not text:
            return jsonify({"ok": False, "error": "empty message"}), 400
        try:
            router = _get_router()
            messages = router.route_multi(sender, text)
            return jsonify({"ok": True, "responses": messages or []})
        except Exception as e:
            log.exception("simulate error")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/board")
    def api_board():
        """The live board (read-only; the simulator's board is sandboxed)."""
        board_path = Path(_state["cfg"]["_cache_dir"]) / "board.json"
        if not board_path.exists():
            return jsonify({"ok": True, "posts": []})
        try:
            data = json.loads(board_path.read_text(encoding="utf-8"))
            posts = data.get("posts", []) if isinstance(data, dict) else []
            return jsonify({"ok": True, "posts": [p for p in posts if isinstance(p, dict)]})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/board/post", methods=["POST"])
    def api_board_post():
        """Post to the live board as the operator (same rules as !post)."""
        from del_fi.core.board import Board
        c = _state["cfg"]
        if not c.get("board_enabled"):
            return jsonify({"ok": False, "error": "The board is off (set board_enabled: true)."}), 400
        body = request.get_json(silent=True) or {}
        sender = str(body.get("sender") or "!gui00000").strip()[:20] or "!gui00000"
        text = str(body.get("text", ""))
        with _state["_router_lock"]:
            board = _state.get("_live_board")
            if board is None or _state.get("_live_board_cfg") is not c:
                board = Board(c)
                _state["_live_board"], _state["_live_board_cfg"] = board, c
        return jsonify({"ok": True, "result": board.post(sender, text)})

    @app.route("/api/logs")
    def api_logs():
        try:
            n = min(max(int(request.args.get("lines", 100)), 10), 500)
        except ValueError:
            n = 100
        c = _state["cfg"]
        log_path = Path(c.get("log_file") or os.path.join(c.get("_config_dir", "."), "del_fi.log"))
        if not log_path.exists():
            return jsonify({"ok": True, "lines": [], "file": str(log_path)})
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
            return jsonify({
                "ok": True,
                "lines": text.splitlines()[-n:],
                "file": str(log_path),
            })
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    return app


def launch(cfg: dict, config_path: str, port: int = 5174, open_browser: bool = True):
    """Start the GUI server and optionally open the browser."""
    app = create_app(cfg, config_path, port=port)
    url = f"http://127.0.0.1:{port}"
    print(f"\n  ·· DEL-FI GUI ··  {url}\n  config: {config_path}\n  Ctrl+C to stop\n")
    log.info(f"GUI server at {url}")

    if open_browser:
        def _open():
            time.sleep(0.8)
            webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    # Localhost-only; never bind to 0.0.0.0 (remote SSH users: use port forwarding)
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
