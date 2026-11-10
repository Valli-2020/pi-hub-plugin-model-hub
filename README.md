# Pi Hub Plugin: Model Hub

One tab to run a GPU machine's AI models: start and stop them, watch the
hardware, download new ones. Text, image, audio, video or anything else.

## What it does

- **All models in one list**, filterable by type. Ollama models appear on their
  own; everything else is an *engine* (below).
- **Start and stop** with one click. If something else holds the GPU, the first
  click says what will be stopped and the second click does it. Stopping the
  last GPU engine brings Ollama back.
- **Monitor** video memory, GPU load, temperature, power, CPU, memory, disk.
- **Download** Ollama models by name, or any file over https into a folder an
  engine declares, with progress, cancel and resume.
- **Wake** the machine when it is off.

## Install

1. On the GPU machine: `sudo ./agent/install.sh <bind-ip> [engines-dir]`.
   Use a LAN or VPN address. The token is in `/etc/model-agent/token`.
2. In Pi Hub: add this repository under **Settings → Plugins**, install and
   enable it, and approve `ui.frame` and `hosts.wake`.
3. **Configure**: agent URL `http://<bind-ip>:9872`, the token, and the Pi Hub
   host id for Wake (optional).

## Adding new things

| New thing | What to do |
|---|---|
| Ollama model | Download it in the tab. It appears by itself. |
| Model file for an engine | Download it in the tab, or copy it into one of the engine's `models_dirs`. |
| A systemd service | **Add an engine** in the tab: name, type, service. |
| A plain command | One JSON file in `/etc/model-agent/engines.d/`. |

Files are picked up within seconds, no restart. The `type` is free text, and a
new type gets its own filter. Examples are in `agent/engines.example/`.

| Field | Meaning |
|---|---|
| `name`, `type` | Required. The file name is the engine's id. |
| `unit` or `run` | Required, one of them. `unit`: a systemd service. `run`: `argv`, plus optional `cwd`, `env`, `memory_max`; the agent runs it as a transient service. |
| `runtime`, `note` | Shown in the list. |
| `port`, `url` | Running means this port answers. An IP in `url` is where the port is checked. |
| `exclusive` | Default `true`: needs the whole GPU, so Ollama and other exclusive engines stop first. |
| `start_timeout` | Seconds until a start counts as failed (default 180). |
| `models_dirs` | Folders whose large files count as this engine's models. |
| `targets` | Download folders: `[{"label": "VAE", "path": "/opt/x/vae"}]`. |

## Security

- The agent runs as root because it controls services and writes model files.
  Every request needs the Bearer token, sent over plain HTTP, so bind it to a
  LAN or VPN address and keep the port off the internet.
- The token stays on the Pi Hub server. The tab runs in a sandboxed frame and
  only reaches this plugin's routes.
- Actions are admin-only. Viewers see the monitor and the list.
- The dashboard form only accepts existing systemd services and refuses system
  ones such as ssh or Tailscale. Commands can only be defined on the machine.

## Development

```sh
python3 tests/test_agent.py                                     # agent, fake machine
PYTHONPATH=<pi-hub checkout> python3 tests/harness.py fake x tests/browser.mjs   # real dashboard
```

The browser test needs Playwright; set `PLAYWRIGHT_ROOT` to the folder that
holds its `node_modules`.

## License

MIT
