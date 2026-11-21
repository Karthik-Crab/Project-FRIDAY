"""FRIDAY's built-in ComfyUI workflow: plain text to image with one SDXL checkpoint.

The same graph as ComfyUI's basic text-to-image template, in API format, with
placeholders FRIDAY fills in. Any other workflow can be used instead: export it
from ComfyUI (Workflow > Export (API)) and put ``{{prompt}}`` where the words go.
"""

from __future__ import annotations

from typing import Any


def sdxl_text_to_image() -> dict[str, Any]:
    return {
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "{{checkpoint}}"},
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": "{{width}}", "height": "{{height}}", "batch_size": 1},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "{{prompt}}", "clip": ["4", 1]},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "{{negative}}", "clip": ["4", 1]},
        },
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": "{{seed}}",
                "steps": "{{steps}}",
                "cfg": "{{cfg}}",
                "sampler_name": "dpmpp_2m",
                "scheduler": "karras",
                "denoise": 1.0,
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "FRIDAY", "images": ["8", 0]},
        },
    }


def ace_step_music() -> dict[str, Any]:
    """Text to music with ACE-Step 1.5 (the all-in-one checkpoint).

    The graph of ComfyUI's "ACE-Step 1.5 checkpoint" template, saving MP3.
    ``{{prompt}}`` is the style ("calm piano, rainy evening"), ``{{lyrics}}``
    the words or ``[instrumental]``, ``{{seconds}}`` the length.
    """
    return {
        "97": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "{{checkpoint}}"},
        },
        "78": {
            "class_type": "ModelSamplingAuraFlow",
            "inputs": {"model": ["97", 0], "shift": 3.0},
        },
        "94": {
            "class_type": "TextEncodeAceStepAudio1.5",
            "inputs": {
                "clip": ["97", 1],
                "tags": "{{prompt}}",
                "lyrics": "{{lyrics}}",
                "seed": "{{seed}}",
                "bpm": "{{bpm}}",
                "duration": "{{seconds}}",
                "timesignature": "4",
                "language": "en",
                "keyscale": "{{key}}",
                "generate_audio_codes": True,
                "cfg_scale": 2.0,
                "temperature": 0.85,
                "top_p": 0.9,
                "top_k": 0,
                "min_p": 0.0,
            },
        },
        "47": {
            "class_type": "ConditioningZeroOut",
            "inputs": {"conditioning": ["94", 0]},
        },
        "98": {
            "class_type": "EmptyAceStep1.5LatentAudio",
            "inputs": {"seconds": "{{seconds}}", "batch_size": 1},
        },
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": "{{seed}}",
                "steps": 8,
                "cfg": 1.0,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
                "model": ["78", 0],
                "positive": ["94", 0],
                "negative": ["47", 0],
                "latent_image": ["98", 0],
            },
        },
        "18": {
            "class_type": "VAEDecodeAudio",
            "inputs": {"samples": ["3", 0], "vae": ["97", 2]},
        },
        "106": {
            "class_type": "SaveAudioMP3",
            "inputs": {"audio": ["18", 0], "filename_prefix": "FRIDAY/music", "quality": "V0"},
        },
    }


def qwen_image_edit() -> dict[str, Any]:
    """Change an attached picture by instruction: Qwen-Image-Edit (Rapid AIO).

    The model and its text reader are GGUF files, loaded by city96's
    ComfyUI-GGUF add-on. The picture is scaled to about one megapixel (what
    the model was made for) and the result keeps its shape. The Rapid
    version is tuned for 4 steps at CFG 1.
    """
    return {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": "{{unet}}"}},
        "2": {
            "class_type": "CLIPLoaderGGUF",
            "inputs": {"clip_name": "{{text_encoder}}", "type": "qwen_image"},
        },
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "{{vae}}"}},
        "4": {"class_type": "LoadImage", "inputs": {"image": "{{image}}"}},
        "5": {
            "class_type": "ImageScaleToTotalPixels",
            "inputs": {
                "image": ["4", 0],
                "upscale_method": "lanczos",
                "megapixels": 1.0,
                "resolution_steps": 16,
            },
        },
        "6": {
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": {
                "clip": ["2", 0],
                "prompt": "{{prompt}}",
                "vae": ["3", 0],
                "image1": ["5", 0],
            },
        },
        "7": {
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": {"clip": ["2", 0], "prompt": "", "vae": ["3", 0], "image1": ["5", 0]},
        },
        "8": {"class_type": "VAEEncode", "inputs": {"pixels": ["5", 0], "vae": ["3", 0]}},
        "9": {
            "class_type": "KSampler",
            "inputs": {
                "seed": "{{seed}}",
                "steps": "{{steps}}",
                "cfg": 1.0,
                "sampler_name": "euler_ancestral",
                "scheduler": "beta",
                "denoise": 1.0,
                "model": ["1", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["8", 0],
            },
        },
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["3", 0]}},
        "11": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "FRIDAY-edit", "images": ["10", 0]},
        },
    }
