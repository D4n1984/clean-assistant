"""Pre-clean safety: settings never replace zones; failed preparation never starts."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.update(CONGA_MODE="mock", MQTT_HOST="", DATA_DIR=tempfile.mkdtemp())
from backend import app as A
from conga_core import commands as cmd
from conga_core.config import RobotConfig
from conga_core.legacy.robot import LegacyRobot
from conga_core.legacy import protobuf as pb
from conga_core.legacy import mapdecode
from backend.mqtt_bridge import MqttBridge
from backend.schedules import ScheduleStore


sent = []
A.robot.command = lambda c: (sent.append(c), {"result": 0})[1]
A.robot.state.map_head_id = 77
A.robot.map = {"stored_zones": [{"kind": "nogo", "points_m": [[0, 0], [1, 0]]}]}
A.zones.zones = [{"id": 1, "kind": "nogo", "name": "CA", "mapid": 77,
                  "points": [[2, 0], [3, 0]]},
                 {"id": 2, "kind": "clean", "name": "Clean", "mapid": 77,
                  "points": [[4, 0], [5, 1]]}]
A._ui_prefs.update(resend_before_clean=True, resend_restrictions_before_clean=False)
A._resend_zones_and_prefs()
assert sent and all(c["control"] not in ("set_area", "set_virwall") for c in sent)
sent.clear()
A._ui_prefs.update(resend_before_clean=False, resend_restrictions_before_clean=True)
A._resend_zones_and_prefs()
assert len(sent) == 1 and len(sent[0]["virwallList"]) == 2
assert sent[0]["control"] == "set_virwall"
sent.clear()
# Saved coordinates are rounded; an equivalent wall reported in reverse by
# the robot must not be duplicated in its next replacement list.
A.robot.map["stored_zones"][0]["points_m"] = [[3.0000001, 0], [2, 0]]
A._resend_zones_and_prefs()
assert len(sent) == 1 and len(sent[0]["virwallList"]) == 1
sent.clear()
A.zones.zones = []
A.robot.map = {"stored_zones": []}
A._resend_zones_and_prefs()
assert not sent  # An empty CA store must never erase robot restrictions.

A.robot.map = None
result = asyncio.run(A.post_command({"action": "start"}))
assert result["ok"] is False and result["code"] == "pre_clean_failed"
assert not sent and A._state_dict()["last_clean_error"]
A.schedules.plans = [{"id": "p", "rooms": [{"room": 1}]}]
result = asyncio.run(A.schedule_run({"id": "p"}))
assert result["ok"] is False and not sent
now = A.datetime.datetime.now()
A.schedules.plans = [{"id": "due", "name": "Due", "mapid": 77,
                      "time": now.strftime("%H:%M"),
                      "days": ["lun", "mar", "mie", "jue", "vie", "sab", "dom"],
                      "rooms": [{"room": 1}]}]
asyncio.run(A._run_due_schedules_legacy())
assert not sent and A._legacy_sched["last"]["error"]
A.robot.map = {"stored_zones": [], "_cached": True}
assert asyncio.run(A.post_command({"action": "start"}))["ok"] is False
A._ui_prefs.update(resend_before_clean=False, resend_restrictions_before_clean=False)
A.robot.command = lambda c: {"ok": False, "error": "offline"}
result = asyncio.run(A.post_command({"action": "start"}))
assert result["code"] == "command_failed" and result["error"] == "offline"
A.robot.command = lambda c: {"result": 0}
assert asyncio.run(A.post_command({"action": "start"}))["ok"] is True
assert A._state_dict()["last_clean_error"] is None

# Old preferences remain enabled for settings, but never implicitly opt into walls.
Path(A.PREFS_PATH).write_text(json.dumps({"resend_before_clean": True}), encoding="utf-8")
A._load_prefs()
assert A._ui_prefs["resend_before_clean"] is True
assert A._ui_prefs["resend_restrictions_before_clean"] is False


def legacy():
    r = LegacyRobot(RobotConfig(), logger=lambda *args: None)
    r._lmap = {"head": {"map_head_id": 77}, "walls": {"areas": []},
               "rooms": [{"id": 1, "name": "Kitchen"}]}
    r._send = lambda op, f: (sent.append((op, f)), {"result": 0})[1]
    return r


for operation in (lambda r: r.command(cmd.clean_rooms([1])), lambda r: r.command(cmd.start())):
    for fail_at in (1, 2):
        sent.clear()
        r = legacy()
        counter = [0]
        def response(*args):
            counter[0] += 1
            return counter[0] != fail_at
        r._send_recv = response
        result = operation(r)
        assert result["ok"] is False
        assert not any(op == "DEVICE_AUTO_CLEAN_REQ" for op, _ in sent)

# A positive wall acknowledgement updates the walls included in the next room plan.
sent.clear()
r = legacy()
r._send_recv = lambda *args: True
assert r.command(cmd.set_virwall([{"points": [(0, 0), (1, 0)], "type": 200}], 77))["result"] == 0
assert r._plan_rooms_req(77, lambda _: True)[6][3] == 1
r._send_recv = lambda *args: False
assert r.command(cmd.set_virwall([{"points": [(0, 0), (1, 0)], "type": 301}], 77))["ok"] is False

# Wire responses: explicit zero succeeds; errors, empty responses and timeout fail.
for fields, expected in (({1: [0]}, True), ({1: [5]}, False), ({}, False)):
    r = legacy()
    r._send = lambda op, f: (r._wake("TEST_RSP", fields), {"result": 0})[1]
    assert r._send_recv("TEST_REQ", {}, "TEST_RSP", timeout=0.01) is expected
    assert not r._waiters
r = legacy()
assert r._send_recv("TEST_REQ", {}, "TEST_RSP", timeout=0.001) is False
assert not r._waiters

# Partial pushes from another map cannot retain the previous map's walls/rooms.
r = legacy()
decode = mapdecode.decode_map
mapdecode.decode_map = lambda _: {"head": {"map_head_id": 88}}
try:
    r._on_map(b"test")
    assert "walls" not in r._lmap and "rooms" not in r._lmap
finally:
    mapdecode.decode_map = decode
r._send = lambda *args: {"ok": False}
assert r._send_recv("TEST_REQ", {}, "TEST_RSP", timeout=0.001) is False
assert not r._waiters

# A legacy wall failure propagates through pre-clean and the API boundary.
A.robot = r = legacy()
A.LEGACY = True
r.state.map_head_id = 77
r.map = {"stored_zones": [{"kind": "nogo", "points_m": [[0, 0], [1, 0]]}]}
A._ui_prefs.update(resend_before_clean=False, resend_restrictions_before_clean=True)
r._send_recv = lambda *args: False
sent.clear()
result = asyncio.run(A.post_command({"action": "start"}))
assert result["code"] == "pre_clean_failed"
assert not any(op == "DEVICE_AUTO_CLEAN_REQ" for op, _ in sent)
sent.clear()
r._send_recv = lambda op, f, *args: (sent.append((op, f)), True)[1]
assert asyncio.run(A.post_command({"action": "start"}))["ok"] is True
ops = [op for op, _ in sent]
assert ops.index("DEVICE_MAPID_SET_AREA_RESTRICTED_INFO_REQ") < ops.index("DEVICE_MAPID_SET_PLAN_PARAMS_REQ")
assert ops.index("DEVICE_MAPID_SELECT_MAP_PLAN_REQ") < ops.index("DEVICE_AUTO_CLEAN_REQ")
assert A._state_dict()["last_clean_error"] is None

# MQTT start, room button and plan button all stop when pre-clean fails.
robot = SimpleNamespace(state=SimpleNamespace(state="docked"), command=lambda c: sent.append(c))
store = ScheduleStore(str(Path(tempfile.mkdtemp()) / "schedules.json"))
store.plans = [{"id": "p", "rooms": [{"room": 1}]}]
bridge = MqttBridge(robot, store, lambda k, default=None: default, lambda: 77, lambda: {})
errors = []
bridge.on_clean_error = errors.append
def fail():
    raise RuntimeError("preparation failed")
bridge.pre_clean_resend = fail
for topic, payload in ((bridge.t_cmd, "start"), (f"conga/{bridge.node}/room_command", "1"),
                       (f"conga/{bridge.node}/plan_run", "p")):
    sent.clear()
    bridge._on_message(None, None, SimpleNamespace(topic=topic, payload=payload.encode()))
    assert not sent
assert len(errors) == 3
started = []
bridge.pre_clean_resend = None
bridge.on_clean_started = lambda: started.append(True)
bridge._vacuum_command("start")
assert started == [True]
print("OK resend safety: settings, restrictions, API, legacy confirmations and MQTT")
