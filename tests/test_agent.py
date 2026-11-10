"""model-agent against a fake machine. Run: python3 tests/test_agent.py"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import FILE_BODY, FakeMachine, free_port  # noqa: E402

PASS = 0


def check(name: str, cond: bool, detail: object = "") -> None:
    global PASS
    if not cond:
        raise SystemExit(f"FAIL: {name} {detail}")
    PASS += 1
    print("ok  ", name)


late_port = free_port()
ENGINES = {
    "comfy": {"name": "ComfyUI", "type": "image", "runtime": "ComfyUI", "unit": "comfy.service"},
    "zimage": {"name": "Z-Image", "type": "image", "run": {
        "argv": ["/opt/py", "main.py", "--port", "8189"], "cwd": "/opt/z",
        "env": {"A": "b"}, "memory_max": "26G"}},
    "tts": {"name": "TTS", "type": "audio", "unit": "tts.service", "exclusive": False},
    "slow": {"name": "Slow", "type": "Video", "unit": "slow.service", "port": late_port,
             "start_timeout": 5},
    "broken": {"name": "Broken", "type": "image", "unit": "broken.service", "start_timeout": 10},
    "bad-file": {"name": "No unit", "type": "image"},
}

m = FakeMachine(ENGINES)
try:
    target_spec = {"name": "Files", "type": "misc", "unit": "files.service",
                   "models_dirs": [m.downloads],
                   "targets": [{"label": "Models", "path": m.downloads}]}
    m.write_engine("files", target_spec)

    # Auth and routing
    check("no token is refused", m.call("GET", "/status", token=None)[0] == 401)
    check("wrong token is refused", m.call("GET", "/status", token="x" * 30)[0] == 401)
    check("unknown route is 404", m.call("GET", "/nope")[0] == 404)

    # Inventory
    st = m.status()
    items = {i["id"]: i for i in st["items"]}
    check("ollama text model listed", items["ollama:llama3.1:8b"]["type"] == "text")
    check("embedding type comes from capabilities",
          items["ollama:embeddinggemma:latest"]["type"] == "embedding")
    check("vision tag kept", "vision" in items["ollama:llama3.1:8b"]["tags"])
    check("engine listed with its type", items["engine:comfy"]["type"] == "image")
    check("new type is lowercased and free-form", items["engine:slow"]["type"] == "video")
    check("bad engine file is reported, not fatal",
          any("bad-file" in e for e in st["catalog_errors"]) and "engine:bad-file" not in items)
    check("download target exposed", st["targets"][0]["label"] == "Models")

    # Hot-add through a plain file
    m.write_engine("3d-gen", {"name": "3D Gen", "type": "3d", "unit": "gen3d.service"})
    check("a dropped-in file appears without restart", m.item("engine:3d-gen")["type"] == "3d")

    # Start an Ollama model
    code, res = m.call("POST", "/models/start", {"id": "ollama:llama3.1:8b"})
    check("start model accepted", code == 200, res)
    check("model load finishes", m.wait_job(res["job"])["state"] == "done")
    check("model shows as running", m.item("ollama:llama3.1:8b")["state"] == "running")
    check("vram reported", m.item("ollama:llama3.1:8b")["vram"] == 5000000000)

    code, res = m.call("POST", "/models/start", {"id": "ollama:embeddinggemma:latest"})
    check("embedding model loads through /api/embed", m.wait_job(res["job"])["state"] == "done")

    # Exclusive engine needs confirmation and displaces Ollama
    code, res = m.call("POST", "/models/start", {"id": "engine:comfy"})
    check("exclusive start asks first", code == 409 and "llama3.1:8b" in res["conflicts"], res)
    check("nothing changed before confirming", m.is_active("ollama.service"))
    check("conflicts are in the status for the UI",
          "llama3.1:8b" in m.item("engine:comfy")["conflicts"])
    code, res = m.call("POST", "/models/start", {"id": "engine:comfy", "force": True})
    check("forced start finishes", m.wait_job(res["job"])["state"] == "done", res)
    check("engine active", m.is_active("comfy.service"))
    check("ollama stopped", not m.is_active("ollama.service"))
    time.sleep(0.2)
    st = m.status()
    check("active engine reported", st["active_engine"] == "ComfyUI")
    check("ollama reported down", st["ollama"]["up"] is False)
    check("ollama models stay listed while it is down",
          m.item("ollama:llama3.1:8b")["state"] == "unavailable")

    # Non-exclusive engine runs beside it
    code, res = m.call("POST", "/models/start", {"id": "engine:tts"})
    check("non-exclusive start needs no confirmation", code == 200, res)
    m.wait_job(res["job"])
    check("both run together", m.is_active("tts.service") and m.is_active("comfy.service"))
    code, res = m.call("POST", "/models/stop", {"id": "engine:tts"})
    m.wait_job(res["job"])

    # Switch between exclusive engines, transient command engine
    code, res = m.call("POST", "/models/start", {"id": "engine:zimage"})
    check("switching engines asks first", code == 409 and res["conflicts"] == ["ComfyUI"], res)
    code, res = m.call("POST", "/models/start", {"id": "engine:zimage", "force": True})
    check("switch finishes", m.wait_job(res["job"])["state"] == "done")
    check("old engine stopped", not m.is_active("comfy.service"))
    run_log = m.run_log()
    check("command engine runs as a transient unit",
          "--unit=model-agent-zimage.service" in run_log and "-- /opt/py main.py --port 8189" in run_log, run_log)
    check("memory limit, cwd and env passed",
          "MemoryMax=26G" in run_log and "--working-directory=/opt/z" in run_log and "-E A=b" in run_log, run_log)

    # Stopping the last exclusive engine brings Ollama back
    code, res = m.call("POST", "/models/stop", {"id": "engine:zimage"})
    check("stop finishes", m.wait_job(res["job"])["state"] == "done")
    time.sleep(0.2)
    check("ollama restored", m.is_active("ollama.service"))
    check("ollama back up in status", m.status()["ollama"]["up"] is True)

    # Starting a model while an engine holds the GPU
    code, res = m.call("POST", "/models/start", {"id": "engine:comfy", "force": True})
    m.wait_job(res["job"])
    code, res = m.call("POST", "/models/start", {"id": "ollama:llama3.1:8b"})
    check("model start asks before stopping the engine", code == 409 and res["conflicts"] == ["ComfyUI"])
    code, res = m.call("POST", "/models/start", {"id": "ollama:llama3.1:8b", "force": True})
    check("model start recovers ollama", m.wait_job(res["job"])["state"] == "done")
    check("engine was stopped", not m.is_active("comfy.service"))

    # Port readiness
    code, res = m.call("POST", "/models/start", {"id": "engine:slow", "force": True})
    job_id = res["job"]
    time.sleep(0.5)
    check("active without port is starting", m.item("engine:slow")["state"] == "starting")
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", late_port)); srv.listen()
    check("ready once the port opens", m.wait_job(job_id)["state"] == "done")
    check("running after port opens", m.item("engine:slow")["state"] == "running")
    srv.close()
    code, res = m.call("POST", "/models/stop", {"id": "engine:slow"})
    m.wait_job(res["job"])

    # A failed start says why and gives the GPU back
    open(os.path.join(m.state, "fail_start", "broken.service"), "w").close()
    code, res = m.call("POST", "/models/start", {"id": "engine:broken", "force": True})
    job = m.wait_job(res["job"])
    check("failed start reported with log", job["state"] == "error" and "out of memory" in job["message"], job)
    time.sleep(0.2)
    check("ollama restored after failed start", m.is_active("ollama.service"))

    # Health path: an open port that still answers 503 is not ready yet
    import http.server
    health = {"code": 503}

    class Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(health["code"] if self.path == "/health" else 404)
            self.end_headers()

        def log_message(self, *_a):
            pass

    hport = free_port()
    hsrv = http.server.ThreadingHTTPServer(("127.0.0.1", hport), Health)
    threading.Thread(target=hsrv.serve_forever, daemon=True).start()
    m.write_engine("llm", {"name": "LLM", "type": "text", "unit": "llm.service",
                           "port": hport, "health_path": "/health", "start_timeout": 10})
    m.write_engine("bad-health", {"name": "Bad", "type": "text", "unit": "bh.service",
                                  "port": hport, "health_path": "health"})
    time.sleep(0.1)
    check("bad health_path rejected", any("bad-health" in e for e in m.status()["catalog_errors"]))
    code, res = m.call("POST", "/models/start", {"id": "engine:llm", "force": True})
    job_id = res["job"]
    time.sleep(1.5)
    check("open port answering 503 is still starting", m.item("engine:llm")["state"] == "starting")
    health["code"] = 200
    check("ready once health answers 200", m.wait_job(job_id)["state"] == "done")
    check("running after health ok", m.item("engine:llm")["state"] == "running")
    code, res = m.call("POST", "/models/stop", {"id": "engine:llm"})
    m.wait_job(res["job"])
    hsrv.shutdown()

    # One switch at a time, input validation
    check("unknown engine", m.call("POST", "/models/start", {"id": "engine:nope"})[0] == 404)
    check("bad id", m.call("POST", "/models/start", {"id": "x"})[0] == 400)
    check("shell-y model name refused",
          m.call("POST", "/models/stop", {"id": "ollama:a;reboot"})[0] == 400)
    check("oversized body refused", m.call("POST", "/download", {"kind": "x" * 70000})[0] == 413)

    # Ollama pull
    m.pull_hold.clear()
    code, res = m.call("POST", "/download", {"kind": "ollama", "name": "new:latest"})
    job_id = res["job"]
    time.sleep(0.4)
    job = next(j for j in m.status()["jobs"] if j["id"] == job_id)
    check("pull progress sums the layers", (job["total"], job["done"]) == (400, 80), job)
    check("duplicate pull refused",
          m.call("POST", "/download", {"kind": "ollama", "name": "new:latest"})[0] == 409)
    m.pull_hold.set()
    check("pull completes", m.wait_job(job_id)["state"] == "done")
    code, res = m.call("POST", "/download", {"kind": "ollama", "name": "bad/model"})
    job = m.wait_job(res["job"])
    check("pull error surfaced", job["state"] == "error" and "does not exist" in job["message"], job)

    m.pull_hold.clear()
    code, res = m.call("POST", "/download", {"kind": "ollama", "name": "cancel:me"})
    time.sleep(0.3)
    check("cancel accepted", m.call("POST", f"/jobs/{res['job']}/cancel", {})[0] == 200)
    threading.Timer(0.2, m.pull_hold.set).start()
    check("pull cancelled", m.wait_job(res["job"])["state"] == "cancelled")

    # File download
    tid = m.status()["targets"][0]["id"]
    url = m.https + "/org/repo/resolve/main/model.gguf"
    code, res = m.call("POST", "/download", {"kind": "file", "url": url, "target": tid})
    job = m.wait_job(res["job"])
    final = os.path.join(m.downloads, "model.gguf")
    check("file downloaded", job["state"] == "done" and os.path.exists(final), job)
    check("file content intact", open(final, "rb").read() == FILE_BODY)
    check("no part file left", not os.path.exists(final + ".part"))
    check("existing file refused",
          m.call("POST", "/download", {"kind": "file", "url": url, "target": tid})[0] == 409)
    check("files counted for the engine", m.item("engine:files")["files"] == ["model.gguf"])

    # Resume after a dropped connection
    m.file_stop_after = 500000
    url2 = m.https + "/x/resume.safetensors"
    code, res = m.call("POST", "/download", {"kind": "file", "url": url2, "target": tid})
    job = m.wait_job(res["job"])
    check("short download fails", job["state"] == "error", job)
    part = os.path.join(m.downloads, "resume.safetensors.part")
    check("part file kept", os.path.getsize(part) == 500000)
    m.file_stop_after = None
    code, res = m.call("POST", "/download", {"kind": "file", "url": url2, "target": tid})
    check("retry completes", m.wait_job(res["job"])["state"] == "done")
    check("retry used a range request", m.file_requests[-1] == "bytes=500000-", m.file_requests)
    check("resumed file intact",
          open(os.path.join(m.downloads, "resume.safetensors"), "rb").read() == FILE_BODY)

    code, res = m.call("POST", "/download", {"kind": "file", "url": m.https + "/gated.bin", "target": tid})
    job = m.wait_job(res["job"])
    check("gated file explained", "gated" in job["message"], job)

    # Download validation
    for label, body in (
        ("plain http refused", {"kind": "file", "url": "http://x/y", "target": tid}),
        ("file url refused", {"kind": "file", "url": "file:///etc/passwd", "target": tid}),
        ("userinfo refused", {"kind": "file", "url": "https://a:b@x/y", "target": tid}),
        ("unknown folder refused", {"kind": "file", "url": url, "target": "zz:0"}),
        ("traversal name refused", {"kind": "file", "url": url, "target": tid, "filename": "../../etc/x"}),
        ("dotfile name refused", {"kind": "file", "url": url, "target": tid, "filename": ".bashrc"}),
        ("bad pull name refused", {"kind": "ollama", "name": "a b;c"}),
        ("unknown kind refused", {"kind": "torrent"}),
    ):
        check(label, m.call("POST", "/download", body)[0] == 400)

    # Adding engines from the dashboard
    m.add_unit("whisper.service")
    for label, body, want in (
        ("protected unit refused", {"name": "S", "type": "x", "unit": "sshd.service"}, 400),
        ("ollama unit refused", {"name": "S", "type": "x", "unit": "ollama"}, 400),
        ("missing unit refused", {"name": "S", "type": "x", "unit": "nope.service"}, 404),
        ("missing type refused", {"name": "W", "unit": "whisper.service"}, 400),
        ("injection unit refused", {"name": "S", "type": "x", "unit": "a;b.service"}, 400),
        ("bad port refused", {"name": "S", "type": "x", "unit": "whisper", "port": "abc"}, 400),
        ("bad url refused", {"name": "S", "type": "x", "unit": "whisper", "url": "javascript:x"}, 400),
    ):
        check(label, m.call("POST", "/engines", body)[0] == want, body)
    code, res = m.call("POST", "/engines", {"name": "Whisper STT", "type": "Speech", "unit": "whisper",
                                            "url": "http://h:9000/", "exclusive": False})
    check("engine added", code == 200 and res["id"] == "engine:whisper-stt", res)
    item = m.item("engine:whisper-stt")
    check("added engine has its type and is removable", item["type"] == "speech" and item["removable"])
    check("duplicate name refused", m.call("POST", "/engines", {"name": "Whisper STT", "type": "x", "unit": "whisper"})[0] == 409)
    check("hand-written engine cannot be removed from the UI", m.call("DELETE", "/engines/comfy")[0] == 409)
    code, res = m.call("POST", "/models/start", {"id": "engine:whisper-stt", "force": True})
    m.wait_job(res["job"], 20)
    check("running engine cannot be removed", m.call("DELETE", "/engines/whisper-stt")[0] == 409)
    code, res = m.call("POST", "/models/stop", {"id": "engine:whisper-stt"})
    m.wait_job(res["job"])
    check("stopped engine removed", m.call("DELETE", "/engines/whisper-stt")[0] == 200)
    check("gone from list", all(i["id"] != "engine:whisper-stt" for i in m.status()["items"]))

    print(f"\nAll {PASS} checks passed")
finally:
    m.close()
