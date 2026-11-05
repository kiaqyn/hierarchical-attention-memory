"""Explicit public weight download; evaluation itself never needs a network."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.request

MODEL = "intfloat/multilingual-e5-small"
FILES = {"onnx/model_qint8_avx512_vnni.onnx": "model.onnx",
         "tokenizer.json": "tokenizer.json", "config.json": "config.json",
         "tokenizer_config.json": "tokenizer_config.json", "README.md": "MODEL_CARD.md"}


def download(target):
    target.mkdir(parents=True, exist_ok=True)
    # Reproduce the tested revision instead of silently following future main.
    manifest = json.loads(Path(__file__).with_name("model-spec.json").read_text(encoding="utf8"))
    revision = manifest["revision"]
    for remote, local in FILES.items():
        info = next(item for item in manifest["files"] if item["remote"]==remote and item["local"]==local)
        expected = info["sha256"]
        path = target/local
        started = time.monotonic()
        if not path.exists():
            url = f"https://huggingface.co/{MODEL}/resolve/{revision}/{remote}"
            partial = path.with_suffix(path.suffix+".part")
            print(f"Downloading {remote}: {info['bytes']} bytes", flush=True)
            with urllib.request.urlopen(url, timeout=240) as response, partial.open("wb") as output:
                while block := response.read(1024*1024):
                    output.write(block)
            digest = hashlib.sha256(partial.read_bytes()).hexdigest()
            if expected and expected != digest:
                raise ValueError(f"SHA256 mismatch for {remote}; preserve partial for inspection")
            os.replace(partial, path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected and expected != digest:
            raise ValueError(f"Existing file checksum mismatch: {local}")
        print(f"Verified {local}: {path.stat().st_size} bytes, {time.monotonic()-started:.1f}s", flush=True)
    (target/"manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    download(parser.parse_args().directory)
