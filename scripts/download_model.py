"""Download only the pinned checkpoint files into the project cache."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("HF_HOME", str(ROOT / ".runtime/huggingface"))
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def model_path(download=False):
    from huggingface_hub import snapshot_download
    config = json.loads((ROOT / "runtime.json").read_text())
    directory = Path(snapshot_download(
        config["model_repo"], revision=config["model_revision"],
        allow_patterns=["config.json", "model.safetensors", "tokenizer.json"],
        local_files_only=not download,
    ))
    required = ("config.json", "model.safetensors", "tokenizer.json")
    if not all((directory / name).is_file() for name in required):
        raise FileNotFoundError("Incomplete checkpoint. Run ./scripts/haru download first.")
    return directory


if __name__ == "__main__":
    print(model_path(download=True))
