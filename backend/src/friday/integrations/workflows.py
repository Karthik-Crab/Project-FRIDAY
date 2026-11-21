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
