As of the time of writing this you need this driver for best results:
https://www.amd.com/en/resources/support-articles/release-notes/RN-AMDGPU-WINDOWS-PYTORCH-7-1-1.html

HOW TO RUN:

If you have a AMD gpu:

run_amd_gpu.bat

If you have memory issues you can try disabling the smart memory management by running Hanzo Studio with:

run_amd_gpu_disable_smart_memory.bat

IF YOU GET A RED ERROR IN THE UI MAKE SURE YOU HAVE A MODEL/CHECKPOINT IN: HanzoStudio\models\checkpoints

You can download the stable diffusion XL one from: https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/blob/main/sd_xl_base_1.0_0.9vae.safetensors


RECOMMENDED WAY TO UPDATE:
To update the Hanzo Studio code: update\update_studio.bat


TO SHARE MODELS BETWEEN HANZO STUDIO AND ANOTHER UI:
In the HanzoStudio directory you will find a file: extra_model_paths.yaml.example
Rename this file to: extra_model_paths.yaml and edit it with your favorite text editor.
