# Changelog

## 1.3.0
- llama.cpp: the agent lists every model of a llama.cpp router (`llama-server --models-dir`) beside the Ollama models. Start loads a model, Stop unloads it, and the badge shows Loading, Ready and Failed.
- Downloads go into the router's models folder (`--llama-models`), and the router picks up the new file on its own. Ollama pulls work as before.
- Hugging Face: gated files download with the token from `HF_TOKEN` or `~/.cache/huggingface/token` on the GPU machine. A `/blob/` link copied from the browser works too.
- A machine without Ollama: `--ollama off`. The Ollama download option and the "Ollama is stopped" notice then go away.
- Exclusive engines also unload the llama.cpp models before they start.
- `install.sh` takes the extra agent arguments as a third parameter. Agent 1.3.0; update the agent together with the plugin.

## 1.2.0
- Every setting is now in Settings -> Plugins -> Configure, grouped in sections: Agent (URL, token), Wake (host id) and Timing (status cache, status timeout, action timeout).
- The agent URL field is a URL field and is validated.
- Existing configs are migrated automatically (whitespace and a trailing slash are removed).
- Requires Pi Hub 8.3.0.
