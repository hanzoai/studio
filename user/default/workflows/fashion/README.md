# Fashion / swimwear starter workflows

Three starter templates for a designer's swimwear workflow. They live in the
Hanzo Studio workflow directory, so they appear in the **Workflows** sidebar
(Workflows → Browse). You can also **drag any `.json` here onto the canvas** to
load it.

Each workflow contains a **Note** node describing usage and the model files it
needs. Some required files (Flux.2 text encoder / VAE, Qwen-Image-Edit) may not
be downloaded yet — the graph is prewired so you only fill in filenames.

| File | Pipeline | Purpose |
|------|----------|---------|
| `fashion_product_shot.json` | FLUX.2 klein, text-to-image | Editorial swimwear product photography from a prompt |
| `fashion_edit_garment.json` | Qwen-Image-Edit, image-to-image | Re-scene / edit a garment shot while keeping the garment identical |
| `fashion_caption_dataset.json` | Hanzo Vision Caption → Save Text | Caption images into `.txt` pairs for LoRA dataset prep |

## Notes

- `fashion_product_shot.json` — `flux-2-klein-4b.safetensors` is already in
  `models/diffusion_models/`. Add a Flux.2 text encoder (`type=flux2` on the
  CLIPLoader) and a Flux.2 VAE, then queue. Output lands in `output/swimwear/`.
- `fashion_edit_garment.json` — uses the native `TextEncodeQwenImageEdit` path.
  Load a product shot, describe the edit, keep the garment unchanged.
- `fashion_caption_dataset.json` — uses the Hanzo Engine nodes
  (`custom_nodes/hanzo_engine/`). Set `HANZO_ENGINE_URL` and run a vision model
  such as Qwen3-VL. Image/caption pairs land in `output/swimwear/`.
