"""End-to-end startup smoke test: runs Bridge.run() against a mock panel with
MQTT stubbed. Exercises log_version, purge, discovery, publish, inventory."""
import pathlib as _pl, sys as _sys
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[1] / "reliable_controls"))

import json, threading, time, types, logging, sys, subprocess, os, signal
logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s", force=True)
import rc_mqtt_bridge as B, rcp
logging.getLogger("rcp").setLevel(logging.ERROR)

mock = subprocess.Popen(["python3",str(_pl.Path(__file__).resolve().parents[1] / "reliable_controls" / "rc_mock_panel.py"),"--port","21068","--subnets","0"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.5)
_orig = rcp.MachClient.__init__
rcp.MachClient.__init__ = lambda self,host,controller=1,port=0,bind_port=0,timeout=1.5: _orig(
    self,"127.0.0.1",controller,port=21068,bind_port=0,timeout=timeout)

args = types.SimpleNamespace(
    config=None, host="10.83.106.161", name="Main House", controller=1,
    banks=2, interval=8.0, read_only=False, min=None, max=None, step=None,
    fast_point=None, fast_interval=None, purge=True, split_subnets=True,
    subnet_mode="bitmap", mqtt_host="x", mqtt_port=1883,
    mqtt_user=None, mqtt_pass=None, verbose=0)
cfg = B.Config(args)
bridge = B.Bridge(cfg)

published, subscribed = [], []
bridge.mq.publish = lambda t,payload=None,retain=False: published.append((t,payload))
bridge.mq.subscribe = lambda t: subscribed.append(t)
bridge.mq.unsubscribe = lambda t: None
for m in ("connect","loop_start","loop_stop","disconnect"):
    setattr(bridge.mq, m, lambda *a, **k: None)
bridge.purge_discovery = lambda *a, **k: 0   # no broker to replay from

threading.Timer(25.0, bridge.stop.set).start()
rc = bridge.run()

cfgs = [t for t,pl in published if "/config" in t and pl]
states = [t for t,pl in published if t.endswith("/state")]
print(f"\nrun() returned {rc}")
print(f"discovery configs published : {len(cfgs)}")
print(f"state publishes             : {len(states)}")
print(f"command subscriptions       : {len(subscribed)}")
mock.send_signal(signal.SIGKILL)
assert rc == 0 and cfgs and states, "startup path did not complete"
print("\nSMOKE TEST PASSED - full startup path executed with no missing attributes")
