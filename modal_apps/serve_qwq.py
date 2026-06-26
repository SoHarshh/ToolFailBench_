"""Serve QwQ-32B on Modal (1xH200) as an OpenAI-compatible vLLM endpoint."""

import os

import modal

MINUTES = 60
N_GPU = 1
VLLM_PORT = 8000

HF_ID = "Qwen/QwQ-32B"
# Must equal hf_model_id verbatim (with the "Qwen/" slash) — this is what the
# eval sends as the request "model" for qwq-32b, so a different name would 404.
SERVED_MODEL_NAME = "Qwen/QwQ-32B"

VOL_NAME = os.environ.get("MODAL_VOLUME_NAME", "agentB")
VOL_MOUNT = os.environ.get("MODAL_VOLUME_MOUNT", f"/vol/{VOL_NAME}")
HF_HOME = os.environ.get("MODAL_HF_HOME", f"{VOL_MOUNT}/hf_cache")
HF_SECRET = os.environ.get("MODAL_HF_SECRET", "huggingface-secret")

app = modal.App("tfb-serve-qwq-32b")

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
    gpu=f"H200:{N_GPU}",
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
        # hermes tool extraction needs both of these flags.
        "--enable-auto-tool-choice",
        "--tool-call-parser", "hermes",
        "--max-model-len", "32768",
        "--gpu-memory-utilization", "0.92",
    ]
    print("[tfb-serve-qwq]", " ".join(cmd))
    subprocess.Popen(cmd, env=env)
