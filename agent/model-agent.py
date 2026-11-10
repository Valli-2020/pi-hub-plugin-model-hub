#!/usr/bin/env python3
"""model-agent: start, stop, download and monitor local AI models (stdlib only).

Runs on the GPU machine and answers the Pi Hub ``model-hub`` plugin over HTTP
with a Bearer token.  It knows two kinds of things:

* every Ollama model, found through the Ollama API;
* every *engine*: one JSON file per engine in ``engines.d/``.  An engine is a
  systemd unit or a command, of any type (image, audio, video, ...), so a new
  one needs a new file and no code.  See ``engines.example/`` in the plugin.

Engines marked ``exclusive`` need the whole GPU: starting one stops Ollama and
every other exclusive engine; stopping the last one brings Ollama back.

Usage: model-agent.py --bind IP [--port 9872] [--etc /etc/model-agent]
"""
from __future__ import annotations

import argparse
import glob
import hmac
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.1"
MAX_BODY = 64 * 1024
MAX_FINISHED_JOBS = 30
MAX_DOWNLOADS = 3
CHUNK = 1024 * 1024
OLLAMA_WAIT_S = 60
LOAD_TIMEOUT_S = 300
USER_AGENT = "model-agent/" + VERSION

ENGINE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,40}")
TYPE_RE = re.compile(r"[a-z0-9][a-z0-9 _-]{0,23}")
UNIT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._-]{0,120}\.service")
OLLAMA_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")
FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
#: Units the dashboard form may never take over: stopping one locks you out.
PROTECTED_UNIT_RE = re.compile(
    r"(ollama|model-agent|ssh|sshd|tailscaled|dbus|docker|containerd|networking"
    r"|NetworkManager|cron|wol|systemd-.*|getty@.*|user@.*)\.service")

ACTIVE_STATES = ("running", "starting")

args: argparse.Namespace


class AgentError(Exception):
    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


# ── Small helpers ───────────────────────────────────────────────────────────

def run(argv: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", "timed out: " + argv[0])
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, ValueError):
        return None


def read_int(path: str) -> int | None:
    text = read_text(path)
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


def tcp_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def http_ok(host: str, port: int, path: str) -> bool:
    """True when GET path answers 2xx. llama.cpp answers 503 while loading."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=1.5) as resp:
            return 200 <= resp.status < 300
    except (OSError, ValueError):
        return False


def units_state(units: list[str]) -> dict[str, str]:
    """Return the systemd ActiveState of each unit, unknown ones as inactive."""
    if not units:
        return {}
    lines = run(["systemctl", "is-active"] + units, timeout=8).stdout.split()
    return {u: (lines[i] if i < len(lines) else "inactive")
            for i, u in enumerate(units)}


def journal_tail(unit: str, lines: int = 6) -> str:
    out = run(["journalctl", "-u", unit, "-n", str(lines), "--no-pager",
               "-o", "cat"], timeout=8).stdout.strip()
    return out[-600:]


def dir_size_and_files(path: str) -> tuple[int, list[str]]:
    total, names = 0, []
    try:
        entries = list(os.scandir(path))
    except OSError:
        return 0, []
    for entry in entries:
        try:
            # A .part file is an unfinished download, not a model yet.
            if (entry.is_file() and not entry.name.endswith(".part")
                    and entry.stat().st_size > 1024 * 1024):
                total += entry.stat().st_size
                names.append(entry.name)
        except OSError:
            continue
    return total, sorted(names)


# ── Engine catalog (engines.d/*.json) ───────────────────────────────────────

class Catalog:
    """Engine files, re-read whenever one is added, changed or removed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sig: tuple = ()
        self.engines: dict[str, dict] = {}
        self.errors: list[str] = []

    @property
    def directory(self) -> str:
        return os.path.join(args.etc, "engines.d")

    def load(self) -> tuple[dict[str, dict], list[str]]:
        paths = sorted(glob.glob(os.path.join(self.directory, "*.json")))
        sig = []
        for path in paths:
            try:
                sig.append((path, os.stat(path).st_mtime_ns))
            except OSError:
                continue
        with self._lock:
            if tuple(sig) != self._sig:
                self._sig = tuple(sig)
                self._reload([p for p, _ in sig])
            return self.engines, self.errors

    def _reload(self, paths: list[str]) -> None:
        engines, errors = {}, []
        for path in paths:
            stem = os.path.basename(path)[:-len(".json")]
            try:
                with open(path, encoding="utf-8") as f:
                    engines[stem] = validate_engine(stem, json.load(f))
            except (OSError, ValueError, AgentError) as exc:
                errors.append(f"{os.path.basename(path)}: {exc}")
        self.engines, self.errors = engines, errors


def validate_engine(engine_id: str, raw: dict) -> dict:
    if not ENGINE_ID_RE.fullmatch(engine_id):
        raise AgentError("file name must be lowercase letters, digits, - or _")
    if not isinstance(raw, dict):
        raise AgentError("expected a JSON object")
    name = str(raw.get("name") or "").strip()
    etype = str(raw.get("type") or "").strip().lower()
    if not name or len(name) > 60:
        raise AgentError("'name' is required (up to 60 characters)")
    if not TYPE_RE.fullmatch(etype):
        raise AgentError("'type' is required, for example image or audio")

    engine: dict = {
        "id": engine_id,
        "name": name,
        "type": etype,
        "runtime": str(raw.get("runtime") or "Service")[:30],
        "exclusive": bool(raw.get("exclusive", True)),
        "note": str(raw.get("note") or "")[:200],
        "url": "",
        "port": None,
        "models_dirs": [],
        "targets": [],
        "managed_by": str(raw.get("managed_by") or ""),
    }

    has_unit, has_run = bool(raw.get("unit")), bool(raw.get("run"))
    if has_unit == has_run:
        raise AgentError("set exactly one of 'unit' or 'run'")
    if has_unit:
        unit = str(raw["unit"])
        if not UNIT_RE.fullmatch(unit):
            raise AgentError("'unit' must be a systemd .service name")
        engine["unit"] = unit
        engine["run"] = None
    else:
        spec = raw["run"]
        if (not isinstance(spec, dict) or not isinstance(spec.get("argv"), list)
                or not spec["argv"]
                or not all(isinstance(a, str) for a in spec["argv"])):
            raise AgentError("'run.argv' must be a list of strings")
        engine["unit"] = f"model-agent-{engine_id}.service"
        engine["run"] = {
            "argv": spec["argv"],
            "cwd": str(spec.get("cwd") or ""),
            "env": {str(k): str(v) for k, v in dict(spec.get("env") or {}).items()},
            "memory_max": str(spec.get("memory_max") or ""),
        }

    if raw.get("port") is not None:
        port = raw["port"]
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise AgentError("'port' must be a TCP port number")
        engine["port"] = port
    if raw.get("url"):
        url = str(raw["url"])
        if not re.match(r"https?://[^\s/]+", url):
            raise AgentError("'url' must start with http:// or https://")
        engine["url"] = url
    health = raw.get("health_path") or ""
    if health and (not isinstance(health, str) or not re.fullmatch(r"/[A-Za-z0-9._~/?=&-]{0,120}", health)):
        raise AgentError("'health_path' must be a path such as /health")
    if health and engine["port"] is None:
        raise AgentError("'health_path' needs 'port'")
    engine["health_path"] = health
    timeout = raw.get("start_timeout", 180)
    if not isinstance(timeout, int) or not 5 <= timeout <= 1800:
        raise AgentError("'start_timeout' must be 5 to 1800 seconds")
    engine["start_timeout"] = timeout

    dirs = raw.get("models_dirs") or []
    if not isinstance(dirs, list) or not all(
            isinstance(d, str) and os.path.isabs(d) for d in dirs):
        raise AgentError("'models_dirs' must be a list of absolute paths")
    engine["models_dirs"] = dirs

    targets = raw.get("targets") or []
    if not isinstance(targets, list):
        raise AgentError("'targets' must be a list")
    for t in targets:
        if not (isinstance(t, dict) and t.get("label")
                and os.path.isabs(str(t.get("path") or ""))):
            raise AgentError("each target needs 'label' and an absolute 'path'")
        engine["targets"].append({"label": str(t["label"])[:60],
                                  "path": str(t["path"])})
    return engine


catalog = Catalog()


def engine_probe_host(engine: dict) -> str:
    """An IP from the engine's URL (it may bind only there), else loopback.

    Names are not resolved: a slow DNS lookup would stall every status call.
    """
    host = urllib.parse.urlsplit(engine["url"]).hostname if engine["url"] else None
    try:
        return str(ipaddress.ip_address(host or ""))
    except ValueError:
        return "127.0.0.1"


def engine_states(engines: dict[str, dict]) -> dict[str, str]:
    units = units_state([e["unit"] for e in engines.values()])
    states = {}
    for eid, e in engines.items():
        unit_state = units.get(e["unit"], "inactive")
        if unit_state == "active":
            ready = e["port"] is None or tcp_open(engine_probe_host(e), e["port"])
            if ready and e["port"] is not None and e["health_path"]:
                ready = http_ok(engine_probe_host(e), e["port"], e["health_path"])
            states[eid] = "running" if ready else "starting"
        elif unit_state == "activating":
            states[eid] = "starting"
        elif unit_state == "deactivating":
            states[eid] = "stopping"
        elif unit_state == "failed":
            states[eid] = "failed"
        else:
            states[eid] = "stopped"
    return states


# ── Ollama ──────────────────────────────────────────────────────────────────

def ollama_request(path: str, body: dict | None = None, timeout: float = 4):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        args.ollama + path, data=data,
        headers={"Content-Type": "application/json"} if data else {})
    return urllib.request.urlopen(req, timeout=timeout)


def ollama_json(path: str, body: dict | None = None, timeout: float = 4) -> dict:
    with ollama_request(path, body, timeout) as resp:
        return json.load(resp)


class OllamaView:
    """Last known Ollama models, so the list survives while Ollama is stopped."""

    def __init__(self) -> None:
        self.models: list[dict] = []
        self.capabilities: dict[str, list[str]] = {}
        self.version = ""

    def _caps(self, name: str, digest: str) -> list[str]:
        key = digest or name
        if key not in self.capabilities:
            try:
                info = ollama_json("/api/show", {"model": name}, timeout=4)
                self.capabilities[key] = list(info.get("capabilities") or [])
            except (OSError, ValueError):
                return []
        return self.capabilities[key]

    def snapshot(self) -> dict:
        try:
            tags = ollama_json("/api/tags")
            loaded = {m.get("name"): m for m in ollama_json("/api/ps").get("models") or []}
            if not self.version:
                self.version = ollama_json("/api/version").get("version", "")
        except (OSError, ValueError):
            return {"up": False, "version": self.version, "models": self.models,
                    "loaded": {}}
        models = []
        for m in tags.get("models") or []:
            name = m.get("name") or ""
            caps = self._caps(name, m.get("digest") or "")
            details = m.get("details") or {}
            models.append({
                "name": name,
                "size": m.get("size") or 0,
                "family": details.get("family") or "",
                "quant": details.get("quantization_level") or "",
                "params": details.get("parameter_size") or "",
                "capabilities": caps,
            })
        self.models = models
        return {"up": True, "version": self.version, "models": models,
                "loaded": loaded}


ollama_view = OllamaView()


def model_type(capabilities: list[str]) -> str:
    return "embedding" if "embedding" in capabilities else "text"


def ensure_ollama() -> None:
    if tcp_open(*ollama_address()):
        return
    run(["systemctl", "start", "ollama.service"], timeout=30)
    deadline = time.time() + OLLAMA_WAIT_S
    while time.time() < deadline:
        if tcp_open(*ollama_address()):
            return
        time.sleep(1)
    raise AgentError("Ollama did not come up", 500)


def ollama_address() -> tuple[str, int]:
    parts = urllib.parse.urlsplit(args.ollama)
    return parts.hostname or "127.0.0.1", parts.port or 11434


def load_ollama_model(name: str, capabilities: list[str]) -> None:
    if "embedding" in capabilities:
        path, body = "/api/embed", {"model": name, "input": "load"}
    else:
        path, body = "/api/generate", {"model": name, "prompt": ""}
    last = None
    # The first load after a cold boot can time out while ROCm initialises.
    for _ in range(2):
        try:
            with ollama_request(path, {**body, "stream": False}, LOAD_TIMEOUT_S) as r:
                r.read()
            return
        except urllib.error.HTTPError as exc:
            last = exc.read().decode(errors="replace")[:300]
        except OSError as exc:
            last = str(exc)
    raise AgentError(f"Could not load {name}: {last}", 500)


# ── Jobs ────────────────────────────────────────────────────────────────────

class Jobs:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._cancels: dict[str, threading.Event] = {}

    def create(self, kind: str, label: str) -> dict:
        job = {"id": uuid.uuid4().hex[:12], "kind": kind, "label": label,
               "state": "running", "message": "", "total": 0, "done": 0,
               "speed": 0, "started": time.time(), "ended": None}
        with self._lock:
            self._jobs[job["id"]] = job
            self._cancels[job["id"]] = threading.Event()
            finished = [j for j in self._jobs.values() if j["state"] != "running"]
            for old in finished[:-MAX_FINISHED_JOBS]:
                self._jobs.pop(old["id"], None)
                self._cancels.pop(old["id"], None)
        return job

    def update(self, job: dict, **fields) -> None:
        with self._lock:
            job.update(fields)

    def finish(self, job: dict, state: str, message: str = "") -> None:
        with self._lock:
            job.update(state=state, message=message, speed=0, ended=time.time())

    def cancel_event(self, job: dict) -> threading.Event:
        return self._cancels[job["id"]]

    def request_cancel(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise AgentError("No such job", 404)
            if job["kind"] not in ("pull", "file") or job["state"] != "running":
                raise AgentError("This job cannot be cancelled", 409)
            self._cancels[job_id].set()

    def running(self, kinds: tuple[str, ...]) -> list[dict]:
        with self._lock:
            return [dict(j) for j in self._jobs.values()
                    if j["state"] == "running" and j["kind"] in kinds]

    def list(self) -> list[dict]:
        with self._lock:
            return sorted((dict(j) for j in self._jobs.values()),
                          key=lambda j: j["started"], reverse=True)


jobs = Jobs()
switch_lock = threading.Lock()   # one start/stop at a time: the GPU is shared


def spawn(job: dict, fn, *fn_args, lock: threading.Lock | None = None) -> None:
    def runner() -> None:
        try:
            fn(job, *fn_args)
        except AgentError as exc:
            jobs.finish(job, "error", str(exc))
        except Exception as exc:  # a job thread must always end its job
            jobs.finish(job, "error", f"{type(exc).__name__}: {exc}")
        finally:
            if lock is not None:
                lock.release()
    threading.Thread(target=runner, daemon=True).start()


class SpeedMeter:
    def __init__(self) -> None:
        self.t0 = time.time()
        self.base = 0

    def rate(self, done: int) -> int | None:
        now = time.time()
        if now - self.t0 < 1:
            return None
        speed = int(max(done - self.base, 0) / (now - self.t0))
        self.t0, self.base = now, done
        return speed


# ── Start and stop ──────────────────────────────────────────────────────────

def active_exclusive(states: dict[str, str], engines: dict[str, dict],
                     skip: str = "") -> list[dict]:
    return [e for eid, e in engines.items()
            if eid != skip and e["exclusive"] and states.get(eid) in ACTIVE_STATES]


def conflicts_for(item_id: str, engines: dict[str, dict],
                  states: dict[str, str], loaded: dict) -> list[str]:
    """Names of what starting *item_id* would interrupt."""
    if item_id.startswith("ollama:"):
        return [e["name"] for e in active_exclusive(states, engines)]
    eid = item_id[len("engine:"):]
    engine = engines.get(eid)
    if engine is None or not engine["exclusive"]:
        return []
    names = [e["name"] for e in active_exclusive(states, engines, skip=eid)]
    return names + sorted(loaded)


def stop_engine_unit(engine: dict) -> None:
    res = run(["systemctl", "stop", engine["unit"]], timeout=90)
    if res.returncode != 0:
        raise AgentError(f"Could not stop {engine['name']}: {res.stderr.strip()[:200]}", 500)


def restore_ollama_if_free() -> None:
    engines, _ = catalog.load()
    if not active_exclusive(engine_states(engines), engines):
        run(["systemctl", "--no-block", "start", "ollama.service"], timeout=20)


def start_ollama_model(job: dict, name: str, capabilities: list[str]) -> None:
    engines, _ = catalog.load()
    for engine in active_exclusive(engine_states(engines), engines):
        jobs.update(job, message=f"Stopping {engine['name']}")
        stop_engine_unit(engine)
    jobs.update(job, message="Waiting for Ollama")
    ensure_ollama()
    jobs.update(job, message="Loading into memory")
    load_ollama_model(name, capabilities)
    jobs.finish(job, "done", "Loaded")


def launch_unit(engine: dict) -> subprocess.CompletedProcess:
    spec = engine["run"]
    if spec is None:
        return run(["systemctl", "start", "--no-block", engine["unit"]], timeout=30)
    # A leftover failed run would make systemd-run refuse the unit name.
    run(["systemctl", "reset-failed", engine["unit"]], timeout=10)
    argv = ["systemd-run", f"--unit={engine['unit']}", "--collect", "--no-block"]
    if spec["cwd"]:
        argv.append(f"--working-directory={spec['cwd']}")
    if spec["memory_max"]:
        argv += ["-p", f"MemoryMax={spec['memory_max']}"]
    for key, value in spec["env"].items():
        argv += ["-E", f"{key}={value}"]
    return run(argv + ["--"] + spec["argv"], timeout=30)


def start_engine(job: dict, engine: dict) -> None:
    engines, _ = catalog.load()
    if engine["exclusive"]:
        for other in active_exclusive(engine_states(engines), engines, skip=engine["id"]):
            jobs.update(job, message=f"Stopping {other['name']}")
            stop_engine_unit(other)
        jobs.update(job, message="Stopping Ollama")
        run(["systemctl", "stop", "ollama.service"], timeout=60)
    jobs.update(job, message="Starting")
    res = launch_unit(engine)
    if res.returncode != 0:
        restore_after_failure(engine)
        raise AgentError(f"Could not start: {res.stderr.strip()[:300]}", 500)

    launched = time.time()
    deadline = launched + engine["start_timeout"]
    while time.time() < deadline:
        state = engine_states({engine["id"]: engine})[engine["id"]]
        if state == "running":
            jobs.finish(job, "done", "Running")
            return
        if state in ("failed", "stopped") and time.time() > launched + 3:
            log = journal_tail(engine["unit"])
            restore_after_failure(engine)
            raise AgentError(f"Exited during start. {log}".strip(), 500)
        time.sleep(1)
    raise AgentError(f"Not ready after {engine['start_timeout']} s", 500)


def restore_after_failure(engine: dict) -> None:
    if engine["exclusive"]:
        restore_ollama_if_free()


def stop_ollama_model(job: dict, name: str) -> None:
    res = run(["ollama", "stop", name], timeout=60)
    if res.returncode != 0:
        raise AgentError(res.stderr.strip()[:300] or "ollama stop failed", 500)
    jobs.finish(job, "done", "Unloaded")


def stop_engine(job: dict, engine: dict) -> None:
    stop_engine_unit(engine)
    if engine["exclusive"]:
        jobs.update(job, message="Bringing Ollama back")
        restore_ollama_if_free()
    jobs.finish(job, "done", "Stopped")


def resolve(item_id: str) -> tuple[str, object]:
    """Map an item id to ("ollama", name) or ("engine", engine dict)."""
    if item_id.startswith("ollama:"):
        name = item_id[len("ollama:"):]
        if not OLLAMA_NAME_RE.fullmatch(name):
            raise AgentError("Invalid model name")
        return "ollama", name
    if item_id.startswith("engine:"):
        engines, _ = catalog.load()
        engine = engines.get(item_id[len("engine:"):])
        if engine is None:
            raise AgentError("Unknown engine", 404)
        return "engine", engine
    raise AgentError("Invalid id")


def api_start(body: dict) -> dict:
    item_id = str(body.get("id") or "")
    kind, target = resolve(item_id)
    engines, _ = catalog.load()
    states = engine_states(engines)
    snap = ollama_view.snapshot()
    conflicts = conflicts_for(item_id, engines, states, snap["loaded"])
    if conflicts and not body.get("force"):
        raise AgentError("This stops: " + ", ".join(conflicts), 409, conflicts=conflicts)
    if not switch_lock.acquire(blocking=False):
        raise AgentError("Another start or stop is still running", 409)
    if kind == "ollama":
        caps = next((m["capabilities"] for m in snap["models"]
                     if m["name"] == target), [])
        job = jobs.create("start", str(target))
        spawn(job, start_ollama_model, target, caps, lock=switch_lock)
    else:
        job = jobs.create("start", target["name"])
        spawn(job, start_engine, target, lock=switch_lock)
    return {"job": job["id"]}


def api_stop(body: dict) -> dict:
    kind, target = resolve(str(body.get("id") or ""))
    if not switch_lock.acquire(blocking=False):
        raise AgentError("Another start or stop is still running", 409)
    if kind == "ollama":
        job = jobs.create("stop", str(target))
        spawn(job, stop_ollama_model, target, lock=switch_lock)
    else:
        job = jobs.create("stop", target["name"])
        spawn(job, stop_engine, target, lock=switch_lock)
    return {"job": job["id"]}


# ── Downloads ───────────────────────────────────────────────────────────────

def pull_ollama(job: dict, name: str) -> None:
    cancel = jobs.cancel_event(job)
    layers: dict[str, tuple[int, int]] = {}
    meter = SpeedMeter()
    try:
        resp = ollama_request("/api/pull", {"model": name, "stream": True}, timeout=120)
    except urllib.error.HTTPError as exc:
        raise AgentError(exc.read().decode(errors="replace")[:300], 500)
    with resp:
        for raw in resp:
            if cancel.is_set():
                jobs.finish(job, "cancelled", "Partial data is kept for a later retry")
                return
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if line.get("error"):
                raise AgentError(str(line["error"])[:300], 500)
            if line.get("digest") and line.get("total"):
                layers[line["digest"]] = (line["total"], line.get("completed") or 0)
            total = sum(t for t, _ in layers.values())
            done = sum(c for _, c in layers.values())
            speed = meter.rate(done)
            fields = {"total": total, "done": done, "message": line.get("status", "")}
            if speed is not None:
                fields["speed"] = speed
            jobs.update(job, **fields)
            if line.get("status") == "success":
                jobs.finish(job, "done", "Downloaded")
                return
    raise AgentError("Ollama ended the download early", 500)


def download_file(job: dict, url: str, directory: str, filename: str) -> None:
    cancel = jobs.cancel_event(job)
    final = os.path.join(directory, filename)
    part = final + ".part"
    offset = os.path.getsize(part) if os.path.exists(part) else 0
    headers = {"User-Agent": USER_AGENT}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as exc:
        hint = " (gated or private, needs a token)" if exc.code in (401, 403) else ""
        raise AgentError(f"HTTP {exc.code}{hint}", 500)
    except OSError as exc:
        raise AgentError(f"Download failed: {exc}", 500)
    meter = SpeedMeter()
    with resp:
        if resp.status != 206:
            offset = 0
        length = int(resp.headers.get("Content-Length") or 0)
        total = offset + length if length else 0
        if length and length > shutil.disk_usage(directory).free:
            raise AgentError(f"Not enough free disk for {filename}", 500)
        done = offset
        meter.base = done
        jobs.update(job, total=total, done=done, message="Downloading")
        with open(part, "ab" if offset else "wb") as out:
            while True:
                if cancel.is_set():
                    jobs.finish(job, "cancelled", "Partial file is kept for a later retry")
                    return
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                speed = meter.rate(done)
                if speed is None:
                    jobs.update(job, done=done)
                else:
                    jobs.update(job, done=done, speed=speed)
    if total and done != total:
        raise AgentError("Connection closed early, retry to resume", 500)
    os.replace(part, final)
    jobs.finish(job, "done", "Downloaded")


def all_targets(engines: dict[str, dict]) -> dict[str, dict]:
    targets = {}
    for e in engines.values():
        for i, t in enumerate(e["targets"]):
            targets[f"{e['id']}:{i}"] = {"id": f"{e['id']}:{i}", "engine": e["name"],
                                          "label": t["label"], "path": t["path"]}
    return targets


def filename_from(url: str) -> str:
    return urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])


def api_download(body: dict) -> dict:
    kind = body.get("kind")
    active = jobs.running(("pull", "file"))
    if len(active) >= MAX_DOWNLOADS:
        raise AgentError(f"At most {MAX_DOWNLOADS} downloads at a time", 409)

    if kind == "ollama":
        name = str(body.get("name") or "").strip()
        if not OLLAMA_NAME_RE.fullmatch(name):
            raise AgentError("Invalid Ollama model name")
        if not tcp_open(*ollama_address()):
            raise AgentError("Ollama is stopped. Stop the running engine first.", 409)
        if any(j["label"] == name for j in active):
            raise AgentError("Already downloading", 409)
        job = jobs.create("pull", name)
        spawn(job, pull_ollama, name)
        return {"job": job["id"]}

    if kind == "file":
        url = str(body.get("url") or "").strip()
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username:
            raise AgentError("The URL must start with https://")
        engines, _ = catalog.load()
        target = all_targets(engines).get(str(body.get("target") or ""))
        if target is None:
            raise AgentError("Choose a download folder")
        filename = str(body.get("filename") or "").strip() or filename_from(url)
        if not FILENAME_RE.fullmatch(filename) or filename.endswith(".part"):
            raise AgentError("Enter a file name (letters, digits, dot, - or _)")
        directory = target["path"]
        if not os.path.isdir(directory):
            raise AgentError(f"Folder does not exist: {directory}", 500)
        if os.path.exists(os.path.join(directory, filename)):
            raise AgentError(f"{filename} already exists there", 409)
        if any(j["label"] == filename for j in active):
            raise AgentError("Already downloading", 409)
        job = jobs.create("file", filename)
        spawn(job, download_file, url, directory, filename)
        return {"job": job["id"]}

    raise AgentError("Unknown download kind")


# ── Adding engines from the dashboard ───────────────────────────────────────

def api_add_engine(body: dict) -> dict:
    unit = str(body.get("unit") or "").strip()
    if unit and not unit.endswith(".service"):
        unit += ".service"
    if not UNIT_RE.fullmatch(unit):
        raise AgentError("Enter the systemd unit name")
    if PROTECTED_UNIT_RE.fullmatch(unit):
        raise AgentError(f"{unit} is a system service and cannot be added")
    if run(["systemctl", "cat", unit], timeout=8).returncode != 0:
        raise AgentError(f"No unit named {unit} on this machine", 404)

    name = str(body.get("name") or "").strip()
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
    if not slug or not ENGINE_ID_RE.fullmatch(slug):
        raise AgentError("Enter a name with letters or digits")
    path = os.path.join(catalog.directory, slug + ".json")
    if os.path.exists(path):
        raise AgentError(f"An engine called {slug} already exists", 409)

    entry: dict = {"name": name, "type": body.get("type"), "unit": unit,
                   "exclusive": bool(body.get("exclusive", True)),
                   "managed_by": "ui"}
    if body.get("port") not in (None, ""):
        try:
            entry["port"] = int(body["port"])
        except (TypeError, ValueError):
            raise AgentError("Port must be a number")
    if body.get("url"):
        entry["url"] = str(body["url"]).strip()
    validate_engine(slug, entry)

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entry, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return {"id": "engine:" + slug}


def api_remove_engine(engine_id: str) -> dict:
    engines, _ = catalog.load()
    engine = engines.get(engine_id)
    if engine is None:
        raise AgentError("Unknown engine", 404)
    if engine["managed_by"] != "ui":
        raise AgentError("This engine comes from a file on the machine; delete it there", 409)
    if engine_states({engine_id: engine})[engine_id] != "stopped":
        raise AgentError("Stop the engine first", 409)
    os.unlink(os.path.join(catalog.directory, engine_id + ".json"))
    return {"removed": engine_id}


# ── Monitoring ──────────────────────────────────────────────────────────────

_cpu_prev: tuple[int, int] | None = None
_cpu_lock = threading.Lock()


def cpu_percent() -> float | None:
    global _cpu_prev
    line = (read_text("/proc/stat") or "").split("\n", 1)[0].split()
    if len(line) < 8:
        return None
    values = [int(v) for v in line[1:9]]
    idle, total = values[3] + values[4], sum(values)
    with _cpu_lock:
        prev, _cpu_prev = _cpu_prev, (idle, total)
    if prev is None or total == prev[1]:
        return None
    return round(100 * (1 - (idle - prev[0]) / (total - prev[1])), 1)


def memory() -> dict:
    info = {}
    for line in (read_text("/proc/meminfo") or "").splitlines():
        key, _, rest = line.partition(":")
        if rest.strip():
            info[key] = int(rest.split()[0]) * 1024
    total = info.get("MemTotal", 0)
    return {"total": total, "used": total - info.get("MemAvailable", 0)}


def cpu_temp() -> float | None:
    for hwmon in glob.glob("/sys/class/hwmon/hwmon*"):
        if read_text(hwmon + "/name") in ("k10temp", "coretemp", "zenpower"):
            value = read_int(hwmon + "/temp1_input")
            if value is not None:
                return round(value / 1000, 1)
    return None


def gpu() -> dict | None:
    """The card with the most VRAM, read from amdgpu sysfs."""
    best, best_total = None, 0
    for card in glob.glob("/sys/class/drm/card[0-9]*/device"):
        total = read_int(card + "/mem_info_vram_total") or 0
        if total > best_total:
            best, best_total = card, total
    if best is None:
        return None
    result = {"vram_total": best_total,
              "vram_used": read_int(best + "/mem_info_vram_used"),
              "busy": read_int(best + "/gpu_busy_percent"),
              "temp": None, "power": None}
    for hwmon in glob.glob(best + "/hwmon/hwmon*"):
        temp = read_int(hwmon + "/temp1_input")
        power = read_int(hwmon + "/power1_average") or read_int(hwmon + "/power1_input")
        if temp is not None:
            result["temp"] = round(temp / 1000, 1)
        if power is not None:
            result["power"] = round(power / 1e6, 1)
    return result


def mountpoint(path: str) -> str:
    path = os.path.realpath(path)
    while not os.path.ismount(path):
        path = os.path.dirname(path)
    return path


def disks(engines: dict[str, dict]) -> list[dict]:
    paths = {"/"}
    for e in engines.values():
        paths.update(t["path"] for t in e["targets"])
        paths.update(e["models_dirs"])
    mounts = sorted({mountpoint(p) for p in paths if os.path.exists(p)})
    result = []
    for mount in mounts:
        usage = shutil.disk_usage(mount)
        result.append({"path": mount, "total": usage.total, "used": usage.used,
                       "free": usage.free})
    return result


def build_status() -> dict:
    engines, errors = catalog.load()
    states = engine_states(engines)
    snap = ollama_view.snapshot()
    items = []

    for m in snap["models"]:
        item_id = "ollama:" + m["name"]
        loaded = snap["loaded"].get(m["name"])
        if not snap["up"]:
            state = "unavailable"
        else:
            state = "running" if loaded else "stopped"
        items.append({
            "id": item_id, "name": m["name"], "type": model_type(m["capabilities"]),
            "runtime": "Ollama", "state": state, "size": m["size"],
            "detail": " · ".join(x for x in (m["family"], m["params"], m["quant"]) if x),
            "tags": [c for c in m["capabilities"] if c in ("vision", "tools", "thinking")],
            "vram": (loaded or {}).get("size_vram"),
            "until": (loaded or {}).get("expires_at"),
            "conflicts": conflicts_for(item_id, engines, states, snap["loaded"]),
        })

    for eid, e in engines.items():
        item_id = "engine:" + eid
        size, files = 0, []
        for directory in e["models_dirs"]:
            dir_total, names = dir_size_and_files(directory)
            size += dir_total
            files += names
        items.append({
            "id": item_id, "name": e["name"], "type": e["type"],
            "runtime": e["runtime"], "state": states[eid], "size": size,
            "detail": e["note"], "tags": [], "files": files,
            "has_model_dirs": bool(e["models_dirs"]), "url": e["url"],
            "port": e["port"], "removable": e["managed_by"] == "ui",
            "conflicts": conflicts_for(item_id, engines, states, snap["loaded"]),
        })

    active_engine = next((engines[eid]["name"] for eid, s in states.items()
                          if s in ACTIVE_STATES and engines[eid]["exclusive"]), "")
    return {
        "agent_version": VERSION,
        "hostname": socket.gethostname(),
        "gpu": gpu(),
        "cpu": {"percent": cpu_percent(), "temp": cpu_temp(),
                "cores": os.cpu_count()},
        "ram": memory(),
        "disks": disks(engines),
        "ollama": {"up": snap["up"], "version": snap["version"]},
        "active_engine": active_engine,
        "items": items,
        "jobs": jobs.list(),
        "targets": list(all_targets(engines).values()),
        "catalog_errors": errors,
    }


# ── HTTP ────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "model-agent"
    sys_version = ""

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(supplied.encode(), f"Bearer {args.token}".encode())

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise AgentError("Bad Content-Length")
        if length > MAX_BODY:
            raise AgentError("Request too large", 413)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            raise AgentError("Body must be JSON")
        if not isinstance(data, dict):
            raise AgentError("Body must be a JSON object")
        return data

    def _dispatch(self, method: str) -> None:
        try:
            if not self._authorized():
                self._send(401, {"error": "Unauthorized"})
                return
            path = urllib.parse.urlsplit(self.path).path
            self._send(200, self._route(method, path))
        except AgentError as exc:
            self._send(exc.status, {"error": str(exc), **exc.extra})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _route(self, method: str, path: str) -> dict:
        parts = [p for p in path.split("/") if p]
        if method == "GET" and parts == ["status"]:
            return build_status()
        if method == "POST" and parts == ["models", "start"]:
            return api_start(self._body())
        if method == "POST" and parts == ["models", "stop"]:
            return api_stop(self._body())
        if method == "POST" and parts == ["download"]:
            return api_download(self._body())
        if method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel":
            jobs.request_cancel(parts[1])
            return {"cancelling": parts[1]}
        if method == "POST" and parts == ["engines"]:
            return api_add_engine(self._body())
        if method == "DELETE" and len(parts) == 2 and parts[0] == "engines":
            return api_remove_engine(parts[1])
        raise AgentError("Not found", 404)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def log_message(self, *_a) -> None:
        pass


def main() -> None:
    global args
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--bind", required=True)
    parser.add_argument("--port", type=int, default=9872)
    parser.add_argument("--etc", default="/etc/model-agent")
    parser.add_argument("--ollama", default="http://127.0.0.1:11434")
    args = parser.parse_args()
    args.ollama = args.ollama.rstrip("/")
    args.token = (read_text(os.path.join(args.etc, "token")) or "").strip()
    if len(args.token) < 16:
        raise SystemExit(f"missing or short token in {args.etc}/token")
    os.makedirs(os.path.join(args.etc, "engines.d"), exist_ok=True)
    ThreadingHTTPServer((args.bind, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
