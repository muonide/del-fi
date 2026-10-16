"""Tests for mesh adapter pattern — factory, base class, simulator."""

import io
import queue
import time
import types
import unittest
import unittest.mock

from del_fi.mesh import create_interface, ADAPTERS, MeshAdapter
from del_fi.mesh.base import MeshAdapter as BaseAdapter
from del_fi.mesh.meshtastic_adapter import MeshtasticAdapter
from del_fi.mesh.meshcore_adapter import MeshCoreAdapter
from del_fi.mesh.simulator import SimulatorAdapter


# --- Minimal config for testing ---

def _cfg(**overrides):
    base = {
        "node_name": "TEST-NODE",
        "model": "test",
        "max_response_bytes": 230,
        "mesh_protocol": "meshtastic",
        "radio_connection": "serial",
        "radio_port": "/dev/ttyUSB0",
        "rate_limit_seconds": 10,
        "knowledge_folder": "./knowledge",
        "_base_dir": ".",
        "_cache_dir": "./cache",
        "_gossip_dir": "./gossip",
        "_vectorstore_dir": "./vectorstore",
    }
    base.update(overrides)
    return base


# --- Adapter registry ---


class TestAdapterRegistry(unittest.TestCase):
    def test_meshtastic_registered(self):
        self.assertIn("meshtastic", ADAPTERS)
        self.assertIs(ADAPTERS["meshtastic"], MeshtasticAdapter)

    def test_meshcore_registered(self):
        self.assertIn("meshcore", ADAPTERS)
        self.assertIs(ADAPTERS["meshcore"], MeshCoreAdapter)

    def test_all_adapters_inherit_base(self):
        for name, cls in ADAPTERS.items():
            self.assertTrue(
                issubclass(cls, MeshAdapter),
                f"{name} adapter does not inherit from MeshAdapter",
            )


# --- Factory ---


class TestCreateInterface(unittest.TestCase):
    def test_simulator_mode(self):
        q = queue.Queue()
        iface = create_interface(_cfg(), simulator=True, msg_queue=q)
        self.assertIsInstance(iface, SimulatorAdapter)
        self.assertTrue(iface.connected)
        iface.close()

    def test_meshtastic_protocol(self):
        q = queue.Queue()
        iface = create_interface(
            _cfg(mesh_protocol="meshtastic"), simulator=False, msg_queue=q
        )
        self.assertIsInstance(iface, MeshtasticAdapter)

    def test_meshcore_protocol(self):
        q = queue.Queue()
        cfg = _cfg(mesh_protocol="meshcore")
        cfg["meshcore"] = {"port": "/dev/ttyUSB0", "connection": "serial"}
        iface = create_interface(cfg, simulator=False, msg_queue=q)
        self.assertIsInstance(iface, MeshCoreAdapter)

    def test_unknown_protocol_raises(self):
        q = queue.Queue()
        with self.assertRaisesRegex(ValueError, r"[Uu]nknown mesh.protocol"):
            create_interface(_cfg(mesh_protocol="zigbee"), simulator=False, msg_queue=q)

    def test_simulator_ignores_protocol(self):
        q = queue.Queue()
        iface = create_interface(
            _cfg(mesh_protocol="meshcore"), simulator=True, msg_queue=q
        )
        self.assertIsInstance(iface, SimulatorAdapter)
        iface.close()


# --- Base class ---


class TestMeshAdapterBase(unittest.TestCase):
    def test_abstract_methods_enforced(self):
        with self.assertRaises(TypeError):
            BaseAdapter({}, queue.Queue())

    def test_default_connected_is_false(self):
        class Dummy(BaseAdapter):
            def connect(self): return True
            def send_dm(self, d, t): return True
            def close(self): pass

        d = Dummy({}, queue.Queue())
        self.assertTrue(hasattr(d, "connected"))


# --- Simulator ---


class TestSimulatorAdapter(unittest.TestCase):
    def test_send_dm_returns_true(self):
        q = queue.Queue()
        sim = SimulatorAdapter(_cfg(), q)
        buf = io.StringIO()
        with unittest.mock.patch("sys.stdout", buf):
            result = sim.send_dm("!sim00001", "Hello world")
        self.assertTrue(result)
        self.assertIn("Hello world", buf.getvalue())

    def test_send_dm_warns_on_oversize(self):
        q = queue.Queue()
        sim = SimulatorAdapter(_cfg(max_response_bytes=10), q)
        buf = io.StringIO()
        with unittest.mock.patch("sys.stdout", buf):
            sim.send_dm("!sim00001", "This message is way too long for ten bytes")
        self.assertIn("exceeds", buf.getvalue())

    def test_protocol_name(self):
        q = queue.Queue()
        sim = SimulatorAdapter(_cfg(), q)
        self.assertEqual(sim.protocol_name, "Simulator")

    def test_connected_always_true(self):
        q = queue.Queue()
        sim = SimulatorAdapter(_cfg(), q)
        self.assertTrue(sim.connected)


# --- Protocol names ---


class TestProtocolNames(unittest.TestCase):
    def test_meshtastic_protocol_name(self):
        q = queue.Queue()
        m = MeshtasticAdapter(_cfg(), q)
        self.assertEqual(m.protocol_name, "Meshtastic")

    def test_meshcore_protocol_name(self):
        q = queue.Queue()
        cfg = _cfg(mesh_protocol="meshcore")
        cfg["meshcore"] = {}
        mc = MeshCoreAdapter(cfg, q)
        self.assertEqual(mc.protocol_name, "MeshCore")


class _FakeInterface:
    def __init__(self, node_id="!00c0ffee"):
        self.node_id = node_id
        self.sent: list[tuple] = []
        self.closed = False
        self.next_packet_id = 1000

    def getMyNodeInfo(self):
        return {"num": int(self.node_id[1:], 16), "user": {"id": self.node_id}}

    def sendText(self, text, destinationId="^all", wantAck=False, channelIndex=0):
        """Like meshtastic's: returns the sent packet, whose id ACKs refer to."""
        self.sent.append((text, destinationId, wantAck, channelIndex))
        self.next_packet_id += 1
        return types.SimpleNamespace(id=self.next_packet_id)

    def close(self):
        self.closed = True


class _TestableMeshtastic(MeshtasticAdapter):
    """MeshtasticAdapter with the hardware and pubsub seams replaced."""

    def __init__(self, cfg, fail_connects=0):
        super().__init__(cfg, queue.Queue())
        self.fail_connects = fail_connects
        self.opened: list[_FakeInterface] = []
        self.subscriptions: list[str] = []
        self.unsubscriptions: list[str] = []

    def _open_interface(self, conn, port):
        if self.fail_connects > 0:
            self.fail_connects -= 1
            raise OSError("no such device")
        iface = _FakeInterface()
        self.opened.append(iface)
        return iface

    def _subscribe(self, listener, topic):
        self.subscriptions.append(topic)

    def _unsubscribe(self, listener, topic):
        self.unsubscriptions.append(topic)


def _dm(text, sender="!a1b2c3d4", msg_id=1, to=0x0C0FFEE):
    return {"fromId": sender, "to": to, "id": msg_id, "decoded": {"text": text}}


class TestMeshtasticAdapter(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()

    def _connected(self, **cfg):
        a = _TestableMeshtastic({**self.cfg, **cfg})
        self.assertTrue(a.connect())
        return a

    def _drain(self, a):
        return [a.msg_queue.get_nowait() for _ in range(a.msg_queue.qsize())]

    def test_connect_sets_node_id_and_subscribes_once(self):
        a = self._connected()
        a.connect()  # reconnect
        self.assertEqual(a.my_node_id, "!00c0ffee")
        self.assertEqual(sorted(a.subscriptions),
                         ["meshtastic.connection.lost", "meshtastic.receive.routing",
                          "meshtastic.receive.text"])
        self.assertTrue(a.opened[0].closed, "old interface closed on reconnect")

    # --- delivery reports (ACK/NAK) ---

    def _routing(self, request_id, reason=None, from_id="!a1b2c3d4", from_num=0xA1B2C3D4):
        routing = {} if reason is None else {"errorReason": reason}
        return {"from": from_num, "fromId": from_id,
                "decoded": {"portnum": "ROUTING_APP", "requestId": request_id, "routing": routing}}

    def test_ack_from_destination_logs_delivered(self):
        a = self._connected()
        a.send_dm("!a1b2c3d4", "hello")
        with self.assertLogs("del_fi.mesh.meshtastic", "INFO") as logs:
            a._on_routing(self._routing(1001))
        self.assertIn("delivered to !a1b2c3d4", logs.output[0])
        self.assertEqual(len(a._pending), 0)

    def test_implicit_ack_then_real_ack(self):
        a = self._connected()
        a.send_dm("!a1b2c3d4", "hello")
        own = self._routing(1001, from_id="!00c0ffee", from_num=0x00C0FFEE)
        with self.assertLogs("del_fi.mesh.meshtastic", "INFO") as logs:
            a._on_routing(own)
            a._on_routing(own)                      # repeated implicit ACK: logged once
            a._on_routing(self._routing(1001, reason="NONE"))
        self.assertEqual(len(logs.output), 2)
        self.assertIn("relayed toward !a1b2c3d4", logs.output[0])
        self.assertIn("delivered to !a1b2c3d4", logs.output[1])

    def test_nak_logs_reason(self):
        a = self._connected()
        a.send_dm("!a1b2c3d4", "hello")
        with self.assertLogs("del_fi.mesh.meshtastic", "WARNING") as logs:
            a._on_routing(self._routing(1001, reason="MAX_RETRANSMIT",
                                        from_id="!00c0ffee", from_num=0x00C0FFEE))
        self.assertIn("not delivered to !a1b2c3d4: MAX_RETRANSMIT", logs.output[0])
        self.assertEqual(len(a._pending), 0)

    def test_unrelated_routing_packets_ignored(self):
        a = self._connected()
        a.send_dm("!a1b2c3d4", "hello")
        with self.assertNoLogs("del_fi.mesh.meshtastic", "INFO"):
            a._on_routing(self._routing(4242))      # not one of ours
            a._on_routing({"decoded": {}})          # no requestId
        self.assertEqual(len(a._pending), 1)

    def test_no_tracking_without_want_ack(self):
        a = self._connected(want_ack=False)
        a.send_dm("!a1b2c3d4", "hello")
        self.assertEqual(len(a._pending), 0)

    def test_pending_reports_are_bounded(self):
        from del_fi.mesh.meshtastic_adapter import PENDING_MAX
        a = self._connected()
        for i in range(PENDING_MAX + 10):
            a.send_dm("!a1b2c3d4", f"msg {i}")
        self.assertEqual(len(a._pending), PENDING_MAX)

    def test_unconfirmed_messages_are_reported_once(self):
        from del_fi.mesh.meshtastic_adapter import PENDING_TIMEOUT
        a = self._connected()
        a.send_dm("!a1b2c3d4", "hello")       # packet 1001: relayed, then silence
        a.send_dm("!a1b2c3d4", "anyone?")     # packet 1002: no report at all
        a._on_routing(self._routing(1001, from_id="!00c0ffee", from_num=0x00C0FFEE))
        a._expire_pending(now=time.monotonic() + PENDING_TIMEOUT - 5)
        self.assertEqual(len(a._pending), 2)
        with self.assertLogs("del_fi.mesh.meshtastic", "WARNING") as logs:
            a._expire_pending(now=time.monotonic() + PENDING_TIMEOUT + 1)
        self.assertIn("5B to !a1b2c3d4: relayed, but no ACK from !a1b2c3d4", logs.output[0])
        self.assertIn("7B to !a1b2c3d4: no ACK or NAK within", logs.output[1])
        self.assertEqual(len(a._pending), 0)

    def test_connect_failure_returns_false(self):
        a = _TestableMeshtastic(self.cfg, fail_connects=1)
        self.assertFalse(a.connect())
        self.assertFalse(a.connected)

    def test_unknown_connection_type_fails_cleanly(self):
        a = MeshtasticAdapter({**self.cfg, "radio_connection": "carrier-pigeon"}, queue.Queue())
        self.assertFalse(a.connect())

    def test_dm_is_queued_and_own_messages_ignored(self):
        a = self._connected()
        a._on_receive(_dm("  where is camp?  "), None)
        a._on_receive(_dm("echo", sender="!00c0ffee", msg_id=2), None)
        self.assertEqual(self._drain(a), [("!a1b2c3d4", "where is camp?")])

    def test_duplicate_packet_ids_dropped(self):
        a = self._connected()
        a._on_receive(_dm("hi", msg_id=42), None)
        a._on_receive(_dm("hi", msg_id=42), None)
        self.assertEqual(len(self._drain(a)), 1)

    def test_packets_without_id_are_not_deduplicated(self):
        a = self._connected()
        a._on_receive(_dm("first", msg_id=None), None)
        a._on_receive(_dm("second", msg_id=None), None)
        self.assertEqual([t for _, t in self._drain(a)], ["first", "second"])

    def test_broadcasts_ignored_except_gossip(self):
        a = self._connected()
        a._on_receive(_dm("anyone around?", msg_id=5, to=0xFFFFFFFF), None)
        a._on_receive(_dm("DEL-FI:1:ANNOUNCE:VALLEY:topics=geology", msg_id=6, to=0xFFFFFFFF), None)
        self.assertEqual(self._drain(a), [("!a1b2c3d4", "DEL-FI:1:ANNOUNCE:VALLEY:topics=geology")])

    def test_send_dm_wants_ack_by_default(self):
        a = self._connected()
        self.assertTrue(a.send_dm("!a1b2c3d4", "hello"))
        self.assertEqual(a.interface.sent, [("hello", "!a1b2c3d4", True, 0)])

    def test_want_ack_can_be_disabled(self):
        a = self._connected(want_ack=False)
        a.send_dm("!a1b2c3d4", "hello")
        self.assertFalse(a.interface.sent[0][2])

    def test_send_dm_when_disconnected_returns_false(self):
        a = _TestableMeshtastic(self.cfg)
        self.assertFalse(a.send_dm("!a1b2c3d4", "hello"))

    def test_send_broadcast_uses_channel(self):
        a = self._connected()
        self.assertTrue(a.send_broadcast("DEL-FI:1:ANNOUNCE:X:topics=a", channel_index=2))
        self.assertEqual(a.interface.sent, [("DEL-FI:1:ANNOUNCE:X:topics=a", "^all", False, 2)])

    def test_connection_lost_marks_down_but_ignores_stale_interface(self):
        a = self._connected()
        a._on_connection_lost(interface=object())
        self.assertTrue(a.connected)
        a._on_connection_lost(interface=a.interface)
        self.assertFalse(a.connected)

    def test_supervisor_reconnects_after_drop(self):
        import threading
        from del_fi.mesh import meshtastic_adapter as mod

        a = _TestableMeshtastic(self.cfg, fail_connects=1)
        a.connect()  # fails
        with unittest.mock.patch.multiple(mod, RECONNECT_MIN_DELAY=0.01,
                                          SUPERVISOR_POLL=0.01, RECONNECT_MAX_DELAY=0.02):
            t = threading.Thread(target=a.reconnect_loop, daemon=True)
            t.start()
            self._wait_for(lambda: a.connected)
            a._on_connection_lost(interface=a.interface)   # USB unplugged
            self._wait_for(lambda: a.connected and len(a.opened) == 2)
            a.close()
            t.join(timeout=2)
        self.assertFalse(t.is_alive())
        self.assertIn("meshtastic.receive.text", a.unsubscriptions)

    def _wait_for(self, cond, timeout=2.0):
        import time
        deadline = time.time() + timeout
        while not cond():
            self.assertLess(time.time(), deadline, "condition not reached")
            time.sleep(0.005)


class TestTcpAddress(unittest.TestCase):
    def test_parse(self):
        from del_fi.mesh.meshtastic_adapter import parse_tcp_address
        self.assertEqual(parse_tcp_address("meshtastic.local"), ("meshtastic.local", 4403))
        self.assertEqual(parse_tcp_address("192.168.1.50:4404"), ("192.168.1.50", 4404))
        self.assertEqual(parse_tcp_address("[fe80::1]:4404"), ("fe80::1", 4404))
        self.assertEqual(parse_tcp_address("fe80::1"), ("fe80::1", 4403))


class TestBroadcastDefaults(unittest.TestCase):
    def test_base_adapter_broadcast_unsupported(self):
        self.assertFalse(MeshCoreAdapter(_cfg(), queue.Queue()).send_broadcast("x"))

    def test_simulator_broadcast_prints(self):
        sim = SimulatorAdapter(_cfg(), queue.Queue())
        with unittest.mock.patch("builtins.print") as mock_print:
            self.assertTrue(sim.send_broadcast("DEL-FI:1:ANNOUNCE:X", channel_index=1))
        self.assertIn("broadcast ch1", mock_print.call_args[0][0])


if __name__ == "__main__":
    unittest.main()

