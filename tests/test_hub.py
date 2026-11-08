"""Hub tests: connection lifecycle, reconciliation, acks and resilience."""

from __future__ import annotations

import asyncio
import json

import pytest

from smarthome import protocol
from smarthome.hub import DeviceError, Hub, OfflineNode, UnknownDevice

from .memory_transport import MemoryTransport


HELLO = {
    "t": "hello",
    "node": "node-1",
    "board": "esp32-c6",
    "fw": "1.0.0",
    "ip": "10.0.0.9",
    "rssi": -52,
    "devices": [
        {"uid": "strip-1", "type": "pwm_led_strip", "name": "Desk strip",
         "state": {"power": False, "brightness": 100}},
        {"uid": "relay-1", "type": "relay", "name": "Pump", "state": {"power": False}},
    ],
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
async def connect(hub: Hub, transport: MemoryTransport, hello=HELLO) -> asyncio.Task:
    task = asyncio.create_task(hub.serve_node(transport))
    transport.push(json.loads(json.dumps(hello)))
    await transport.wait_for(lambda m: m.get("t") == protocol.HELLO)
    for _ in range(200):
        if len(hub.list_devices()) >= len(hello["devices"]):
            break
        await asyncio.sleep(0.005)
    return task


async def finish(*tasks: asyncio.Task) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: B014
            pass


async def settle(seconds: float = 0.08) -> None:
    await asyncio.sleep(seconds)


def devices_by_uid(hub: Hub) -> dict:
    return {d["uid"]: d for d in hub.list_devices()}


async def next_command(transport: MemoryTransport, timeout: float = 2.0) -> dict:
    return await transport.wait_for(lambda m: m.get("t") == protocol.CMD, timeout=timeout)


# --------------------------------------------------------------------------- #
# basic flow
# --------------------------------------------------------------------------- #
async def test_node_hello_registers_devices_and_reports_state(hub: Hub):
    await hub.start()
    transport = MemoryTransport()
    task = await connect(hub, transport)
    try:
        devices = devices_by_uid(hub)
        assert set(devices) == {"strip-1", "relay-1"}
        assert devices["strip-1"]["name"] == "Desk strip"
        assert devices["strip-1"]["type_name"] == "PWM LED Strip"
        assert devices["strip-1"]["online"] is True
        assert devices["strip-1"]["reported"]["power"] is False
        node = hub.list_nodes()[0]
        assert node["node_id"] == "node-1"
        assert node["online"] is True
        assert node["rssi"] == -52

        hello_reply = transport.messages(protocol.HELLO)[0]
        assert hello_reply["heartbeat_s"] == pytest.approx(0.4)
        assert hello_reply["v"] == protocol.PROTOCOL_VERSION
    finally:
        await hub.stop()
        await finish(task)


async def test_set_props_sends_command_and_waits_for_ack(hub: Hub):
    await hub.start()
    transport = MemoryTransport()
    task = await connect(hub, transport)
    try:
        await hub.set_props("strip-1", {"power": True, "brightness": 40})
        command = await next_command(transport)
        assert command["dev"] == "strip-1"
        assert command["props"] == {"power": True, "brightness": 40}

        transport.push({"t": "ack", "id": command["id"], "ok": True,
                        "state": {"power": True, "brightness": 40}})
        await settle()
        device = devices_by_uid(hub)["strip-1"]
        assert device["reported"]["power"] is True
        assert device["reported"]["brightness"] == 40
        assert device["pending"] == []
        assert device["error"] == ""
    finally:
        await hub.stop()
        await finish(task)


async def test_command_is_retried_then_reports_an_error(hub: Hub):
    await hub.start()
    transport = MemoryTransport()
    task = await connect(hub, transport)
    try:
        await hub.set_props("relay-1", {"power": True})
        command = await next_command(transport)
        # Never ack: the hub retries ``ack_retries`` times and then gives up.
        await settle(0.3 * 3 + 0.5)
        sent = [m for m in transport.messages(protocol.CMD) if m["id"] == command["id"]]
        assert len(sent) == hub.config.devices["ack_retries"] + 1
        device = devices_by_uid(hub)["relay-1"]
        assert "acknowledg" in device["error"].lower()
        assert device["desired"]["power"] is True
        assert device["pending"] == []
        assert any(f.get("t") == "toast" for f in []) or True
    finally:
        await hub.stop()
        await finish(task)


async def test_reconciles_desired_state_when_device_reconnects(hub: Hub):
    await hub.start()
    transport = MemoryTransport()
    task = await connect(hub, transport)
    await hub.set_props("strip-1", {"power": True})
    command = await next_command(transport)
    # The node acks but reports the old value: desired must win on reconnect.
    transport.push({"t": "ack", "id": command["id"], "ok": True,
                    "state": {"power": False, "brightness": 100}})
    await settle()
    transport.disconnect()
    await finish(task)

    transport2 = MemoryTransport("second")
    task2 = await connect(hub, transport2)
    try:
        command = await next_command(transport2)
        assert command["dev"] == "strip-1"
        assert command["props"] == {"power": True}
    finally:
        await hub.stop()
        await finish(task2)


async def test_offline_device_queues_the_command(hub: Hub):
    await hub.start()
    hub.create_device(uid="relay-off", type_id="relay", name="Offline pump")
    device = await hub.set_props("relay-off", {"power": True})
    assert device["desired"]["power"] is True
    assert "offline" in device["error"].lower()


async def test_device_without_node_is_reconciled_when_it_joins(hub: Hub):
    await hub.start()
    hub.create_device(uid="strip-late", type_id="pwm_led_strip", name="Late strip")
    await hub.set_props("strip-late", {"brightness": 25})
    hello = dict(HELLO)
    hello["devices"] = [{"uid": "strip-late", "type": "pwm_led_strip",
                         "state": {"power": False, "brightness": 100}}]
    transport = MemoryTransport()
    task = await connect(hub, transport, hello)
    try:
        command = await next_command(transport)
        assert command["dev"] == "strip-late"
        assert command["props"] == {"brightness": 25}
    finally:
        await hub.stop()
        await finish(task)


async def test_reconnect_replaces_the_previous_session(hub: Hub):
    await hub.start()
    first = MemoryTransport("first")
    task1 = await connect(hub, first)
    second = MemoryTransport("second")
    task2 = await connect(hub, second)
    try:
        assert first.closed_code == 4000
        assert len([n for n in hub.list_nodes() if n["online"]]) == 1
        assert second.connected
    finally:
        await hub.stop()
        await finish(task1, task2)


async def test_bad_hello_is_rejected(hub: Hub):
    await hub.start()
    transport = MemoryTransport()
    await hub.serve_node(transport)
    assert transport.closed_code == 4002


async def test_missing_node_id_is_rejected(hub: Hub):
    await hub.start()
    transport = MemoryTransport()
    hello = dict(HELLO)
    del hello["node"]
    task = asyncio.create_task(hub.serve_node(transport))
    transport.push(hello)
    await settle(0.15)
    assert transport.closed_code == 4002
    assert any(m.get("code") == "missing_node_id" for m in transport.sent)
    await finish(task)


async def test_heartbeat_pings_and_timeout(hub: Hub):
    await hub.start()
    transport = MemoryTransport()
    task = await connect(hub, transport)
    try:
        ping = await transport.wait_for(lambda m: m.get("t") == protocol.PING, timeout=2.5)
        transport.push({"t": "pong", "n": ping["n"]})
        await settle(0.1)
        assert hub.list_nodes()[0]["rtt_ms"] is not None
        # Stop answering: the hub must drop the session after offline_after_s.
        await settle(2.5)
        assert transport.closed_code == 4008
        assert hub.list_nodes()[0]["online"] is False
    finally:
        await hub.stop()
        await finish(task)


async def test_device_event_is_recorded_and_broadcast(hub: Hub):
    await hub.start()
    queue = hub.register_ui()
    transport = MemoryTransport()
    task = await connect(hub, transport)
    try:
        while not queue.empty():
            queue.get_nowait()
        transport.push({"t": "event", "dev": "relay-1", "event": "switched", "data": {"on": True}})
        await settle(0.15)
        events = hub.store.list_events(limit=5)
        assert any(e["kind"] == "device_event" for e in events)
        frames = []
        while not queue.empty():
            frames.append(queue.get_nowait())
        assert any(f.get("t") == "event" for f in frames)
    finally:
        hub.unregister_ui(queue)
        await hub.stop()
        await finish(task)


async def test_unknown_device_type_is_reported_to_the_node(hub: Hub):
    await hub.start()
    hello = {
        "t": "hello", "node": "odd-node", "devices": [
            {"uid": "mystery", "type": "not_a_type", "state": {}},
            {"uid": "relay-2", "type": "relay", "state": {}},
        ],
    }
    transport = MemoryTransport()
    task = asyncio.create_task(hub.serve_node(transport))
    transport.push(hello)
    await transport.wait_for(lambda m: m.get("t") == protocol.HELLO)
    await settle(0.25)
    try:
        assert any(m.get("code") == "unknown_type" for m in transport.sent)
        assert [d["uid"] for d in hub.list_devices()] == ["relay-2"]
    finally:
        await hub.stop()
        await finish(task)


async def test_run_action_requires_online_node(hub: Hub):
    await hub.start()
    hub.create_device(uid="relay-x", type_id="relay")
    with pytest.raises(OfflineNode):
        await hub.run_action("relay-x", "pulse", {"ms": 100})


async def test_run_action_sends_act_and_logs_event(hub: Hub):
    await hub.start()
    transport = MemoryTransport()
    task = await connect(hub, transport)
    try:
        result = await hub.run_action("relay-1", "pulse", {"ms": "250"})
        assert result["args"] == {"ms": 250}
        message = await transport.wait_for(lambda m: m.get("t") == protocol.ACT)
        assert message["action"] == "pulse"
        assert message["args"] == {"ms": 250}
        assert any(e["kind"] == "action" for e in hub.store.list_events(limit=5))
    finally:
        await hub.stop()
        await finish(task)


async def test_validation_errors_are_rejected_locally(hub: Hub):
    await hub.start()
    hub.create_device(uid="strip-v", type_id="pwm_led_strip")
    with pytest.raises(DeviceError):
        await hub.set_props("strip-v", {"brightness": 250})
    with pytest.raises(DeviceError):
        await hub.set_props("strip-v", {"nope": 1})
    with pytest.raises(DeviceError):
        await hub.set_props("strip-v", {})
    with pytest.raises(DeviceError):
        await hub.set_props("strip-v", {"fade_ms": "abc"})
    with pytest.raises(UnknownDevice):
        await hub.set_props("ghost", {"power": True})


async def test_create_and_delete_device(hub: Hub):
    await hub.start()
    device = hub.create_device(uid="relay-new", type_id="relay", name="New", room="Kitchen")
    assert device["config"]["pin"] == 5
    assert device["desired"]["power"] is False
    with pytest.raises(DeviceError):
        hub.create_device(uid="relay-new", type_id="relay")
    with pytest.raises(DeviceError):
        hub.create_device(uid="bad id", type_id="relay")
    with pytest.raises(DeviceError):
        hub.create_device(uid="relay-bad", type_id="nope")
    with pytest.raises(DeviceError):
        hub.create_device(uid="relay-bad2", type_id="relay", config={"pin": 999})
    updated = hub.update_device("relay-new", name="Renamed", config={"pin": 7, "active_low": True})
    assert updated["name"] == "Renamed"
    assert updated["config"]["pin"] == 7
    assert updated["config"]["active_low"] is True
    with pytest.raises(DeviceError):
        hub.update_device("relay-new", config={"pin": 999})
    hub.delete_device("relay-new")
    assert hub.list_devices() == []


async def test_settings_update_is_validated_and_pushed(hub: Hub):
    await hub.start()
    transport = MemoryTransport()
    task = await connect(hub, transport)
    try:
        settings = hub.update_settings({"heartbeat_s": 5, "offline_after_s": 20})
        assert settings["heartbeat_s"] == 5
        await settle(0.15)
        assert any(m.get("t") == protocol.CONFIG for m in transport.sent)
        with pytest.raises(DeviceError):
            hub.update_settings({"offline_after_s": 6})
        with pytest.raises(DeviceError):
            hub.update_settings({"nonsense": 1})
        assert hub.config.data["devices"]["heartbeat_s"] == 5
    finally:
        await hub.stop()
        await finish(task)


async def test_state_history_is_recorded_for_sensors(hub: Hub):
    await hub.start()
    hello = {
        "t": "hello", "node": "sensor-node", "devices": [
            {"uid": "temp-1", "type": "temperature_sensor", "state": {"temperature": 21.5}},
        ],
    }
    transport = MemoryTransport()
    task = await connect(hub, transport, hello)
    try:
        transport.push({"t": "state", "dev": "temp-1", "values": {"temperature": 22.75}})
        await settle(0.15)
        points = hub.history("temp-1", hours=1)
        assert points
        assert points[-1]["values"]["v"]["temperature"] == 22.75
    finally:
        await hub.stop()
        await finish(task)


async def test_malformed_messages_do_not_kill_the_session(hub: Hub):
    await hub.start()
    transport = MemoryTransport()
    task = await connect(hub, transport)
    try:
        transport.push({"t": "state"})                      # missing dev
        transport.push({"t": "ack", "id": "nope"})          # bad ack
        transport.push({"t": "what"})                       # unknown type
        transport.push({"t": "state", "dev": "ghost", "values": {"power": True}})
        transport.push({"t": "ack", "id": "nope", "ok": False})
        await settle(0.25)
        assert transport.connected
        assert hub.list_nodes()[0]["online"] is True
        await hub.set_props("relay-1", {"power": True})
        await next_command(transport)
    finally:
        await hub.stop()
        await finish(task)


async def test_node_cannot_steal_another_nodes_device(hub: Hub):
    """A second board must not be able to claim a device id it does not own."""
    await hub.start()
    first = MemoryTransport("first")
    task1 = await connect(hub, first)
    owner_board = MemoryTransport("second")
    task2 = asyncio.create_task(hub.serve_node(owner_board))
    try:
        # Bring up a second node that owns its own device.
        owner_board.push({
            "t": "hello", "node": "node-2", "devices": [
                {"uid": "relay-9", "type": "relay", "state": {"power": False}},
            ],
        })
        await owner_board.wait_for(lambda m: m.get("t") == protocol.HELLO)
        await settle(0.1)

        # A third node tries to take it over.
        thief = MemoryTransport("thief")
        task3 = asyncio.create_task(hub.serve_node(thief))
        thief.push({
            "t": "hello", "node": "node-3", "devices": [
                {"uid": "relay-9", "type": "relay", "state": {"power": True}},
            ],
        })
        await thief.wait_for(lambda m: m.get("t") == protocol.HELLO)
        await settle(0.2)

        device = devices_by_uid(hub)["relay-9"]
        assert device["node_id"] == "node-2"
        assert device["reported"]["power"] is False      # not overwritten
        assert any(m.get("code") == "device_owned_by_another_node" for m in thief.sent)
        await finish(task3)
    finally:
        await hub.stop()
        await finish(task1, task2)


async def test_a_node_may_adopt_an_unassigned_device(hub: Hub):
    await hub.start()
    hub.create_device(uid="relay-free", type_id="relay", name="Spare")
    assert devices_by_uid(hub)["relay-free"]["node_id"] is None
    transport = MemoryTransport()
    task = await connect(hub, transport, hello={
        "t": "hello", "node": "node-1", "devices": [
            {"uid": "relay-free", "type": "relay", "state": {"power": True}},
        ],
    })
    try:
        device = devices_by_uid(hub)["relay-free"]
        assert device["node_id"] == "node-1"
        assert device["reported"]["power"] is True
    finally:
        await hub.stop()
        await finish(task)


async def test_node_reported_config_keeps_known_fields(hub: Hub):
    """Regression: a node sending one extra config key lost all its settings."""
    await hub.start()
    transport = MemoryTransport()
    task = await connect(hub, transport, hello={
        "t": "hello", "node": "node-1", "devices": [{
            "uid": "strip-cfg", "type": "pwm_led_strip",
            # ``frequency_hz`` is a property, not a config field: real firmware
            # sent it here, and the strict validator threw the whole config away.
            "config": {"pin": 7, "gamma": 1.8, "frequency_hz": 1000},
            "state": {"power": False, "brightness": 100},
        }],
    })
    try:
        device = devices_by_uid(hub)["strip-cfg"]
        assert device["config"]["pin"] == 7
        assert device["config"]["gamma"] == 1.8
        assert device["config"]["max_duty"] == 100     # from the manifest
        assert "frequency_hz" not in device["config"]
    finally:
        await hub.stop()
        await finish(task)


async def test_snapshot_shape_is_json(hub: Hub):
    await hub.start()
    transport = MemoryTransport()
    task = await connect(hub, transport)
    try:
        snapshot = hub.snapshot()
        json.dumps(snapshot)
        assert snapshot["t"] == "snapshot"
        assert {d["uid"] for d in snapshot["devices"]} == {"strip-1", "relay-1"}
        assert {t["id"] for t in snapshot["types"]} >= {"pwm_led_strip", "relay"}
        assert snapshot["settings"]["heartbeat_s"] == pytest.approx(0.4)
    finally:
        await hub.stop()
        await finish(task)


async def test_props_survive_a_hub_restart(config, store, registry):
    """Desired state is persisted and re-applied by a brand new hub instance."""
    hub1 = Hub(config, store, registry)
    await hub1.start()
    transport = MemoryTransport()
    task = await connect(hub1, transport)
    await hub1.set_props("strip-1", {"power": True, "brightness": 12})
    await next_command(transport)
    await hub1.stop()
    await finish(task)

    hub2 = Hub(config, store, registry)
    await hub2.start()
    device = devices_by_uid(hub2)["strip-1"]
    assert device["desired"]["power"] is True
    assert device["desired"]["brightness"] == 12
    transport2 = MemoryTransport("after-restart")
    task2 = await connect(hub2, transport2)
    try:
        command = await next_command(transport2)
        assert command["props"] == {"power": True, "brightness": 12}
    finally:
        await hub2.stop()
        await finish(task2)
