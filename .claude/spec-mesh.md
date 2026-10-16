# Del-Fi — Mesh Adapter Specification

<!-- Parent: .claude/claude.md §6 -->
<!-- Related: spec-config.md §mesh keys -->

---

## 1. MeshAdapter ABC

All radio adapters implement `MeshAdapter` from `del_fi/mesh/base.py`.
Adapters are deliberately thin: they move text between the radio and the
`Dispatcher` (`del_fi/core/dispatcher.py`), which owns rate limiting,
queueing and replies for every protocol.

```python
class MeshAdapter(ABC):
    def __init__(self, cfg: dict, msg_queue: queue.Queue): ...

    @abstractmethod
    def connect(self) -> bool:
        """Open the radio. Returns True on success; catches its own errors.
        Safe to call again to reconnect (closes the previous link first)."""

    @abstractmethod
    def send_dm(self, dest_id: str, text: str) -> bool:
        """Send a direct message. Returns True if handed to the radio.
        Never raises: failures are logged and reported as False."""

    @abstractmethod
    def close(self) -> None:
        """Release the radio and stop reconnect_loop(). Must not raise."""

    def send_broadcast(self, text: str, channel_index: int = 0) -> bool:
        """Optional: broadcast on a channel (gossip announcements).
        Default returns False (unsupported)."""

    def reconnect_loop(self) -> None:
        """Optional supervisor thread body, started once at daemon startup:
        keep the link up for the daemon's whole life. Default: no-op."""

    connected: bool        # property: link currently up
    protocol_name: str     # shown in the banner
```

Inbound messages are put on `msg_queue` as `(sender_id, text)`, where
`sender_id` is protocol-native (`"!a1b2c3d4"` for Meshtastic).

### Invariants

- `send_dm()` receives text already formatted to ≤ `max_response_bytes`;
  oversize text is split defensively, never silently truncated.
- Adapters never rate-limit or reply on their own — that is the
  dispatcher's job, so behaviour is identical across protocols.
- Dependency direction: `mesh/` may import helpers from `core/` (e.g.
  `formatter.chunk_text`); `core/` never imports `mesh/`.

---

## 2. Meshtastic Adapter

File: `del_fi/mesh/meshtastic_adapter.py`

### 2.1 Connection modes

| `radio_connection` | `radio_port` | When to use |
|--------------------|--------------|-------------|
| `serial` | `/dev/ttyUSB0`, `/dev/ttyACM0` | Direct USB/UART connection |
| `tcp` | `host`, `host:port`, `[ipv6]:port` (default port 4403) | Wi-Fi or networked radio |
| `ble` | BLE address | Bluetooth (slower) |

`want_ack` (default `true`) sends DMs with `wantAck`, so the firmware
retries each chunk across hops until the destination acknowledges it —
what the Meshtastic apps do for DMs.

### 2.2 Receiving

The adapter subscribes (once) to three meshtastic pub/sub topics:

| Topic | Handler |
|-------|---------|
| `meshtastic.receive.text` | `_on_receive(packet, interface)` |
| `meshtastic.receive.routing` | `_on_routing(packet, interface)` → delivery reports (§2.7) |
| `meshtastic.connection.lost` | `_on_connection_lost(interface)` → marks the link down |

`_on_receive` drops packets with no sender or text, the node's own
messages, and duplicate packet IDs, then:

- **Direct message** → queued for the dispatcher.
- **Broadcast starting with `DEL-FI:`** → queued (gossip announcement).
- **Any other broadcast** → ignored (channel chatter is not for us).

### 2.3 Rate limiting

Adapters do **not** rate-limit. Rate limiting is protocol-agnostic and lives
in the `Dispatcher`, so every adapter gets the same behaviour: one question
per `rate_limit_seconds` per sender, commands exempt (except `!retry`), and
one "slow down" reply per window instead of a silent drop. See
`.claude/spec-router.md §5`.

### 2.4 Message deduplication

Meshtastic can deliver the same packet more than once (mesh flooding). The
last 500 packet IDs are kept in a `deque`; repeats are dropped. Packets
without an ID are never deduplicated. In-memory only.

### 2.5 Outbound pacing

The dispatcher pauses 0.5 s between the chunks of one reply; the radio's
own transmit queue and duty-cycle limits handle the rest. If `send_dm()`
is ever handed text over `max_response_bytes`, it splits it and waits 3 s
between pieces.

### 2.6 Supervisor (reconnect loop)

`main.py` always starts `reconnect_loop()` in a thread for real radios —
not only when the first `connect()` fails — so a radio that drops later
(USB unplugged, TCP reset, radio reboot) comes back on its own:

```
while not stopped:
    if connected: report expired delivery checks (§2.7); wait 5 s; continue
    connect()  (closes the old interface first)
    on failure: wait 10 s, doubling to at most 120 s
```

`close()` sets the stop event, unsubscribes and closes the interface.

### 2.7 Delivery reports

With `want_ack: true`, every DM part sent is remembered by packet ID (the
latest 256) until its fate is known, and each outcome is logged:

| Log line | Meaning |
|----------|---------|
| `→ sent 201B to !id` | handed to the radio |
| `↪ relayed toward !id after 4.1s (implicit ACK)` | our radio heard a neighbour rebroadcast it; the firmware stops retrying. Logged once. |
| `✓ delivered to !id in 6.2s (201B)` | the destination's ACK arrived |
| `✗ not delivered to !id: MAX_RETRANSMIT after 38.0s (201B)` | a NAK, with the firmware's reason (`NO_ROUTE`, `TOO_LARGE`, `PKI_UNKNOWN_PUBKEY`, ...) |
| `? 201B to !id: relayed, but no ACK from !id within 180s` | relayed but never confirmed, or neither ACK nor NAK arrived |

Routing packets are told apart by `from`: an ACK (`errorReason` absent or
`NONE`) from our own node number or ID is the implicit ACK; one from
anyone else is the destination's. Delivery tracking is logging only — no
message is resent by Del-Fi (the firmware already retries).

---

## 3. MeshCore Adapter

File: `del_fi/mesh/meshcore_adapter.py`

**Status: Stub.** The MeshCore Python library is not yet stable enough for
production integration. The adapter scaffolding is maintained so that a future
implementer can drop in the library calls without restructuring the codebase.

```python
class MeshCoreAdapter(MeshAdapter):
    
    def connect(self) -> None:
        raise NotImplementedError(
            "MeshCore adapter is a stub. "
            "Set mesh_type: meshtastic-serial in config to use a real radio."
        )
    
    def send_dm(self, dest: str, text: str) -> None:
        raise NotImplementedError("MeshCore adapter is a stub.")
    
    def close(self) -> None:
        pass  # no-op; never connected
```

When the MeshCore library stabilises, implement following the same pattern as
`MeshtasticAdapter`: pub/sub callback, dedup, rate limiting, reconnect loop.

---

## 4. Simulator Adapter

File: `del_fi/mesh/simulator.py`

Used for development and testing. Reads messages from stdin, writes to stdout.
No radio hardware required.

### 4.1 Message format

Input lines (stdin):

```
!a1b2c3d4> message text here
```

The prefix `!a1b2c3d4>` specifies the sender node ID. If omitted, the default
sender ID `!simulator` is used.

Output (stdout):

```
[RIDGELINE → !a1b2c3d4] Response text here
```

### 4.2 Colorized output

The simulator uses ANSI colors to distinguish participants. Colors are suppressed
if stdout is not a TTY (`sys.stdout.isatty()` is False).

| Role | Color | ANSI |
|------|-------|------|
| Outgoing (oracle) | Cyan | `\033[36m` |
| Incoming (user) | Yellow | `\033[33m` |
| System / info | Dim | `\033[2m` |

```
\033[2m[sim] Enter messages as: !nodeID> text  (or just: text)\033[0m
\033[33m!a1b2c3d4> what birds are common here?\033[0m
\033[36m[RIDGELINE → !a1b2c3d4] Common species: Clark's Nutcracker, Stellar's Jay,\033[0m
\033[36mAmerican Dipper. Peak activity May–Sept at elevation.\033[0m
```

### 4.3 Implementation notes

- The simulator reads from `sys.stdin` in the main thread and calls `self._callback()`.
- `send_dm()` writes to `sys.stdout`.
- The inter-chunk delay is suppressed in simulator mode (config override `chunk_delay_seconds: 0`).
- `KeyboardInterrupt` in simulator mode triggers clean shutdown via the normal
  SIGINT handler — the simulator does not catch it independently.

### 4.4 Usage

```bash
python main.py --simulator [--config PATH]

# Pipe mode (scripted testing)
echo "!test1> what is the current temperature?" | python main.py --simulator
```

---

## 5. Adapter Registration

The adapter is chosen by the `mesh_protocol` config key (`meshtastic` or
`meshcore`) through the registry in `del_fi/mesh/__init__.py`:

```python
ADAPTERS: dict[str, type[MeshAdapter]] = {
    "meshtastic": MeshtasticAdapter,   # radio_connection: serial | tcp | ble
    "meshcore": MeshCoreAdapter,       # stub
}
```

`--simulator` forces `SimulatorAdapter` regardless of `mesh_protocol`.

---

## 6. Adding a New Adapter

1. Create `del_fi/mesh/<name>_adapter.py`.
2. Subclass `MeshAdapter`; implement `connect()`, `send_dm()`, `close()`.
3. Implement `reconnect_loop()` so a dropped link recovers, and
   `send_broadcast()` if the protocol can broadcast (gossip).
4. Register it in `ADAPTERS` and add its name to `SUPPORTED_PROTOCOLS`
   in `del_fi/config.py`.
5. Put the hardware and library calls behind small methods (like
   `MeshtasticAdapter._open_interface` / `_subscribe`) so tests can
   replace them, and add tests in `tests/test_mesh.py` covering: connect
   and reconnect, `send_dm()` to the right destination, deduplication,
   and which broadcasts are forwarded.

---

<!-- End of spec-mesh.md -->
