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
| **Hanzo Music (Engine)** | `POST /v1/audio/music` | prompt -> `AUDIO` |
| **Hanzo ASR (Engine)** | `POST /v1/audio/transcriptions` | `AUDIO` -> `text` |
| **Hanzo Engine Request** | any `/v1` path | method + path + JSON -> `response` |
| **Hanzo Vision Caption (Engine)** | `POST /v1/chat/completions` (vision) | `IMAGE` + prompt -> `caption` |
| **Hanzo Text to Video (Engine)** | async `/v1/videos` | prompt -> `IMAGE` frames |
| **Hanzo Image to 3D (Engine)** | async `/v1/3d` | `IMAGE` (+`texture`) -> mesh path |
| **Hanzo Dub / Lip-Sync (Engine)** | `POST /v1/animate` | `IMAGE` + `AUDIO` -> `IMAGE` frames + `AUDIO` |
| **Hanzo Save Text** | -- | `text` -> writes `output/*.txt` |

- **Chat** streams tokens over SSE, driving the node progress bar, and returns
  the full text (also previewed on the node). `model` is a dropdown populated
  from `/v1/models`; `default` always routes to the loaded model.
- **Image Gen** sends the engine's `width`/`height`/`n` schema and decodes the
  `b64_json` (or `url`) response into a Studio `IMAGE`.
- **TTS/Music/ASR** speak the Studio `AUDIO` type (`{waveform, sample_rate}`)
  and compose with the built-in Load/Save/Preview Audio nodes. TTS and Music
  serve `wav`/`pcm` (the engine's synchronous audio formats); Music is stereo
  44.1 kHz ACE-Step.
- **Engine Request** is the forward-compatible escape hatch: reach any endpoint
  the day it ships, then promote it to a dedicated node here.
- **Image to 3D** exposes the `texture` flag: on (default) runs the full
  SLAT + FlexiCubes stage for a textured GLB; off returns the coarse mesh.
- **Text to Video / Image to 3D** use the engine's async job protocol
  (`submit -> poll -> fetch content`) and report progress. **Dub** is
  synchronous: it posts the portrait `IMAGE` + driving `AUDIO` and returns the
  animated frames plus the audio (mux them with a video-combine node).

## Requirements

Only libraries already in Hanzo Studio: `requests`, `Pillow`, `numpy`, `torch`
(always), plus `av` (loaded lazily, for the audio/video decode paths). WAV
encode/decode uses the stdlib `wave` module. No extra install.

## Verification

`contract_check.py` drives every node through real HTTP against a stub engine
that enforces the exact request fields each Rust handler deserializes and the
async job protocol (submit -> poll -> content):

```bash
python custom_nodes/hanzo_engine/contract_check.py
```

## Examples

- `user/default/workflows/hanzo-native-pipeline.json` -- Chat writes an image
  prompt, Image Gen renders it, image + prompt are saved.
- `user/default/workflows/hanzo-full-generative-pipeline.json` -- the flagship
  multimodal pipeline: Chat expands a prompt, which fans out to Image Gen ->
  (textured Image-to-3D **and** WAN Text-to-Video), plus TTS narration and
  ACE-Step Music. One graph, every modality, all on the native engine.
