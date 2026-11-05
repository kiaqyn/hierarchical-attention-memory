"""Explicit download of two public, checksum-pinned E5 files. Never auto-run."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.request

try:
    from .hfa_semantic import MODEL, REVISION, PINNED, model_identity
except ImportError:
    from hfa_semantic import MODEL, REVISION, PINNED, model_identity

REMOTE={'model.onnx':'onnx/model_qint8_avx512_vnni.onnx','tokenizer.json':'tokenizer.json'}


def download(directory):
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    for name,remote in REMOTE.items():
        target=directory/name
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest()!=PINNED[name]:
                raise ValueError('Existing model file differs; choose a new empty directory: '+name)
            continue
        fd,tempname=tempfile.mkstemp(prefix='.hfa-model-',suffix='.part',dir=str(directory))
        temporary=Path(tempname)
        try:
            hasher=hashlib.sha256()
            with os.fdopen(fd,'wb') as stream:
                with urllib.request.urlopen(f'https://huggingface.co/{MODEL}/resolve/{REVISION}/{remote}',timeout=120) as response:
                    while block:=response.read(1024*1024):
                        stream.write(block)
                        hasher.update(block)
                stream.flush()
                os.fsync(stream.fileno())
            if hasher.hexdigest()!=PINNED[name]:raise ValueError('Downloaded model checksum mismatch: '+name)
            if target.exists():raise ValueError('Concurrent model download; retry after the other download finishes')
            os.replace(temporary,target)
        finally:
            temporary.unlink(missing_ok=True)
    payload={'model':MODEL,'revision':REVISION,'files':[{'local':name,'sha256':value} for name,value in PINNED.items()]}
    fd,tempname=tempfile.mkstemp(prefix='.hfa-model-manifest-',dir=str(directory))
    with os.fdopen(fd,'w',encoding='utf8') as stream:json.dump(payload,stream,indent=2)
    os.replace(tempname,directory/'manifest.json')
    return model_identity(directory)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--verify',action='store_true',help='Verify existing files without writing/downloading')
    args=parser.parse_args()
    try:
        identity=model_identity(args.output) if args.verify else download(args.output)
    except (ValueError,OSError) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False))
        return 1
    print(json.dumps({'verified':True,**identity},ensure_ascii=False))
    return 0


if __name__=='__main__':raise SystemExit(main())
