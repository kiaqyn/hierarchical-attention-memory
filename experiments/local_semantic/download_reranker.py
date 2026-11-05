"""Explicit public, pinned ONNX cross-encoder download; no inference API."""
import hashlib
import json
from pathlib import Path
import os
import urllib.request

MODEL = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
REVISION = "1427fd652930e4ba29e8149678df786c240d8825"
FILES = {"onnx/model_quint8_avx2.onnx": ("model.onnx", "6c2513767fb63d008a4377bef7a7a3555433d9436342bb53e35a3a72ffc52d4b"),
         "tokenizer.json": ("tokenizer.json", "62c24cdc13d4c9952d63718d6c9fa4c287974249e16b7ade6d5a85e7bbb75626"),
         "config.json": ("config.json", "cc2cfe51aa3fd759d21d21acf5dfd6994aa67a3c9210636d22e143699d336c77"), "README.md": ("MODEL_CARD.md", "474736a65d6393a060119a8dc304563af67af4d8d86ccfee4a05dd0df107fc11")}


def download(directory):
    directory.mkdir(parents=True,exist_ok=True)
    manifest = {"model":MODEL,"revision":REVISION,"variant":"official quantized ONNX AVX2","files":[]}
    for remote,(local,expected) in FILES.items():
        path = directory/local
        if not path.exists():
            print("Downloading "+remote,flush=True)
            partial = directory/(local+".part")
            with urllib.request.urlopen(f"https://huggingface.co/{MODEL}/resolve/{REVISION}/{remote}",timeout=240) as response, partial.open("wb") as out:
                while block := response.read(1024*1024):
                    out.write(block)
            digest = hashlib.sha256(partial.read_bytes()).hexdigest()
            if expected and digest!=expected:
                raise ValueError("Public model checksum mismatch: "+remote)
            os.replace(partial,path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected and digest!=expected:
            raise ValueError("Existing checksum mismatch: "+remote)
        manifest["files"].append({"remote":remote,"local":local,"sha256":digest,"bytes":path.stat().st_size})
        print("Verified "+local,flush=True)
    (directory/"manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf8")


if __name__ == "__main__":
    import sys
    download(Path(sys.argv[1]))
