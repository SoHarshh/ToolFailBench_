"""Launch vLLM for a registry model with the tool-call parser + chat template from its YAML."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from models.registry import get_model_config, KNOWN_VLLM_PARSERS


def _resolve_chat_template(rel: str) -> str:
    """Locate the chat-template file: $VLLM_REPO, then the installed vllm package, then a local path."""
    if not rel:
        return ""
    vllm_repo = os.getenv("VLLM_REPO")
    if vllm_repo and Path(vllm_repo, rel).is_file():
        return str(Path(vllm_repo, rel))
    try:
        import vllm
        cand = Path(vllm.__file__).parent.parent / rel
        if cand.is_file():
            return str(cand)
    except ImportError:
        pass
    if Path(rel).is_file():
        return rel
    print(f"WARNING: chat_template {rel!r} not found. Set VLLM_REPO=/path/to/vllm "
          f"or pass --chat-template explicitly.", file=sys.stderr)
    return ""


def main():
    argv = sys.argv[1:]
    if not argv:
        sys.exit("Usage: python scripts/serve_model.py <model-id> [-- <extra vllm flags>]")
    model_id, extra = argv[0], argv[1:]
    if extra and extra[0] == "--":
        extra = extra[1:]

    try:
        m = get_model_config(model_id)
    except Exception as e:
        sys.exit(f"Unknown model id: {model_id} ({e})")
    if m["inference_backend"] != "vllm":
        sys.exit(f"Model {model_id} uses inference_backend={m['inference_backend']!r}; vLLM serve is not applicable.")

    parser = os.getenv("TOOL_CALL_PARSER") or m.get("tool_call_parser", "")
    if parser and parser not in KNOWN_VLLM_PARSERS:
        print(f"WARNING: tool_call_parser={parser!r} is not in stock vLLM; "
              f"vLLM will fail to start unless a plugin registers it.", file=sys.stderr)

    cmd = ["vllm", "serve", m["hf_model_id"], "--port", os.getenv("VLLM_PORT", "8000")]
    if m.get("trust_remote_code"):
        cmd.append("--trust-remote-code")
    if parser:
        cmd += ["--enable-auto-tool-choice", "--tool-call-parser", parser]
    chat_template = _resolve_chat_template(m.get("chat_template", ""))
    if chat_template:
        cmd += ["--chat-template", chat_template]
    cmd += extra

    print(f"# vLLM serve command for {model_id}:\n  {' '.join(cmd)}", file=sys.stderr)
    if os.getenv("DRY_RUN") == "1":
        print(f"# DRY_RUN=1 — not executing. Next: python scripts/preflight.py {model_id}", file=sys.stderr)
        return
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
