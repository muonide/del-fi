"""Meshtastic mesh adapter.

Connects to a Meshtastic radio via serial, TCP, or BLE, forwards incoming
direct messages (and DEL-FI gossip broadcasts) to the dispatcher, and
keeps the link alive: a supervisor thread reconnects whenever the radio
drops (USB unplugged, TCP reset, radio reboot).
"""

import collections
import logging
import queue
import threading
import time

from del_fi.core.formatter import chunk_text
from del_fi.mesh.base import MeshAdapter

log = logging.getLogger("del_fi.mesh.meshtastic")

BROADCAST_NUM = 0xFFFFFFFF
BROADCAST_ADDR = "^all"
DEFAULT_TCP_PORT = 4403
GOSSIP_PREFIX = "DEL-FI:"

RECEIVE_TOPIC = "meshtastic.receive.text"
LOST_TOPIC = "meshtastic.connection.lost"

RECONNECT_MIN_DELAY = 10
RECONNECT_MAX_DELAY = 120
SUPERVISOR_POLL = 5


def parse_tcp_address(address: str) -> tuple[str, int]:
    """'host', 'host:4404', '[fe80::1]:4404' -> (host, port)."""
    address = address.strip()
    if address.startswith("["):  # [ipv6]:port
        host, _, rest = address[1:].partition("]")
        port = rest.lstrip(":")
        return host, int(port) if port.isdigit() else DEFAULT_TCP_PORT
    if address.count(":") == 1:
        host, port = address.split(":")
        if port.isdigit():
            return host, int(port)
    return address, DEFAULT_TCP_PORT


class MeshtasticAdapter(MeshAdapter):
    """Real Meshtastic radio interface."""

    protocol_name = "Meshtastic"

    def __init__(self, cfg: dict, msg_queue: queue.Queue):
        super().__init__(cfg, msg_queue)
        self.interface = None
        self.my_node_id: str | None = None
        self._seen_ids: collections.deque = collections.deque(maxlen=500)
        self._lock = threading.Lock()
        self._connected = False
        self._stop = threading.Event()
        self._subscribed = False
        self._want_ack: bool = bool(cfg.get("want_ack", True))

    # --- Connection ---

    def connect(self) -> bool:
        """Connect to the Meshtastic radio. Returns True on success."""
        conn = self.cfg["radio_connection"]
        port = self.cfg["radio_port"]
        try:
            self._close_interface()
            self.interface = self._open_interface(conn, port)

            node_info = self.interface.getMyNodeInfo()
            if node_info is not None:
                self.my_node_id = node_info.get("user", {}).get("id", None)

            if not self._subscribed:
                self._subscribe(self._on_receive, RECEIVE_TOPIC)
                self._subscribe(self._on_connection_lost, LOST_TOPIC)
                self._subscribed = True

            self._connected = True
            log.info(f"radio connected via {conn} ({port})")
            return True

        except Exception as e:
            log.error(f"radio connection failed: {e}")
            self._connected = False
            return False

    def _open_interface(self, conn: str, port: str):
        """Create the meshtastic interface object (seam for tests)."""
        if conn == "serial":
            from meshtastic.serial_interface import SerialInterface
            return SerialInterface(devPath=port)
        if conn == "tcp":
            from meshtastic.tcp_interface import TCPInterface
            host, tcp_port = parse_tcp_address(port)
            return TCPInterface(hostname=host, portNumber=tcp_port)
        if conn == "ble":
            from meshtastic.ble_interface import BLEInterface
            return BLEInterface(address=port)
        raise ValueError(f"unknown radio_connection {conn!r} (serial, tcp or ble)")

    def _subscribe(self, listener, topic: str):
        from pubsub import pub
        pub.subscribe(listener, topic)

    def _unsubscribe(self, listener, topic: str):
        from pubsub import pub
        pub.unsubscribe(listener, topic)

    def _on_connection_lost(self, interface=None):
        """pubsub callback: the meshtastic library lost the radio."""
        if interface is not None and interface is not self.interface:
            return  # a stale interface we already replaced
        if self._connected:
            log.warning("radio connection lost — reconnecting")
        self._connected = False

    def reconnect_loop(self):
        """Supervisor thread: reconnect with backoff whenever the link is down.

        Runs for the life of the daemon, so a radio that drops after
        startup comes back on its own instead of leaving a deaf process.
        """
        delay = RECONNECT_MIN_DELAY
        while not self._stop.is_set():
            if self._connected:
                delay = RECONNECT_MIN_DELAY
                self._stop.wait(SUPERVISOR_POLL)
                continue
            log.info("attempting radio reconnect...")
            if self.connect():
                delay = RECONNECT_MIN_DELAY
                continue
            self._stop.wait(delay)
            delay = min(delay * 2, RECONNECT_MAX_DELAY)

    @property
    def connected(self) -> bool:
        return self._connected

    def close(self):
        self._stop.set()
        self._connected = False
        if self._subscribed:
            for listener, topic in (
                (self._on_receive, RECEIVE_TOPIC),
                (self._on_connection_lost, LOST_TOPIC),
            ):
                try:
                    self._unsubscribe(listener, topic)
                except Exception:
                    pass
            self._subscribed = False
        self._close_interface()

    def _close_interface(self):
        if self.interface is not None:
            try:
                self.interface.close()
            except Exception:
                pass
            self.interface = None

    # --- Receiving ---

    def _on_receive(self, packet, interface=None):
        """Callback fired by meshtastic pub/sub on incoming text."""
        try:
            sender = packet.get("fromId", "")
            text = packet.get("decoded", {}).get("text", "")
            msg_id = packet.get("id")
            to = packet.get("to", 0)

            if not sender or not text:
                return

            if sender == self.my_node_id:
                return

            if msg_id:
                with self._lock:
                    if msg_id in self._seen_ids:
                        return
                    self._seen_ids.append(msg_id)

            if to == BROADCAST_NUM:
                # Channel chatter is not for us — except Del-Fi gossip
                # announcements, which are broadcast by design.
                if text.startswith(GOSSIP_PREFIX):
                    log.debug(f"← gossip broadcast from {sender}")
                    self.msg_queue.put((sender, text.strip()))
                else:
                    log.debug(f"← broadcast from {sender} ignored")
                return

            log.info(f'← {sender}: "{text[:80]}"')
            self.msg_queue.put((sender, text.strip()))

        except Exception:
            log.exception("error handling incoming message")

    # --- Sending ---

    def send_dm(self, dest_id: str, text: str) -> bool:
        """Send a direct message to a node. Chunks if necessary."""
        if not self._connected or not self.interface:
            log.warning(f"can't send to {dest_id}: radio not connected")
            return False

        max_bytes = self.cfg["max_response_bytes"]
        if len(text.encode("utf-8")) <= max_bytes:
            return self._send_one(dest_id, text)

        chunks = chunk_text(text, max_bytes)
        for i, chunk in enumerate(chunks):
            if not self._send_one(dest_id, chunk):
                return False
            if i < len(chunks) - 1:
                time.sleep(3)  # inter-chunk delay to avoid flooding

        return True

    def send_broadcast(self, text: str, channel_index: int = 0) -> bool:
        """Broadcast on a channel (used for gossip announcements)."""
        if not self._connected or not self.interface:
            log.warning("can't broadcast: radio not connected")
            return False
        try:
            self.interface.sendText(
                text, destinationId=BROADCAST_ADDR, channelIndex=channel_index
            )
            log.info(f"  ✓ broadcast {len(text.encode('utf-8'))} bytes on channel {channel_index}")
            return True
        except Exception:
            log.exception("broadcast failed")
            return False

    def _send_one(self, dest_id: str, text: str) -> bool:
        try:
            # wantAck makes the firmware retry DMs across hops until the
            # destination acknowledges — what the Meshtastic apps do.
            self.interface.sendText(text, destinationId=dest_id, wantAck=self._want_ack)
            log.info(f"  ✓ sent {len(text.encode('utf-8'))} bytes → {dest_id}")
            return True
        except Exception:
            log.exception(f"send failed to {dest_id}")
            return False
