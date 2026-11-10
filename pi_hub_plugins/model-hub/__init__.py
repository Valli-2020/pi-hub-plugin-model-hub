"""Model Hub plugin (Plugin API v2): control and monitor local AI models.

The Python side is a thin, authenticated proxy.  A plugin cannot run commands
on another host, so the real work is done by ``model-agent`` on the GPU
machine (see ``agent/`` in the plugin repository).  The agent token stays on
the server: the sandboxed frame only ever talks to this plugin's routes.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from pi_hub.plugins.base import FrameDef, Plugin, PluginContext, RouteDef

log = logging.getLogger(__name__)

STATUS_TTL_S = 2.0
STATUS_TIMEOUT_S = 4
ACTION_TIMEOUT_S = 15
JOB_ID_RE = re.compile(r"[a-f0-9]{1,32}")
ENGINE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,40}")

ICON_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="5" y="5" width="14" height="14" rx="2"/>'
    '<rect x="9" y="9" width="6" height="6" rx="1"/>'
    '<path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/></svg>'
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The agent never redirects; following one would forward the token."""

    def redirect_request(self, *_args, **_kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


class ModelHubPlugin(Plugin):
    name = "model-hub"
    version = "1.0.1"
    description = ("Start, stop, download and monitor every local AI model on "
                   "a GPU machine: text, image, audio and anything you add")
    min_core_version = "8.0.0"
    plugin_api_version = 2
    capabilities = ["ui.frame", "hosts.wake"]

    def load(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._cache_lock = threading.Lock()
        self._cache: tuple[float, dict] | None = None

    # ── Descriptors ────────────────────────────────────────────────────────

    def get_config_schema(self) -> list[dict]:
        return [
            {"name": "agent_url", "label": "Agent URL", "type": "text",
             "required": True, "placeholder": "http://192.168.1.50:9872",
             "help": "Address of model-agent on the GPU machine."},
            {"name": "token", "label": "Agent token", "type": "password",
             "help": "Contents of /etc/model-agent/token on the GPU machine. "
                     "Left blank keeps the current token."},
            {"name": "host_id", "label": "Pi Hub host id (for Wake)",
             "type": "text", "placeholder": "gpu-box",
             "help": "Host with a MAC address in Pi Hub. Empty hides Wake."},
        ]

    def on_config_change(self, old: dict, new: dict) -> None:
        with self._cache_lock:
            self._cache = None

    def get_routes(self) -> list[RouteDef]:
        admin = ["admin"]
        return [
            RouteDef("GET", "/status", self.status),
            RouteDef("POST", "/models/start", self.start, caps=admin),
            RouteDef("POST", "/models/stop", self.stop, caps=admin),
            RouteDef("POST", "/download", self.download, caps=admin),
            RouteDef("POST", "/jobs/{id}/cancel", self.cancel, caps=admin),
            RouteDef("POST", "/engines", self.add_engine, caps=admin),
            RouteDef("DELETE", "/engines/{id}", self.remove_engine, caps=admin),
            RouteDef("POST", "/wake", self.wake, caps=admin),
        ]

    def get_frames(self) -> list[FrameDef]:
        return [FrameDef(
            "models",
            entry=["models.js"], css=["models.css"],
            surfaces=[{"type": "tab", "label": "Models", "icon_svg": ICON_SVG}],
            height=640,
        )]

    # ── Agent access ───────────────────────────────────────────────────────

    def _settings(self) -> tuple[str, str]:
        cfg = self.ctx.get_config()
        return (str(cfg.get("agent_url") or "").strip().rstrip("/"),
                str(cfg.get("token") or "").strip())

    def _agent(self, method: str, path: str, body: dict | None,
               timeout: float) -> tuple[dict, int]:
        url, token = self._settings()
        if not url or not token:
            return {"error": "Set the agent URL and token in the plugin settings"}, 400
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            return {"error": "The agent URL must start with http:// or https://"}, 400
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url + path, data=data, method=method,
                                     headers=headers)
        try:
            with _opener.open(req, timeout=timeout) as resp:
                return json.load(resp), 200
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read() or b"{}")
            except ValueError:
                payload = {}
            if exc.code == 401:
                return {"error": "The agent rejected the token"}, 502
            if not payload.get("error"):
                payload = {"error": f"Agent answered HTTP {exc.code}"}
            return payload, exc.code
        except (OSError, ValueError) as exc:
            log.info("model-agent unreachable: %s", exc)
            return {"error": "The agent does not answer"}, 502

    def _forward(self, method: str, path: str, body: dict | None = None):
        result = self._agent(method, path, body, ACTION_TIMEOUT_S)
        with self._cache_lock:
            self._cache = None
        return result

    # ── Routes ─────────────────────────────────────────────────────────────

    def _snapshot(self) -> dict:
        with self._cache_lock:
            if self._cache and time.monotonic() - self._cache[0] < STATUS_TTL_S:
                return self._cache[1]
            data, code = self._agent("GET", "/status", None, STATUS_TIMEOUT_S)
            snapshot: dict[str, Any]
            if code == 200:
                snapshot = {"reachable": True, **data}
            else:
                snapshot = {"reachable": False,
                            "error": data.get("error", "The agent does not answer")}
            self._cache = (time.monotonic(), snapshot)
            return snapshot

    def status(self, session: dict) -> dict:
        snapshot = dict(self._snapshot())
        snapshot["can_wake"] = bool(str(self.ctx.get_config().get("host_id") or "").strip())
        if session.get("role") != "admin":
            snapshot.pop("targets", None)   # paths on the machine are admin-only
            snapshot["can_wake"] = False
        return snapshot

    def start(self, body: dict):
        return self._forward("POST", "/models/start", body or {})

    def stop(self, body: dict):
        return self._forward("POST", "/models/stop", body or {})

    def download(self, body: dict):
        return self._forward("POST", "/download", body or {})

    def cancel(self, params: dict):
        job_id = params["id"]
        if not JOB_ID_RE.fullmatch(job_id):
            return {"error": "Invalid job id"}, 400
        return self._forward("POST", f"/jobs/{job_id}/cancel", {})

    def add_engine(self, body: dict):
        return self._forward("POST", "/engines", body or {})

    def remove_engine(self, params: dict):
        engine_id = params["id"]
        if not ENGINE_ID_RE.fullmatch(engine_id):
            return {"error": "Invalid engine id"}, 400
        return self._forward("DELETE", f"/engines/{engine_id}")

    def wake(self):
        host_id = str(self.ctx.get_config().get("host_id") or "").strip()
        if not host_id:
            return {"error": "No Pi Hub host id configured"}, 400
        if not self.ctx.wake_host(host_id):
            return {"error": "Wake failed. Check the host id and its MAC."}, 502
        return {"success": True}
