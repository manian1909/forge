"""Serve your Forge GGUF on a rented cloud GPU (Modal) as an OpenAI-compatible API.

Your weights run on a 16 GB T4 with every layer on the GPU, with a much larger context window than the laptop can
manage. The container starts on the first request and stops after a few idle minutes, so you only use credit
while you are actually generating.

One-time setup (see REMOTE.md):
    pip install modal
    modal setup                                   # opens a browser once to log in
    modal volume create forge-weights
    modal volume put forge-weights models/forge-8b-Q4_K_M.gguf forge.gguf
    modal secret create forge-api-key FORGE_API_KEY=<pick-a-long-random-string>
    modal deploy remote/modal_forge.py            # prints the endpoint URL

Then put that URL in providers.json (see providers.example.json) and run chat.py with --mode auto.
"""

import subprocess

import modal

MODEL_FILE = "/models/forge.gguf"
PORT = 8080
CONTEXT = 32768  # tokens; the laptop is limited to 8192 by VRAM, a T4 has room for far more

app = modal.App("forge")
weights = modal.Volume.from_name("forge-weights", create_if_missing=True)

image = (
    modal.Image.from_registry("ghcr.io/ggml-org/llama.cpp:server-cuda", add_python="3.12")
    .entrypoint([])  # the image's default entrypoint would start its own server
)


@app.function(
    image=image,
    gpu="T4",
    volumes={"/models": weights},
    secrets=[modal.Secret.from_name("forge-api-key")],
    scaledown_window=300,  # seconds idle before the GPU is released (and billing stops)
    timeout=60 * 60,
)
@modal.concurrent(max_inputs=4)
@modal.web_server(port=PORT, startup_timeout=300)
def serve():
    import os

    cmd = [
        "/app/llama-server", "-m", MODEL_FILE,
        "--host", "0.0.0.0", "--port", str(PORT),
        "-ngl", "99",                       # every layer on the GPU
        "-c", str(CONTEXT), "-fa", "on",
        "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
        "--jinja", "--reasoning-budget", "0",   # same flags as scripts/serve.ps1: tool calls on, thinking off
        "--alias", "forge",
        "--api-key", os.environ["FORGE_API_KEY"],
    ]
    subprocess.Popen(cmd)
