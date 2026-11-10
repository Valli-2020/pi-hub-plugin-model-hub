"""Fake machine for the tests: Ollama API, systemd tools and an HTTPS file server."""
from __future__ import annotations

import http.server
import json
import os
import socket
import ssl
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.path.join(ROOT, "agent", "model-agent.py")
TOKEN = "test-token-0123456789abcdef"
FILE_BODY = bytes(range(256)) * 8192   # 2 MiB

SYSTEMCTL = r"""#!/bin/bash
S=$FAKE_STATE
args=()
for a in "$@"; do [[ $a == --* ]] || args+=("$a"); done
echo "systemctl ${args[*]}" >> "$S/log"
case "${args[0]}" in
  is-active) rc=0; for u in "${args[@]:1}"; do
      if [ -f "$S/active/$u" ]; then echo active; else echo inactive; rc=3; fi; done; exit $rc ;;
  start) if [ -f "$S/fail_start/${args[1]}" ]; then exit 0; fi; touch "$S/active/${args[1]}" ;;
  stop) rm -f "$S/active/${args[1]}" ;;
  reset-failed) ;;
  cat) [ -f "$S/units/${args[1]}" ] || exit 1 ;;
esac
"""
SYSTEMD_RUN = r"""#!/bin/bash
S=$FAKE_STATE
echo "$*" >> "$S/run.log"
for a in "$@"; do case "$a" in --unit=*) touch "$S/active/${a#--unit=}" ;; esac; done
"""
JOURNALCTL = '#!/bin/bash\necho "boom: out of memory"\n'
# The agent must not need the CLI (it panics without $HOME): fail loudly if used.
OLLAMA = r"""#!/bin/bash
echo "panic: \$HOME is not defined" >&2
exit 2
"""

MODELS = {
    "llama3.1:8b": ["completion", "tools", "vision"],
    "embeddinggemma:latest": ["embedding"],
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _quiet(handler):
    handler.log_message = lambda *a: None
    return handler


class FakeMachine:
    def __init__(self, engines: dict[str, dict] | None = None):
        self.tmp = tempfile.TemporaryDirectory(prefix="model-hub-test-")
        self.dir = self.tmp.name
        self.state = os.path.join(self.dir, "state")
        self.etc = os.path.join(self.dir, "etc")
        self.bin = os.path.join(self.dir, "bin")
        self.downloads = os.path.join(self.dir, "downloads")
        for sub in ("active", "loaded", "units", "fail_start"):
            os.makedirs(os.path.join(self.state, sub))
        os.makedirs(os.path.join(self.etc, "engines.d"))
        os.makedirs(self.bin)
        os.makedirs(self.downloads)
        open(os.path.join(self.state, "active", "ollama.service"), "w").close()
        with open(os.path.join(self.etc, "token"), "w") as f:
            f.write(TOKEN)
        for name, body in (("systemctl", SYSTEMCTL), ("systemd-run", SYSTEMD_RUN),
                           ("journalctl", JOURNALCTL), ("ollama", OLLAMA)):
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        for eid, spec in (engines or {}).items():
            self.write_engine(eid, spec)

        self.pull_hold = threading.Event()
        self.pull_hold.set()
        self.file_stop_after: int | None = None
        self.file_requests: list[str | None] = []
        self.ollama_port = free_port()
        self.agent_port = free_port()
        self._ollama_srv = None
        self._stop = threading.Event()
        self._start_tls()
        threading.Thread(target=self._watch, daemon=True).start()
        self._wait_port(self.ollama_port)
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"],
                   FAKE_STATE=self.state, SSL_CERT_FILE=self.cert)
        self.agent = subprocess.Popen(
            ["python3", AGENT, "--bind", "127.0.0.1", "--port", str(self.agent_port),
             "--etc", self.etc, "--ollama", f"http://127.0.0.1:{self.ollama_port}"],
            env=env)
        self._wait_port(self.agent_port)

    def write_engine(self, eid: str, spec: dict) -> None:
        with open(os.path.join(self.etc, "engines.d", eid + ".json"), "w") as f:
            json.dump(spec, f)

    def add_unit(self, unit: str) -> None:
        open(os.path.join(self.state, "units", unit), "w").close()

    def is_active(self, unit: str) -> bool:
        return os.path.exists(os.path.join(self.state, "active", unit))

    def log(self) -> str:
        path = os.path.join(self.state, "log")
        return open(path).read() if os.path.exists(path) else ""

    def run_log(self) -> str:
        path = os.path.join(self.state, "run.log")
        return open(path).read() if os.path.exists(path) else ""

    def _wait_port(self, port: int) -> None:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"port {port} never opened")

    # The fake Ollama listens only while its fake unit is active, like the real one.
    def _watch(self) -> None:
        while not self._stop.is_set():
            up = self.is_active("ollama.service")
            if up and self._ollama_srv is None:
                self._ollama_srv = self._serve_ollama()
            elif not up and self._ollama_srv is not None:
                self._ollama_srv.shutdown()
                self._ollama_srv.server_close()
                self._ollama_srv = None
                loaded_dir = os.path.join(self.state, "loaded")
                for name in os.listdir(loaded_dir):   # a stopped Ollama forgets its models
                    os.unlink(os.path.join(loaded_dir, name))
            time.sleep(0.03)

    def _serve_ollama(self):
        machine = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, payload, code=200):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _loaded(self):
                return os.listdir(os.path.join(machine.state, "loaded"))

            def do_GET(self):
                if self.path == "/api/version":
                    self._json({"version": "9.9.9"})
                elif self.path == "/api/tags":
                    self._json({"models": [
                        {"name": n, "digest": n, "size": 6600000000,
                         "details": {"family": "fam", "quantization_level": "Q4", "parameter_size": "9B"}}
                        for n in MODELS]})
                elif self.path == "/api/ps":
                    loaded = self._loaded()
                    self._json({"models": [
                        {"name": n, "size_vram": 5000000000, "expires_at": "2030-01-01T00:00:00Z"}
                        for n in MODELS if n.replace(":", "_").replace("/", "_") in loaded]})
                else:
                    self._json({}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/api/show":
                    self._json({"capabilities": MODELS.get(body["model"], [])})
                elif self.path in ("/api/generate", "/api/embed"):
                    if body["model"].startswith("fail"):
                        self._json({"error": "out of memory"}, 500)
                        return
                    n = body["model"].replace(":", "_").replace("/", "_")
                    if body.get("keep_alive") == 0 and not body.get("prompt"):
                        try:
                            os.remove(os.path.join(machine.state, "loaded", n))
                        except FileNotFoundError:
                            pass
                        self._json({"done": True, "done_reason": "unload"})
                        return
                    if "embedding" in MODELS.get(body["model"], []) and self.path != "/api/embed":
                        self._json({"error": "does not support generate"}, 400)
                        return
                    n = body["model"].replace(":", "_").replace("/", "_")
                    open(os.path.join(machine.state, "loaded", n), "w").close()
                    self._json({"done": True})
                elif self.path == "/api/pull":
                    self.send_response(200)
                    self.end_headers()
                    if body["model"] == "bad/model":
                        self.wfile.write(b'{"error":"pull model manifest: file does not exist"}\n')
                        return
                    for line in ({"status": "pulling manifest"},
                                 {"status": "pulling a", "digest": "a", "total": 100, "completed": 50},
                                 {"status": "pulling b", "digest": "b", "total": 300, "completed": 30}):
                        self.wfile.write(json.dumps(line).encode() + b"\n")
                        self.wfile.flush()
                    machine.pull_hold.wait(10)
                    try:
                        self.wfile.write(b'{"status":"still"}\n')
                        self.wfile.flush()
                        self.wfile.write(b'{"status":"success"}\n')
                    except OSError:
                        pass
                else:
                    self._json({}, 404)

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", self.ollama_port), H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    def _start_tls(self) -> None:
        self.cert = os.path.join(self.dir, "cert.pem")
        key = os.path.join(self.dir, "key.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", key, "-out", self.cert, "-days", "2",
                        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                       check=True, capture_output=True)
        machine = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if "gated" in self.path:
                    self.send_response(401)
                    self.end_headers()
                    return
                rng = self.headers.get("Range")
                machine.file_requests.append(rng)
                start = int(rng.split("=")[1].rstrip("-")) if rng else 0
                data = FILE_BODY[start:]
                self.send_response(206 if rng else 200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if machine.file_stop_after is not None:
                    self.wfile.write(data[:machine.file_stop_after])
                    self.wfile.flush()
                    self.connection.close()
                    return
                self.wfile.write(data)

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert, key)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        srv.daemon_threads = True
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        self.https = f"https://localhost:{srv.server_address[1]}"
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(self, method: str, path: str, body: dict | None = None,
             token: str | None = TOKEN) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(f"http://127.0.0.1:{self.agent_port}{path}",
                                     data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def status(self) -> dict:
        return self.call("GET", "/status")[1]

    def item(self, item_id: str) -> dict:
        return next(i for i in self.status()["items"] if i["id"] == item_id)

    def wait_job(self, job_id: str, timeout: float = 15) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = next(j for j in self.status()["jobs"] if j["id"] == job_id)
            if job["state"] != "running":
                return job
            time.sleep(0.05)
        raise AssertionError("job did not finish: " + job_id)

    def close(self) -> None:
        self.pull_hold.set()
        self.agent.terminate()
        self.agent.wait(5)
        self._stop.set()
        self.tmp.cleanup()
