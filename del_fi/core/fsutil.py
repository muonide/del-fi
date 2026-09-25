"""Filesystem helpers."""

import logging
import os
import threading

log = logging.getLogger("del_fi.core.fsutil")


def write_atomic(path: str, content: str) -> bool:
    """Write *content* to *path* via a temp file, fsync, and rename.

    A power cut never leaves a half-written file, and the temp name is
    unique per process and thread, so concurrent writers never trample
    each other's temp file. Returns True on success; failures are logged.
    """
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        log.exception(f"could not write {path}")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
