"""Serve the GLM-4.7-FP8 judge on Modal (4xB200) as an OpenAI-compatible vLLM endpoint."""

import os

import modal

MINUTES = 60
N_GPU = 4
VLLM_PORT = 8000

HF_ID = "zai-org/GLM-4.7-FP8"
SERVED_MODEL_NAME = "glm-4.7-fp8"

VOL_NAME = os.environ.get("MODAL_VOLUME_NAME", "agentB")
VOL_MOUNT = os.environ.get("MODAL_VOLUME_MOUNT", f"/vol/{VOL_NAME}")
HF_HOME = os.environ.get("MODAL_HF_HOME", f"{VOL_MOUNT}/hf_cache")
HF_SECRET = os.environ.get("MODAL_HF_SECRET", "huggingface-secret")

app = modal.App("tfb-judge-glm47-fp8")

image = (
    modal.Image.from_registry("vllm/vllm-openai:cu130-nightly", add_python="3.11")
    # The vllm/vllm-openai image sets ENTRYPOINT=["vllm"], which collides with
    # Modal's container entrypoint. Reset to default so Python runs normally.
    .entrypoint([])
)

volume = modal.Volume.from_name(VOL_NAME, create_if_missing=False)
secret = modal.Secret.from_name(HF_SECRET)


@app.function(
    image=image,
    gpu=f"B200:{N_GPU}",
    volumes={VOL_MOUNT: volume},
    secrets=[secret],
    timeout=8 * 60 * MINUTES,
    scaledown_window=5 * MINUTES,
    max_containers=1,
)
@modal.concurrent(max_inputs=200)
@modal.web_server(port=VLLM_PORT, startup_timeout=30 * MINUTES)
def serve():
    import subprocess

    env = {
        **os.environ,
        "HF_HOME": HF_HOME,
        "HUGGINGFACE_HUB_CACHE": HF_HOME,
        "VLLM_CACHE_ROOT": f"{VOL_MOUNT}/vllm_cache",
    }
    os.makedirs(HF_HOME, exist_ok=True)
    os.makedirs(env["VLLM_CACHE_ROOT"], exist_ok=True)

    cmd = [
        "vllm", "serve", HF_ID,
        "--served-model-name", SERVED_MODEL_NAME,
        "--host", "0.0.0.0",
        "--port", str(VLLM_PORT),
        "--tensor-parallel-size", str(N_GPU),
        "--tool-call-parser", "glm47",
        "--reasoning-parser", "glm45",
        "--enable-auto-tool-choice",
        "--max-model-len", "32768",
        "--gpu-memory-utilization", "0.92",
    ]
    print("[tfb-judge-glm47]", " ".join(cmd))
    subprocess.Popen(cmd, env=env)
