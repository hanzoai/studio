# Hanzo Engine nodes

First-party Hanzo Studio nodes that run AI workloads on the native-Rust
**Hanzo Engine** instead of in-process Python. Each node is one call to the
engine's OpenAI-compatible HTTP API; the shared transport and Studio<->engine
type conversions live in `client.py`, so every node stays a few lines and a new
engine endpoint becomes a new small node with no framework change.

## Configuration

One way to point at an engine: the base URL, default `http://127.0.0.1:1234`,
overridable by env. Paths are always `/v1/*` (never `/api/*`); a trailing `/v1`
on the base URL is tolerated.

```bash
export HANZO_ENGINE_URL=http://127.0.0.1:1234
hanzo engine serve -m Qwen/Qwen3-4B --port 1234   # or any local path
```

Engine-down and protocol failures raise a clear error on the failing node; the
server keeps running.

## Nodes (category: `Hanzo/Engine`)

| Node | Endpoint | In -> Out |
|------|----------|-----------|
| **Hanzo Chat (Engine)** | `POST /v1/chat/completions` (streams) | model + prompt -> `text` |
| **Hanzo Image Gen (Engine)** | `POST /v1/images/generations` | prompt + width/height -> `IMAGE` |
| **Hanzo TTS (Engine)** | `POST /v1/audio/speech` | text -> `AUDIO` |
| **Hanzo ASR (Engine)** | `POST /v1/audio/transcriptions` | `AUDIO` -> `text` |
| **Hanzo Engine Request** | any `/v1` path | method + path + JSON -> `response` |
| **Hanzo Vision Caption (Engine)** | `POST /v1/chat/completions` (vision) | `IMAGE` + prompt -> `caption` |
| **Hanzo Text to Video (Engine)** | async `/v1/videos` | prompt -> `IMAGE` frames |
| **Hanzo Image to 3D (Engine)** | async `/v1/3d` | `IMAGE` -> mesh path |
| **Hanzo Save Text** | -- | `text` -> writes `output/*.txt` |

- **Chat** streams tokens over SSE, driving the node progress bar, and returns
  the full text (also previewed on the node). `model` is a dropdown populated
  from `/v1/models`; `default` always routes to the loaded model.
- **Image Gen** sends the engine's `width`/`height`/`n` schema and decodes the
  `b64_json` (or `url`) response into a Studio `IMAGE`.
- **TTS/ASR** speak the Studio `AUDIO` type (`{waveform, sample_rate}`) and
  compose with the built-in Load/Save/Preview Audio nodes.
- **Engine Request** is the forward-compatible escape hatch: reach any endpoint
  (music, dub, ...) the day it ships, then promote it to a dedicated node here.
- **Text to Video / Image to 3D** use the engine's async job protocol
  (`submit -> poll -> fetch content`) and report progress.

## Requirements

Only libraries already in Hanzo Studio: `requests`, `Pillow`, `numpy`, `torch`
(always), plus `av`/`torchaudio` (loaded lazily, for the audio/video nodes).
No extra install.

## Example

`user/default/workflows/hanzo-native-pipeline.json` -- Chat writes an image
prompt, Image Gen renders it, the image and the prompt text are saved. All
compute runs on the native engine.
