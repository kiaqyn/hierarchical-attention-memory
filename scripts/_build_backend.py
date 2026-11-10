"""Zero-dependency PEP 517 / PEP 660 build backend (Python stdlib only).

Enables `pip install -e .` and `pip install .` (and `uvx`) to build wheels and
editable installs even in offline Python 3.12+/3.13+ environments where
`setuptools` is not pre-installed.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import re
import tarfile
import zipfile
from pathlib import Path

NAME = "hierarchical-attention-memory"
DIST_NAME = "hierarchical_attention_memory"
ROOT = Path(__file__).resolve().parent.parent


def _package_version() -> str:
    """scripts/__init__.py is the only version string. pyproject.toml must match it."""
    text = (ROOT / "scripts" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'''(?m)^__version__\s*=\s*["']([^"']+)["']''', text)
    if not match:
        raise RuntimeError("scripts/__init__.py is missing __version__")
    return match.group(1)


VERSION = _package_version()
# The import package stays `scripts`. Entry points are scripts.hfa_*:main.
# Renaming it would break installed consoles, so the old name remains.

METADATA = f"""Metadata-Version: 2.1
Name: {NAME}
Version: {VERSION}
Summary: Local transactional file memory for AI coding agents: L0 mainline focus lock, L1 JSON source of truth, SQLite FTS5 hybrid search, lifecycle hooks, and stdio MCP server.
Home-page: https://github.com/kiaqyn/hierarchical-attention-memory
License: MIT
Requires-Python: >=3.10
Provides-Extra: mcp
Provides-Extra: semantic
Requires-Dist: numpy==2.5.4; python_version >= "3.12" and extra == "semantic"
Requires-Dist: onnxruntime==1.31.1; python_version >= "3.12" and extra == "semantic"
Requires-Dist: tokenizers==0.23.3; python_version >= "3.12" and extra == "semantic"
Requires-Dist: mcp==2.3.0; python_version >= "3.11" and extra == "mcp"
Provides-Extra: tokenizer
Requires-Dist: tiktoken==0.14.0; extra == "tokenizer"
Provides-Extra: all
Requires-Dist: mcp==2.3.0; python_version >= "3.11" and extra == "all"
Requires-Dist: tiktoken==0.14.0; extra == "all"
"""

WHEEL_META = """Wheel-Version: 1.0
Generator: hfa-stdlib-backend
Root-Is-Purelib: true
Tag: py3-none-any
"""

ENTRY_POINTS = """[console_scripts]
hfa = scripts.hfa_manager:main
hfa-mcp = scripts.hfa_mcp:main
hfa-hook = scripts.hfa_hook:main
"""


def get_requires_for_build_wheel(config_settings=None):
    return []


def get_requires_for_build_sdist(config_settings=None):
    return []


def get_requires_for_build_editable(config_settings=None):
    return []


def _record_entry(arcname: str, data: bytes) -> list[str]:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
    return [arcname, f"sha256={digest}", str(len(data))]


def _write_wheel(wheel_path: Path, files: dict[str, bytes]) -> str:
    dist_info = f"{DIST_NAME}-{VERSION}.dist-info"
    readme = (ROOT / "README.md").read_bytes() if (ROOT / "README.md").is_file() else b""
    meta_bytes = (METADATA + ("\n" + readme.decode("utf-8", errors="replace") if readme else "")).encode("utf-8")
    files = dict(files)
    files[f"{dist_info}/METADATA"] = meta_bytes
    files[f"{dist_info}/WHEEL"] = WHEEL_META.encode("utf-8")
    files[f"{dist_info}/entry_points.txt"] = ENTRY_POINTS.encode("utf-8")
    if (ROOT / "LICENSE").is_file():
        files[f"{dist_info}/LICENSE"] = (ROOT / "LICENSE").read_bytes()

    records = []
    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname in sorted(files):
            data = files[arcname]
            zf.writestr(arcname, data)
            records.append(_record_entry(arcname, data))
        record_arcname = f"{dist_info}/RECORD"
        records.append([record_arcname, "", ""])
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerows(records)
        zf.writestr(record_arcname, buf.getvalue().encode("utf-8"))
    return wheel_path.name


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    wheel_dir = Path(wheel_directory)
    wheel_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, bytes] = {}
    for py_file in sorted((ROOT / "scripts").glob("*.py")):
        files[f"scripts/{py_file.name}"] = py_file.read_bytes()
    for template_name in ("memory", "minimal"):
        template_dir = ROOT / "templates" / template_name
        if template_dir.is_dir():
            for tmpl in sorted(template_dir.rglob("*")):
                if tmpl.is_file() and not tmpl.name.startswith(".hfa") and "__pycache__" not in tmpl.parts:
                    rel = tmpl.relative_to(template_dir).as_posix()
                    files[f"scripts/templates/{template_name}/{rel}"] = tmpl.read_bytes()
    wheel_path = wheel_dir / f"{DIST_NAME}-{VERSION}-py3-none-any.whl"
    return _write_wheel(wheel_path, files)


def build_sdist(sdist_directory, config_settings=None):
    sdist_dir = Path(sdist_directory)
    sdist_dir.mkdir(parents=True, exist_ok=True)
    base_prefix = f"{DIST_NAME}-{VERSION}"
    sdist_path = sdist_dir / f"{base_prefix}.tar.gz"
    readme = (ROOT / "README.md").read_bytes() if (ROOT / "README.md").is_file() else b""
    pkg_info = (METADATA + ("\n" + readme.decode("utf-8", errors="replace") if readme else "")).encode("utf-8")
    with tarfile.open(sdist_path, "w:gz") as tf:
        info = tarfile.TarInfo(f"{base_prefix}/PKG-INFO")
        info.size = len(pkg_info)
        tf.addfile(info, io.BytesIO(pkg_info))
        for rel_glob in ("pyproject.toml", "README.md", "LICENSE", "SKILL.md", "CHANGELOG.md", "hooks.json", "plugin.json",
                         "scripts/*.py", "templates/memory/**/*", "templates/minimal/**/*",
                         "benchmarks/scale-recall/*.py", "docs/*.md", "tests/*.py",
                         "experiments/quota/*.py", "experiments/quota/fixtures/*.json"):
            for path in sorted(ROOT.glob(rel_glob)):
                if path.is_file() and not path.name.startswith(".hfa") and "__pycache__" not in path.parts:
                    rel = path.relative_to(ROOT).as_posix()
                    tf.add(path, arcname=f"{base_prefix}/{rel}")
    return sdist_path.name


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    wheel_dir = Path(wheel_directory)
    wheel_dir.mkdir(parents=True, exist_ok=True)
    pth_content = (str(ROOT.resolve()) + "\n").encode("utf-8")
    files = {f"{DIST_NAME}.pth": pth_content}
    wheel_path = wheel_dir / f"{DIST_NAME}-{VERSION}-0.editable-py3-none-any.whl"
    return _write_wheel(wheel_path, files)
