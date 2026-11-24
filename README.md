<p align="center"><img src=".github/hero.svg" alt="studio" width="880"></p>

<div align="center">

# Hanzo Studio
**The most powerful and modular visual AI engine and application.**


[![Website][website-shield]][website-url]
[![Dynamic JSON Badge][discord-shield]][discord-url]
[![Twitter][twitter-shield]][twitter-url]
<br>
[![][github-release-shield]][github-release-link]
[![][github-release-date-shield]][github-release-link]
[![][github-downloads-shield]][github-downloads-link]
[![][github-downloads-latest-shield]][github-downloads-link]

[website-shield]: https://img.shields.io/badge/studio.hanzo.ai-0A0A0A?style=flat
[website-url]: https://studio.hanzo.ai/
<!-- Workaround to display total user from https://github.com/badges/shields/issues/4500#issuecomment-2060079995 -->
[discord-shield]: https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdiscord.com%2Fapi%2Finvites%2Fhanzoai%3Fwith_counts%3Dtrue&query=%24.approximate_member_count&logo=discord&logoColor=white&label=Discord&color=green&suffix=%20total
[discord-url]: https://discord.gg/CJCyAsm9Vr
[twitter-shield]: https://img.shields.io/twitter/follow/hanaboroai
[twitter-url]: https://x.com/hanzoai

[github-release-shield]: https://img.shields.io/github/v/release/hanzoai/studio?style=flat&sort=semver
[github-release-link]: https://github.com/hanzoai/studio/releases
[github-release-date-shield]: https://img.shields.io/github/release-date/hanzoai/studio?style=flat
[github-downloads-shield]: https://img.shields.io/github/downloads/hanzoai/studio/total?style=flat
[github-downloads-latest-shield]: https://img.shields.io/github/downloads/hanzoai/studio/latest/total?style=flat&label=downloads%40latest
[github-downloads-link]: https://github.com/hanzoai/studio/releases

![Hanzo Studio Screenshot](https://github.com/user-attachments/assets/7ccaf2c1-9b72-41ae-9a89-5688c94b7abe)
</div>

**`hanzoai/studio`** is the open-source **Hanzo Studio** visual AI execution and node workflow framework (Python diffusion, video, audio, custom nodes, and API runtime) for building generative pipelines and extensions on top of Hanzo Studio.

- **Hosted Creative Suite (Design, Director & Workflows):** [**studio.hanzo.ai**](https://studio.hanzo.ai)
- **Native Desktop Downloads (macOS `.dmg`, Windows `.exe`, Linux `.AppImage` / `.deb`):** [**github.com/hanzoai/studio/releases**](https://github.com/hanzoai/studio/releases)

Hanzo Studio lets you design and execute advanced multimodal diffusion and video pipelines using a graph/nodes/flowchart runtime on Windows, Linux, and macOS.

## Upstream & License

Hanzo Studio is a fork of [**ComfyUI**](https://github.com/comfyanonymous/ComfyUI) by comfyanonymous and the ComfyUI contributors — full credit to the upstream project for the engine this builds on. ComfyUI is licensed under the **GNU General Public License v3.0 (GPL-3.0)**, and this fork stays under the same license. All modifications in this fork are likewise GPL-3.0.

See [`LICENSE`](LICENSE) for the full license text and [`NOTICE`](NOTICE) for the fork's attribution and a summary of what it changes (branding, Hanzo Engine integration nodes, and fashion workflow templates).

## Get Started

#### [Manual Install](#manual-install-windows-linux)
Supports all operating systems and GPU types (NVIDIA, AMD, Intel, Apple Silicon, Ascend).

#### Docker (Recommended for servers)
```bash
docker pull ghcr.io/hanzoai/studio:latest
docker run -p 8188:8188 ghcr.io/hanzoai/studio:latest --listen --cpu
```

## Features
- Nodes/graph/flowchart interface to experiment and create complex Stable Diffusion workflows without needing to code anything.
- Image Models
   - SD1.x, SD2.x, SDXL, SDXL Turbo
   - Stable Cascade, SD3 and SD3.5
   - Pixart Alpha and Sigma, AuraFlow, HunyuanDiT
   - Flux, Flux 2, Lumina Image 2.0, HiDream
   - Qwen Image, Hunyuan Image 2.1, Z Image
- Image Editing Models
   - Omnigen 2, Flux Kontext, HiDream E1.1, Qwen Image Edit
- Video Models
   - Stable Video Diffusion, Mochi, LTX-Video
   - Hunyuan Video, Wan 2.1, Wan 2.2, Hunyuan Video 1.5
- Audio Models
   - Stable Audio, ACE Step
- 3D Models
   - Hunyuan3D 2.0
- Asynchronous Queue system
- Many optimizations: Only re-executes the parts of the workflow that changes between executions.
- Smart memory management: can automatically run large models on GPUs with as low as 1GB vram with smart offloading.
- Works even if you don't have a GPU with: ```--cpu``` (slow)
- Can load ckpt and safetensors: All in one checkpoints or standalone diffusion models, VAEs and CLIP models.
- Safe loading of ckpt, pt, pth, etc.. files.
- Embeddings/Textual inversion
- Loras (regular, locon and loha)
- Hypernetworks
- Loading full workflows (with seeds) from generated PNG, WebP and FLAC files.
- Saving/Loading workflows as Json files.
- Nodes interface can be used to create complex workflows like one for Hires fix or much more advanced ones.
- Area Composition
- Inpainting with both regular and inpainting models.
- ControlNet and T2I-Adapter
- Upscale Models (ESRGAN, ESRGAN variants, SwinIR, Swin2SR, etc...)
- GLIGEN
- Model Merging
- LCM models and Loras
- Latent previews with TAESD
- Works fully offline: core will never download anything unless you want to.
- Optional API nodes to use paid models from external providers through the online API. Disable with: `--disable-api-nodes`
- [Config file](extra_model_paths.yaml.example) to set the search paths for models.

## Shortcuts

| Keybind                            | Explanation                                                                                                        |
|------------------------------------|--------------------------------------------------------------------------------------------------------------------|
| `Ctrl` + `Enter`                      | Queue up current graph for generation                                                                              |
| `Ctrl` + `Shift` + `Enter`              | Queue up current graph as first for generation                                                                     |
| `Ctrl` + `Alt` + `Enter`                | Cancel current generation                                                                                          |
| `Ctrl` + `Z`/`Ctrl` + `Y`                 | Undo/Redo                                                                                                          |
| `Ctrl` + `S`                          | Save workflow                                                                                                      |
| `Ctrl` + `O`                          | Load workflow                                                                                                      |
| `Ctrl` + `A`                          | Select all nodes                                                                                                   |
| `Alt `+ `C`                           | Collapse/uncollapse selected nodes                                                                                 |
| `Ctrl` + `M`                          | Mute/unmute selected nodes                                                                                         |
| `Ctrl` + `B`                           | Bypass selected nodes (acts like the node was removed from the graph and the wires reconnected through)            |
| `Delete`/`Backspace`                   | Delete selected nodes                                                                                              |
| `Ctrl` + `Backspace`                   | Delete the current graph                                                                                           |
| `Space`                              | Move the canvas around when held and moving the cursor                                                             |
| `Ctrl`/`Shift` + `Click`                 | Add clicked node to selection                                                                                      |
| `Ctrl` + `C`/`Ctrl` + `V`                  | Copy and paste selected nodes (without maintaining connections to outputs of unselected nodes)                     |
| `Ctrl` + `C`/`Ctrl` + `Shift` + `V`          | Copy and paste selected nodes (maintaining connections from outputs of unselected nodes to inputs of pasted nodes) |
| `Shift` + `Drag`                       | Move multiple selected nodes at the same time                                                                      |
| `Ctrl` + `D`                           | Load default graph                                                                                                 |
| `Alt` + `+`                          | Canvas Zoom in                                                                                                     |
| `Alt` + `-`                          | Canvas Zoom out                                                                                                    |
| `Ctrl` + `Shift` + LMB + Vertical drag | Canvas Zoom in/out                                                                                                 |
| `P`                                  | Pin/Unpin selected nodes                                                                                           |
| `Ctrl` + `G`                           | Group selected nodes                                                                                               |
| `Q`                                 | Toggle visibility of the queue                                                                                     |
| `H`                                  | Toggle visibility of history                                                                                       |
| `R`                                  | Refresh graph                                                                                                      |
| `F`                                  | Show/Hide menu                                                                                                      |
| `.`                                  | Fit view to selection (Whole graph when nothing is selected)                                                        |
| Double-Click LMB                   | Open node quick search palette                                                                                     |
| `Shift` + Drag                       | Move multiple wires at once                                                                                        |
| `Ctrl` + `Alt` + LMB                   | Disconnect all wires from clicked slot                                                                             |

`Ctrl` can also be replaced with `Cmd` instead for macOS users

# Installing

## Manual Install (Windows, Linux)

Python 3.14 works but some custom nodes may have issues. The free threaded variant works but some dependencies will enable the GIL so it's not fully supported.

Python 3.13 is very well supported. If you have trouble with some custom node dependencies on 3.13 you can try 3.12

torch 2.4 and above is supported but some features and optimizations might only work on newer versions. We generally recommend using the latest major version of pytorch with the latest cuda version unless it is less than 2 weeks old.

### Instructions:

Git clone this repo.

Put your SD checkpoints (the huge ckpt/safetensors files) in: models/checkpoints

Put your VAE in: models/vae


### AMD GPUs (Linux)

AMD users can install rocm and pytorch with pip if you don't have it already installed, this is the command to install the stable version:

```pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/rocm7.1```

This is the command to install the nightly with ROCm 7.2 which might have some performance improvements:

```pip install --pre torch torchvision torchaudio --index-url https://download.pytorch.org/whl/nightly/rocm7.2```


### AMD GPUs (Experimental: Windows and Linux), RDNA 3, 3.5 and 4 only.

These have less hardware support than the builds above but they work on windows. You also need to install the pytorch version specific to your hardware.

RDNA 3 (RX 7000 series):

```pip install --pre torch torchvision torchaudio --index-url https://rocm.nightlies.amd.com/v2/gfx110X-all/```

RDNA 3.5 (Strix halo/Ryzen AI Max+ 365):

```pip install --pre torch torchvision torchaudio --index-url https://rocm.nightlies.amd.com/v2/gfx1151/```

RDNA 4 (RX 9000 series):

```pip install --pre torch torchvision torchaudio --index-url https://rocm.nightlies.amd.com/v2/gfx120X-all/```

### Intel GPUs (Windows and Linux)

Intel Arc GPU users can install native PyTorch with torch.xpu support using pip. More information can be found [here](https://pytorch.org/docs/main/notes/get_start_xpu.html)

1. To install PyTorch xpu, use the following command:

```pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/xpu```

This is the command to install the Pytorch xpu nightly which might have some performance improvements:

```pip install --pre torch torchvision torchaudio --index-url https://download.pytorch.org/whl/nightly/xpu```

### NVIDIA

Nvidia users should install stable pytorch using this command:

```pip install torch torchvision torchaudio --extra-index-url https://download.pytorch.org/whl/cu130```

This is the command to install pytorch nightly instead which might have performance improvements.

```pip install --pre torch torchvision torchaudio --index-url https://download.pytorch.org/whl/nightly/cu130```

#### Troubleshooting

If you get the "Torch not compiled with CUDA enabled" error, uninstall torch with:

```pip uninstall torch```

And install it again with the command above.

### Dependencies

Install the dependencies by opening your terminal inside the Hanzo Studio folder and:

```pip install -r requirements.txt```

After this you should have everything installed and can proceed to running Hanzo Studio.

### Others:

#### Apple Mac silicon

You can install Hanzo Studio in Apple Mac silicon (M1 or M2) with any recent macOS version.

1. Install pytorch nightly. For instructions, read the [Accelerated PyTorch training on Mac](https://developer.apple.com/metal/pytorch/) Apple Developer guide (make sure to install the latest pytorch nightly).
1. Follow the [Hanzo Studio manual installation](#manual-install-windows-linux) instructions for Windows and Linux.
1. Install the Hanzo Studio [dependencies](#dependencies). If you have another Stable Diffusion UI you might be able to reuse the dependencies.
1. Launch Hanzo Studio by running `python main.py`

> **Note**: Remember to add your models, VAE, LoRAs etc. to the corresponding folders, as discussed in [Hanzo Studio manual installation](#manual-install-windows-linux).

#### Ascend NPUs

For models compatible with Ascend Extension for PyTorch (torch_npu). To get started, ensure your environment meets the prerequisites outlined on the [installation](https://ascend.github.io/docs/sources/ascend/quick_install.html) page. Here's a step-by-step guide tailored to your platform and installation method:

1. Begin by installing the recommended or newer kernel version for Linux as specified in the Installation page of torch-npu, if necessary.
2. Proceed with the installation of Ascend Basekit, which includes the driver, firmware, and CANN, following the instructions provided for your specific platform.
3. Next, install the necessary packages for torch-npu by adhering to the platform-specific instructions on the [Installation](https://ascend.github.io/docs/sources/pytorch/install.html#pytorch) page.
4. Finally, adhere to the [Hanzo Studio manual installation](#manual-install-windows-linux) guide for Linux. Once all components are installed, you can run Hanzo Studio as described earlier.

#### Cambricon MLUs

For models compatible with Cambricon Extension for PyTorch (torch_mlu). Here's a step-by-step guide tailored to your platform and installation method:

1. Install the Cambricon CNToolkit by adhering to the platform-specific instructions on the [Installation](https://www.cambricon.com/docs/sdk_1.15.0/cntoolkit_3.7.2/cntoolkit_install_3.7.2/index.html)
2. Next, install the PyTorch(torch_mlu) following the instructions on the [Installation](https://www.cambricon.com/docs/sdk_1.15.0/cambricon_pytorch_1.17.0/user_guide_1.9/index.html)
3. Launch Hanzo Studio by running `python main.py`

#### Iluvatar Corex

For models compatible with Iluvatar Extension for PyTorch. Here's a step-by-step guide tailored to your platform and installation method:

1. Install the Iluvatar Corex Toolkit by adhering to the platform-specific instructions on the [Installation](https://support.iluvatar.com/#/DocumentCentre?id=1&nameCenter=2&productId=520117912052801536)
2. Launch Hanzo Studio by running `python main.py`


## Manager

The **Manager** extension allows you to easily install, update, and manage custom nodes for Hanzo Studio.

### Setup

1. Install the manager dependencies:
   ```bash
   pip install -r manager_requirements.txt
   ```

2. Enable the manager with the `--enable-manager` flag when running Hanzo Studio:
   ```bash
   python main.py --enable-manager
   ```

### Command Line Options

| Flag | Description |
|------|-------------|
| `--enable-manager` | Enable the Manager |
| `--enable-manager-legacy-ui` | Use the legacy manager UI instead of the new UI (requires `--enable-manager`) |
| `--disable-manager-ui` | Disable the manager UI and endpoints while keeping background features like security checks and scheduled installation completion (requires `--enable-manager`) |


# Running

```python main.py```

### For AMD cards not officially supported by ROCm

Try running it with this command if you have issues:

For 6700, 6600 and maybe other RDNA2 or older: ```HSA_OVERRIDE_GFX_VERSION=10.3.0 python main.py```

For AMD 7600 and maybe other RDNA3 cards: ```HSA_OVERRIDE_GFX_VERSION=11.0.0 python main.py```

### AMD ROCm Tips

You can enable experimental memory efficient attention on recent pytorch on some AMD GPUs using this command, it should already be enabled by default on RDNA3. If this improves speed for you on latest pytorch on your GPU please report it so that we can enable it by default.

```TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1 python main.py --use-pytorch-cross-attention```

You can also try setting this env variable `PYTORCH_TUNABLEOP_ENABLED=1` which might speed things up at the cost of a very slow initial run.

# Notes

Only parts of the graph that have an output with all the correct inputs will be executed.

Only parts of the graph that change from each execution to the next will be executed, if you submit the same graph twice only the first will be executed. If you change the last part of the graph only the part you changed and the part that depends on it will be executed.

Dragging a generated png on the webpage or loading one will give you the full workflow including seeds that were used to create it.

You can use () to change emphasis of a word or phrase like: (good code:1.2) or (bad code:0.8). The default emphasis for () is 1.1. To use () characters in your actual prompt escape them like \( or \).

You can use {day|night}, for wildcard/dynamic prompts. With this syntax "{wild|card|test}" will be randomly replaced by either "wild", "card" or "test" by the frontend every time you queue the prompt. To use {} characters in your actual prompt escape them like: \{ or \}.

Dynamic prompts also support C-style comments, like `// comment` or `/* comment */`.

To use a textual inversion concepts/embeddings in a text prompt put them in the models/embeddings directory and use them in the CLIPTextEncode node like this (you can omit the .pt extension):

```embedding:embedding_filename.pt```


## How to show high-quality previews?

Use ```--preview-method auto``` to enable previews.

The default installation includes a fast latent preview method that's low-resolution. To enable higher-quality previews with [TAESD](https://github.com/madebyollin/taesd), download the [taesd_decoder.pth, taesdxl_decoder.pth, taesd3_decoder.pth and taef1_decoder.pth](https://github.com/madebyollin/taesd/) and place them in the `models/vae_approx` folder. Once they're installed, restart Hanzo Studio and launch it with `--preview-method taesd` to enable high-quality previews.

## How to use TLS/SSL?
Generate a self-signed certificate (not appropriate for shared/production use) and key by running the command: `openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem -sha256 -days 3650 -nodes -subj "/C=XX/ST=StateName/L=CityName/O=CompanyName/OU=CompanySectionName/CN=CommonNameOrHostname"`

Use `--tls-keyfile key.pem --tls-certfile cert.pem` to enable TLS/SSL, the app will now be accessible with `https://...` instead of `http://...`.

> Note: Windows users can use [alexisrolland/docker-openssl](https://github.com/alexisrolland/docker-openssl) or one of the [3rd party binary distributions](https://wiki.openssl.org/index.php/Binaries) to run the command example above.
<br/><br/>If you use a container, note that the volume mount `-v` can be a relative path so `... -v ".\:/openssl-certs" ...` would create the key & cert files in the current directory of your command prompt or powershell terminal.

## Support and dev channel

[Discord](https://discord.gg/CJCyAsm9Vr): Try the #help or #feedback channels.

See also: [https://www.hanzo.ai/](https://www.hanzo.ai/)
