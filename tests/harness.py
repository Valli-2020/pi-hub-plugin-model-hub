"""Serve the plugin in the real dashboard from a Pi Hub checkout, on temp state.

Usage: PYTHONPATH=<pi-hub checkout> python3 tests/harness.py <agent-url> <token> [node-script]
With "fake" as the agent URL a FakeMachine is used.  Runs the node script with
HUB, ADMIN and VIEWER in its environment, or serves for 10 minutes without one.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
REPO = os.path.dirname(HERE)

from pi_hub import auth, config, server  # noqa: E402
from pi_hub.plugins import manager as mgr_mod  # noqa: E402


def fake_engines(machine_dir: str) -> dict:
    return {
        "comfyui": {"name": "ComfyUI", "type": "image", "runtime": "ComfyUI",
                    "unit": "comfyui.service", "url": "http://10.0.0.5:8188/simple",
                    "models_dirs": [machine_dir]},
        "s2": {"name": "S2 Pro TTS", "type": "audio", "runtime": "s2.cpp",
               "unit": "s2.service", "exclusive": False,
               "note": "Text to speech server"},
    }


def main() -> None:
    agent_url, token = sys.argv[1], sys.argv[2]
    script = sys.argv[3] if len(sys.argv) > 3 else None
    machine = None
    if agent_url == "fake":
        from fakes import TOKEN, FakeMachine
        machine = FakeMachine()
        for eid, spec in fake_engines(machine.downloads).items():
            machine.write_engine(eid, spec)
        machine.write_engine("files", {
            "name": "Video engine", "type": "video", "unit": "video.service",
            "targets": [{"label": "Models", "path": machine.downloads}]})
        machine.add_unit("whisper.service")
        agent_url, token = f"http://127.0.0.1:{machine.agent_port}", TOKEN

    tmp = tempfile.mkdtemp(prefix="model-hub-harness-")
    auth.USERS_PATH = os.path.join(tmp, "users.json")
    auth.SESSIONS_PATH = os.path.join(tmp, "sessions.json")
    config.CONFIG_PATH = os.path.join(tmp, "config.json")
    with open(os.path.join(os.path.dirname(os.path.dirname(config.__file__)),
                           "config.example.json")) as f:
        cfg = json.load(f)
    cfg.setdefault("auth", {})["enabled"] = True
    with open(config.CONFIG_PATH, "w") as f:
        json.dump(cfg, f)

    auth.add_user("admin1", "correct-horse-battery", role="admin")
    auth.add_user("viewer1", "correct-horse-battery", role="viewer")
    admin = auth.issue_session("admin1", "admin", web=True)
    viewer = auth.issue_session("viewer1", "viewer", web=True)

    plugins = os.path.join(tmp, "plugins")
    shutil.copytree(os.path.join(REPO, "pi_hub_plugins", "model-hub"),
                    os.path.join(plugins, "model-hub"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    with open(os.path.join(plugins, "model-hub", "config.json"), "w") as f:
        json.dump({"agent_url": agent_url, "token": token, "host_id": "test-pc-linux"}, f)

    m = mgr_mod.PluginManager()
    m._root = plugins
    declared = json.load(open(os.path.join(plugins, "model-hub", "pihub-plugin.json")))["capabilities"]
    ok, why = m.approve("model-hub", declared)
    assert ok, why
    m.load_plugin("model-hub")
    mgr_mod._instance = m

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    hub = f"http://127.0.0.1:{httpd.server_address[1]}"
    rc = 0
    try:
        if script:
            env = dict(os.environ, HUB=hub, ADMIN=admin, VIEWER=viewer,
                       AGENT_PID=str(machine.agent.pid) if machine else "")
            rc = subprocess.run(["node", script], env=env).returncode
        else:
            print(hub, admin, flush=True)
            time.sleep(600)
    finally:
        httpd.shutdown()
        if machine:
            try:
                machine.close()
            except subprocess.TimeoutExpired:
                pass
        shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(rc)


if __name__ == "__main__":
    main()
