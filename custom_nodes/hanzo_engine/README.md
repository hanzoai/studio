# Hanzo Engine nodes

Custom nodes that call a local **Hanzo Engine** from a Hanzo Studio graph.
Hanzo Engine is the native-Rust, all-modality inference and training backend
(the training loop lives engine-side); these nodes talk to its
OpenAI-compatible HTTP API.

## Configuration

Set the engine base URL via environment variable (default shown):

```bash
export HANZO_ENGINE_URL=http://127.0.0.1:1234/v1
```

Connection or protocol failures surface as a clear error on the failing node —
the server does not crash.

## Nodes (category: `Hanzo/Engine`)

| Node | Route | In → Out |
|------|-------|----------|
| **Hanzo Chat (Engine)** | `POST /v1/chat/completions` | model + prompt + system → `text` (STRING) |
| **Hanzo Image Gen (Engine)** | `POST /v1/images/generations` | prompt + size + steps → `IMAGE` |
| **Hanzo Vision Caption (Engine)** | `POST /v1/chat/completions` (vision) | `IMAGE` + prompt → `caption` (STRING) |
| **Hanzo Save Text** | — | `text` (STRING) → writes `.txt` to `output/` |

- **Hanzo Chat** — prompt engineering and caption/text chains inside a graph.
- **Hanzo Image Gen** — decodes the engine's `b64_json` (or `url`) response into a
  Hanzo Studio `IMAGE` tensor. Requests `n` images at the chosen `size`.
- **Hanzo Vision Caption** — encodes the input image as an OpenAI `image_url`
  data URL and asks a vision model (e.g. Qwen3-VL) to caption it. Use for
  LoRA/dataset prep.
- **Hanzo Save Text** — writes captions to `output/` as `.txt` (image/caption
  pairs) and previews them in the node. Completes the caption-dataset loop.

## Requirements

Uses only libraries already present in Hanzo Studio: `requests`, `Pillow`,
`numpy`, `torch`. No extra install.
