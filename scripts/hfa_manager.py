#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HFA 2.2: local transactional file memory. No model API is called."""
from __future__ import annotations
import argparse
import base64
import contextlib
import datetime as dt
import errno
import functools
import hashlib
import json
import math
import unicodedata
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_MEMORY_DIR = (
    ROOT_DIR / "templates/memory"
    if (ROOT_DIR / "templates/memory").is_dir()
    else Path(__file__).resolve().parent / "templates/memory"
)
# One re-entrant thread lock per memory root: different memory directories in the
# same process never serialize each other; the file lock handles other processes.
_ROOT_LOCKS: dict[str, threading.RLock] = {}
_ROOT_LOCKS_GUARD = threading.Lock()
JOURNAL = ".hfa-transaction.json"
DEFAULT_CONFIG = {"version": "2.2", "active_mainline": "M1", "router_token_budget": 600,
                  "recall_token_budget": 600, "max_topics_per_epoch": 50, "max_recall_topics": 2}


def resolve_default_memory_dir(explicit: Path | None = None, cwd: Path | None = None) -> Path:
    """Resolve an explicit memory path, HFA_MEMORY_DIR, or workspace default ./memory."""
    if explicit is not None:
        return Path(explicit)
    env_dir = os.environ.get("HFA_MEMORY_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    base = (cwd or Path.cwd()).resolve()
    for rel in ("memory", ".agents/memory", ".hfa/memory"):
        candidate = base / rel
        if (candidate / "directory.md").is_file():
            return candidate
    return base / "memory"

class MemoryError(ValueError):
    """Invalid input, inconsistent state, or failed recovery."""

def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"

def _read_utf8(path):
    with open(path, encoding="utf-8") as stream:
        return stream.read()

def _safe_path(root, relative):
    # Path objects use the native separator on Windows; normalize them. Raw strings
    # (e.g. from L0 links) still reject backslashes as suspicious.
    relative = relative.as_posix() if isinstance(relative, os.PathLike) else str(relative)
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts or not relative or "\\" in relative:
        raise MemoryError(f"Unsafe memory path: {relative!r}")
    root_resolved = root.resolve()
    target = (root_resolved / rel).resolve()
    if not target.is_relative_to(root_resolved) or target == root_resolved:
        raise MemoryError(f"Memory path escapes root: {relative!r}")
    return target

def _atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".hfa-stage-", dir=str(path.parent))
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)

def _restore(root, entries):
    for entry in reversed(entries):
        target = _safe_path(root, entry["path"])
        if entry["before"] is None:
            if target.exists():
                if not target.is_file():
                    raise MemoryError(f"Recovery blocked by non-file: {entry['path']}")
                target.unlink()
        else:
            _atomic_write(target, base64.b64decode(entry["before"], validate=True))

def _recover(root):
    journal = root / JOURNAL
    if not journal.exists():
        return False
    try:
        state = json.loads(_read_utf8(journal))
        if state.get("version") != 1 or state.get("state") not in ("prepared", "committed"):
            raise MemoryError("Invalid transaction journal; preserve for recovery")
        if state["state"] == "prepared":
            _restore(root, state["entries"])
        journal.unlink()
    except Exception as exc:
        raise MemoryError(f"Transaction recovery failed; reads remain blocked: {exc}") from exc
    return True

@contextlib.contextmanager
def _hfa_lock(root, timeout=10):
    if not root.is_dir():
        raise MemoryError(f"Memory directory does not exist: {root}")
    # Tokenizer imports/vocabulary loading can be slow on a cold CLI process.
    # Do that before acquiring any lock so other readers/writers can keep progressing.
    _encoder()
    with _root_lock(root):
        fd = os.open(str(root / ".hfa.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        acquired = False
        try:
            # Never write to the lock file before owning it: on Windows, writing a
            # byte range locked by another process raises PermissionError. Locking a
            # range beyond EOF is legal (LockFile), so an empty file needs no priming.
            deadline = time.monotonic() + timeout
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if time.monotonic() >= deadline:
                        raise MemoryError("Memory lock timed out") from exc
                    time.sleep(0.01)
            _recover(root)
            yield
        finally:
            try:
                if acquired:
                    os.lseek(fd, 0, os.SEEK_SET)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass  # Closing the descriptor below releases the lock regardless.
            finally:
                os.close(fd)

def _root_lock(root):
    key = os.path.normcase(str(Path(root).resolve()))
    with _ROOT_LOCKS_GUARD:
        return _ROOT_LOCKS.setdefault(key, threading.RLock())

def _to_bytes(content: str | bytes) -> bytes:
    return content if isinstance(content, bytes) else content.encode("utf-8")

def _file_matches(path: Path, expected: str | bytes) -> bool:
    if not path.is_file():
        return False
    if isinstance(expected, bytes):
        actual = path.read_bytes()
        if actual == expected:
            return True
        if len(expected) >= 100 and len(actual) == len(expected) and expected.startswith(b"SQLite format 3\x00"):
            return actual[:92] == expected[:92] and actual[100:] == expected[100:]
        return False
    return _read_utf8(path) == expected

def _commit(root, updates):
    """Must hold _hfa_lock. CLI readers see committed or recovered state.

    Raw filesystem readers do not participate in multi-file transactions.
    The journal supports process-crash recovery, not a power-loss guarantee.
    """
    entries = []
    for relative, content in updates.items():
        target = _safe_path(root, relative)
        if target.exists() and not target.is_file():
            raise MemoryError(f"Write target is not a file: {relative}")
        old = target.read_bytes() if target.exists() else None
        if old == _to_bytes(content):
            continue
        entries.append({"path": relative, "before": base64.b64encode(old).decode("ascii") if old is not None else None})
    if not entries:
        return
    state = {"version": 1, "state": "prepared", "entries": entries}
    journal = root / JOURNAL
    _atomic_write(journal, _json(state).encode("utf-8"))
    try:
        for entry in entries:
            _atomic_write(_safe_path(root, entry["path"]), _to_bytes(updates[entry["path"]]))
        state["state"] = "committed"
        _atomic_write(journal, _json(state).encode("utf-8"))
    except BaseException:
        # A failed rollback leaves a prepared journal, blocking subsequent readers.
        _restore(root, entries)
        journal.unlink()
        raise
    try:
        journal.unlink()
    except OSError:
        pass  # A completed commit must not be reported as a failed operation.

@functools.lru_cache(maxsize=1)
def _encoder():
    try:
        import tiktoken
        return tiktoken.get_encoding("o200k_base")
    except Exception:
        return None

_CJK_RANGES = ("\u2e80-\u2fff\u3000-\u303f\u3040-\u30ff\u3100-\u31ff\u3400-\u4dbf\u4e00-\u9fff"
               "\uac00-\ud7af\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef")
_HEURISTIC_PIECES = re.compile(rf"[A-Za-z]+|[0-9]+|[{_CJK_RANGES}]|[ \t]+|[\r\n]+|.", re.S)

@functools.lru_cache(maxsize=4096)
def _heuristic_tokens(text):
    """Conservative o200k-style estimate used only when tiktoken is unavailable.

    Calibrated to over-count typical Markdown/CJK/English text (tests compare it with
    o200k_base): letters ceil(n/4), digits ceil(n/3) (o200k splits 1-3 digit groups),
    one per CJK character or ASCII symbol, one per newline run or multi-space run,
    and UTF-8 bytes for any other character (a strict byte-level BPE bound).
    """
    count = 0
    pieces = _HEURISTIC_PIECES.findall(text)
    for index, piece in enumerate(pieces):
        first = piece[0]
        if first.isascii():
            if first.isalpha():
                count += -(-len(piece) // 4)
            elif first.isdigit():
                count += -(-len(piece) // 3)
            elif first in " \t":
                following = pieces[index + 1][0] if index + 1 < len(pieces) else ""
                # A single space usually merges into a following word or symbol,
                # but not into digits, CJK or other multi-byte characters.
                merges = len(piece) == 1 and following.isascii() and not following.isdigit() and following not in "\r\n"
                count += 0 if merges else 1
            else:
                count += 1
        elif re.match(rf"[{_CJK_RANGES}]", first):
            count += 1
        else:
            count += len(first.encode("utf-8"))
    return count

def measure_tokens(text):
    encoder = _encoder()
    if encoder is not None:
        return {"count": len(encoder.encode(text, disallowed_special=())), "method": "o200k_base", "exact": True}
    return {"count": _heuristic_tokens(text), "method": "heuristic_estimate", "exact": False}

def estimate_routing_tokens(text):
    return measure_tokens(text)["count"]

def extract_hit_words(line):
    match = re.search(r"\[命中词[:：]\s*([^\]]+)\]", line)
    return [w.strip() for w in re.split(r"[,，、/]+", match.group(1)) if w.strip()] if match else []

@functools.lru_cache(maxsize=4096)
def _normalize_for_match(text):
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fffA-Za-z0-9])", "", text)
    return re.sub(r"(?<=[A-Za-z0-9])\s+(?=[\u4e00-\u9fff])", "", text)

@functools.lru_cache(maxsize=4096)
def _keyword_matches(word, text):
    word, text = _normalize_for_match(word), _normalize_for_match(text)
    if not word:
        return False
    if re.fullmatch(r"[a-z0-9_.-]+", word):
        return bool(re.search(rf"(?<![a-z0-9_]){re.escape(word)}(?![a-z0-9_])", text))
    if re.match(r"^[a-z0-9]", word):
        return bool(re.search(rf"(?<![a-z0-9_]){re.escape(word)}", text))
    return word in text

_RETRIEVAL_VERBS_EN = (r"(?:retriev|search|look|check|quer|recall|includ|mention|fetch|consult|bring\s+up|pull\s+up"
                       r"|consider|dig)\w*")
_RETRIEVAL_VERBS_ZH = r"(?:查|看|找|搜|检索|提|翻|读|调取|调用|回忆|召回|参考|管|碰|涉及|用|再)"
# Negation means "do not retrieve X", not merely "X contains a negative word":
# English needs word boundaries plus a retrieval verb, so "whenever", "unavoidable",
# "deploy without downtime", "how to avoid deadlocks" and bug reports such as
# "it never loads" remain positive content. Single-character 别/勿 only count
# before a retrieval verb and never inside compounds such as 级别/区别/类别/识别.
_NEGATION = re.compile(
    rf"\b(?:do\s+not|don'?t|no\s+need\s+to)\s+(?:\w+\s+)?{_RETRIEVAL_VERBS_EN}\b"
    rf"|^\s*(?:please\s+)?never\s+(?:\w+\s+)?{_RETRIEVAL_VERBS_EN}\b"
    r"|\b(?:exclude|excluding|skip|skipping|ignore|ignoring)\s+(?:the\s+)?(?:history|memory|memories|records?|notes?)\b"
    r"|不要|不需要|无需|不用|不必|切勿|请勿|不查|不看"
    rf"|(?<![级区类特分识性派差告离辨鉴个格判])别{_RETRIEVAL_VERBS_ZH}"
    rf"|勿{_RETRIEVAL_VERBS_ZH}|禁止(?:查|看|找|搜|检索|调取|召回|读取)",
    re.I)

def _positive_query(query):
    clauses = re.split(r"[，。；;,\n!?！？]|但是|不过|而是|但|\b(?:but|however|instead)\b", query, flags=re.I)
    return "，".join(clause for clause in clauses if clause and not _NEGATION.search(clause))


# Small, auditable vocabulary; this is lexical expansion, not an embedding model.
DEFAULT_ALIASES = [
    ["乱码", "中文变问号", "文字变成问号", "看不懂的符号", "mojibake", "garbled text"],
    ["部署", "上线", "deployment", "deploy"],
    ["备份", "backup"], ["并发", "同时写入", "concurrency"],
    ["超时", "等太久", "timeout", "timed out"],
    ["回滚", "撤销更改", "撤回", "rollback"],
]
STOP_TERMS = set("the a an to of is are in on for with how what was were please find recall previous this that it and or my we do not should about 的是 之前 怎么 如何 这个 那个 我们 记得 请问 一下 什么 现在 继续 方案 问题 处理 需要 已经 可以 当时 后来 是否 结果".split())
# Common English words: indexed and matchable, but never sufficient evidence alone.
GENERIC_TERMS = set("""use uses used using get gets got set run runs fix make makes new old see check checks show tell
want need needs does did done has have had can could would will shall may might must why when where which who whom
whose our you your me us they them their its be been being am no not yes any some all more most other into from by at
as if then than so such only also just very too here there out up down over under again once same own both each few
nor off now let lets like via per vs etc ok help thanks thank know try trying work works working way ways thing things
something anything one two give go going doing made said say says before after earlier previously last
decide decided decision remember""".split())
_ENGLISH_TERM = re.compile(r"[a-z][a-z0-9_.-]+")

@functools.lru_cache(maxsize=4096)
def _search_terms_cached(text, custom_aliases):
    text = _normalize_for_match(text)
    terms = set(re.findall(r"[a-z0-9][a-z0-9_.-]*", text))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        terms.update(run[i:i+2] for i in range(len(run)-1))
    terms = {t for t in terms if t not in STOP_TERMS and len(t) >= 2}
    for group in DEFAULT_ALIASES + [list(g) for g in custom_aliases]:
        if any(_keyword_matches(word, text) for word in group):
            terms.add("alias:" + _normalize_for_match(group[0]))
    return frozenset(terms)

def _search_terms(text, config):
    custom = tuple(tuple(g) for g in config.get("retrieval_aliases", []))
    return set(_search_terms_cached(text, custom))

@functools.lru_cache(maxsize=1)
def _fts5_supported() -> bool:
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE _t USING fts5(x)")
        finally:
            conn.close()
        return True
    except Exception:
        return False

@functools.lru_cache(maxsize=16)
def _build_fts_cached(digest: str, docs_json: str) -> bytes:
    documents = json.loads(docs_json)
    df_counts: dict[str, int] = {}
    for doc in documents.values():
        for term in set(doc["terms"]):
            df_counts[term] = df_counts.get(term, 0) + 1

    def _populate(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA page_size = 4096")
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('version', '1'), ('source_digest', ?)", (digest,))
        conn.execute("CREATE TABLE term_df (term TEXT PRIMARY KEY, df INTEGER)")
        for term in sorted(df_counts):
            conn.execute("INSERT INTO term_df VALUES (?, ?)", (term, df_counts[term]))
        if _fts5_supported():
            conn.execute(
                "CREATE VIRTUAL TABLE chunks_fts USING fts5("
                "chunk_id UNINDEXED, heading, body, "
                "tokenize=\"unicode61 tokenchars ':_.-'\")"
            )
            for chunk_id in sorted(documents):
                doc = documents[chunk_id]
                conn.execute(
                    "INSERT INTO chunks_fts VALUES (?, ?, ?)",
                    (chunk_id, " ".join(doc["heading"]), " ".join(doc["terms"])),
                )
        conn.commit()

    conn = sqlite3.connect(":memory:")
    try:
        _populate(conn)
        if hasattr(conn, "serialize"):
            return conn.serialize()
    finally:
        conn.close()
    with tempfile.NamedTemporaryFile(prefix=".hfa-fts-", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        fconn = sqlite3.connect(str(tmp_path))
        try:
            _populate(fconn)
        finally:
            fconn.close()
        return tmp_path.read_bytes()
    finally:
        tmp_path.unlink(missing_ok=True)

_FTS_THREAD_LOCAL = threading.local()

def _fts_search(terms: set[str], fts_bytes: bytes | None) -> tuple[dict[str, float] | None, dict[str, int] | None]:
    if not fts_bytes or not terms or not _fts5_supported():
        return None, None
    tmp_path: Path | None = None
    temp_conn: sqlite3.Connection | None = None
    try:
        cache = getattr(_FTS_THREAD_LOCAL, "conn_cache", None)
        if cache is None or cache[0] != fts_bytes:
            if cache is not None:
                try:
                    cache[1].close()
                except Exception:
                    pass
            conn = sqlite3.connect(":memory:")
            if hasattr(conn, "deserialize"):
                conn.deserialize(fts_bytes)
                _FTS_THREAD_LOCAL.conn_cache = (fts_bytes, conn)
            else:
                conn.close()
                with tempfile.NamedTemporaryFile(prefix=".hfa-fts-q-", delete=False) as tmp:
                    tmp.write(fts_bytes)
                    tmp_path = Path(tmp.name)
                temp_conn = sqlite3.connect(str(tmp_path))
                conn = temp_conn
        else:
            conn = cache[1]
        term_list = sorted(terms)
        placeholders = ",".join("?" for _ in term_list)
        df_rows = conn.execute(
            f"SELECT term, df FROM term_df WHERE term IN ({placeholders})",
            term_list,
        ).fetchall()
        df_map = {t: 0 for t in terms}
        present_terms: list[str] = []
        for term, df in df_rows:
            df_int = int(df)
            df_map[str(term)] = df_int
            if df_int > 0:
                present_terms.append(str(term))
        if not present_terms:
            return {}, df_map
        match_expr = " OR ".join('"' + t.replace('"', '""') + '"' for t in present_terms)
        rows = conn.execute(
            "SELECT chunk_id, -bm25(chunks_fts, 0.0, 3.0, 1.0) "
            "FROM chunks_fts WHERE chunks_fts MATCH ?",
            (match_expr,),
        ).fetchall()
        return {str(cid): float(bm25_val) for cid, bm25_val in rows}, df_map
    except Exception:
        return None, None
    finally:
        if temp_conn is not None:
            try:
                temp_conn.close()
            except Exception:
                pass
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

def _rank_content(query, documents, config, fts_bytes=None):
    # Filler words may still match documents, but never count as evidence on their own,
    # never dilute coverage and never count as "unexplained" query concepts.
    terms = {t for t in _search_terms(query, config) if t not in GENERIC_TERMS}
    if not terms:
        return []
    groups = DEFAULT_ALIASES + config.get("retrieval_aliases", [])
    alias_terms = {}
    for group in groups:
        words = _search_terms(" ".join(group), {"retrieval_aliases": []})
        alias_terms.setdefault("alias:" + _normalize_for_match(group[0]), set()).update(
            t for t in words if not t.startswith("alias:"))
    fts_candidates, fts_df = _fts_search(terms, fts_bytes)
    if fts_df is not None:
        frequency = fts_df
        candidate_items = [(cid, documents[cid]) for cid in fts_candidates if cid in documents]
    else:
        frequency = {t: sum(t in d["terms"] for d in documents.values()) for t in terms}
        candidate_items = list(documents.items())
    weights = {t: 1 + math.log((len(documents) + 1) / (frequency[t] + 1)) for t in terms}
    total = sum(weights.values())
    ranked = []
    for chunk_id, doc in candidate_items:
        shared = terms.intersection(doc["terms"])
        if not shared:
            continue
        aliases = {t for t in shared if t.startswith("alias:")}
        # Query words already explained by a matched synonym group are one concept.
        explained = set().union(*(alias_terms.get(a, set()) for a in aliases))
        english = {t for t in shared - aliases - explained if _ENGLISH_TERM.fullmatch(t)}
        cjk = shared - aliases - explained - english
        # Overlapping Chinese bigrams describe roughly one concept per two bigrams.
        concepts = len(aliases) + len(english) + math.ceil(len(cjk) / 2)
        strong = bool(aliases or english)  # includes 2–3 letter terms: API, CLI, Git, SQL
        if concepts == 0 or (len(shared) < 2 and not strong):
            continue  # One generic Chinese bigram is insufficient evidence.
        coverage = sum(weights[t] for t in (shared | (terms & explained))) / total
        if coverage < (0.18 if concepts < 2 else 0.1):
            continue
        # A single matched concept amid several unrelated English concepts is weak
        # evidence (e.g. "backup singer audition" vs. a database backup). Two or more
        # independent concepts ("redis" + "cache") are enough regardless of modifiers.
        unknown = [t for t in terms - shared - explained if _ENGLISH_TERM.fullmatch(t)]
        if concepts == 1 and len(unknown) >= 2:
            continue
        score = sum(weights[t] * (3 if t in doc["heading"] else 1) for t in shared) / total
        ranked.append((chunk_id, score, sorted(shared)))
    return ranked

def _profile_drift(query):
    """Flag likely changes to how the assistant should collaborate, not technical settings.

    Generic words such as 回复/自动执行/取消 also describe software ("数据库回复超时",
    "取消自动执行的定时任务"), so anchors must address the user-assistant interaction.
    """
    anchors = (r"偏好|沟通方式|沟通风格|语气|称呼我|叫我|先问我|问我一下|跟我确认|和我确认|手动确认|大白话"
               r"|(?:你的|你)(?:回复|回答)|(?:回复|回答)(?:我|风格|方式|语言|格式|长度)|用(?:中文|英文)(?:回复|回答)|协作方式"
               r"|全自主|(?:你|助手|AI|模型)(?:不要|别|不再)?(?:再)?(?:自动|自主)执行|(?:自动|自主)执行(?:操作|命令|修改)")
    changes = r"以后|今后|往后|从现在开始|不要|不再|别|停止|取消|改成|改为|换成|先问我|手动确认"
    zh = bool(re.search(anchors, query, re.I) and re.search(changes, query))
    en = bool(re.search(r"\b(?:respond|reply|replies|answer|answers|tone|call\s+me|ask\s+me|confirm\s+with\s+me"
                        r"|verbose|concise|autonomous(?:ly)?)\b", query, re.I)
              and re.search(r"\b(?:from\s+now\s+on|going\s+forward|always|never|stop|don'?t|do\s+not|no\s+longer"
                            r"|instead)\b", query, re.I)
              and re.search(r"\b(?:you|your|me|my)\b", query, re.I))
    return zh or en

def _config(root):
    config = DEFAULT_CONFIG.copy()
    path = root / "config.json"
    if path.exists():
        stored = json.loads(_read_utf8(path))
        if not isinstance(stored, dict):
            raise MemoryError("config.json must contain a JSON object")
        config.update(stored)
    elif (root / "index/projects.json").is_file():
        old = json.loads(_read_utf8(root / "index/projects.json"))
        if not isinstance(old, dict):
            raise MemoryError("Legacy projects.json must contain a JSON object")
        # projects.json "version" is the index schema (integer 1), not the config
        # schema; importing it would permanently downgrade config.json on reindex.
        config.update({k: v for k, v in old.items() if k in DEFAULT_CONFIG and k != "version"})
    if not isinstance(config.get("version"), str) or not re.fullmatch(r"\d+(?:\.\d+)*", config["version"]):
        # Heal configs already corrupted to an integer by older builds.
        config["version"] = DEFAULT_CONFIG["version"]
    if not isinstance(config["active_mainline"], str) or not re.fullmatch(r"M\d+", config["active_mainline"]):
        raise MemoryError("active_mainline in config must look like M<n>")
    for key in ("router_token_budget", "recall_token_budget", "max_topics_per_epoch", "max_recall_topics"):
        if type(config[key]) is not int or config[key] < 1:
            raise MemoryError(f"Invalid positive integer in config: {key}")
    aliases = config.get("retrieval_aliases", [])
    if (not isinstance(aliases, list) or len(aliases) > 64
            or any(not isinstance(group, list) or not 2 <= len(group) <= 8
                   or any(not isinstance(w, str) or not w.strip() or len(w) > 64
                          or "\n" in w or "\r" in w for w in group) for group in aliases)):
        raise MemoryError("retrieval_aliases must contain up to 64 groups of 2–8 short phrases")
    return config

def parse_directory_nodes(directory_md, text_override=None):
    text, root = (_read_utf8(directory_md) if text_override is None else text_override), directory_md.parent
    nodes = {"MAX": [], "HIGH": [], "MEDIUM": [], "TOMBSTONED": [], "HISTORY": []}
    missing, seen = [], set()
    for line in text.splitlines():
        if not line.strip().startswith("* **"):
            continue
        match = re.search(r"\[`[^`]+`\]\(([^)]+)\)", line)
        label = re.match(r"^\*\s+\*\*(.*?)\*\*", line.strip())
        if not match or not label:
            raise MemoryError(f"Malformed L0 entry: {line}")
        relative, node_id = match.group(1).removeprefix("./"), label.group(1).strip("[]")
        if node_id in seen:
            raise MemoryError(f"Duplicate L0 node: {node_id}")
        seen.add(node_id)
        exists = _safe_path(root, relative).is_file()
        if not exists:
            missing.append(relative)
        status_match = re.search(r"➔\s*`([^`]+)`", line)
        status = status_match.group(1) if status_match else "STABLE"
        entry = {"id": node_id, "path": relative, "link": match.group(0), "raw": line,
                 "hit_words": extract_hit_words(line), "exists": exists, "status": status}
        # Only profile/ files are resident HIGH nodes. Any other unrecognized node
        # (DEV, TOOL, branches/...) is a side branch: it needs --unlock to be read
        # and can be tombstoned. Defaulting unknown nodes to HIGH would silently
        # promote them to always-permitted user profile material.
        tier = ("TOMBSTONED" if status == "TOMBSTONED" else "HIGH" if relative.startswith("profile/")
                else "MAX" if re.fullmatch(r"M\d+", node_id)
                else "HISTORY" if relative.startswith("history/") else "MEDIUM")
        nodes[tier].append(entry)
    epoch = re.search(r"history/(epoch_\d+)/summaries\.md", text)
    measurement, config = measure_tokens(text), _config(root)
    return {"active_epoch": epoch.group(1) if epoch else "epoch_01", "nodes": nodes,
            "missing_targets": missing, "routing_tokens": measurement["count"], "token_measurement": measurement,
            "router_token_budget": config["router_token_budget"], "within_budget": measurement["count"] <= config["router_token_budget"]}

def count_topics(summaries_md):
    return len(re.findall(r"^###\s+\[(?:Topic|Summary)\s+#\d+\]", _read_utf8(summaries_md), re.M)) if summaries_md.is_file() else 0

def _chunks(root, state):
    """L1 is authoritative. Support both historical field spellings on read."""
    allowed = {n["id"] for tier, group in state["nodes"].items() if tier in ("MAX", "MEDIUM", "TOMBSTONED") for n in group}
    default_project, output = _config(root)["active_mainline"], []
    for path in sorted((root / "chunks").glob("*.json")):
        _safe_path(root, path.relative_to(root).as_posix())
        value = json.loads(_read_utf8(path))
        if not isinstance(value, dict):
            raise MemoryError(f"L1 must contain a JSON object: {path.name}")
        epoch = value.get("epoch", 1)
        if isinstance(epoch, str) and re.fullmatch(r"epoch_\d+", epoch):
            epoch = int(epoch.split("_")[1])
        number = value.get("number", value.get("num"))
        if number is None:
            match = re.search(r"#(\d+)", value.get("topic_id", value.get("id", "")))
            if not match:
                raise MemoryError(f"Missing topic number: {path.name}")
            number = int(match.group(1))
        project = value.get("project", value.get("parent", default_project))
        if project not in allowed:
            raise MemoryError(f"Orphan chunk {path.name}: unknown project {project}")
        body = value.get("summary", value.get("conclusion", []))
        if isinstance(body, str):
            body = [body]
        words = value.get("hitwords", value.get("hit_words", []))
        if type(epoch) is not int or epoch < 1 or type(number) is not int or number < 1:
            raise MemoryError(f"Invalid epoch/topic number: {path.name}")
        if not isinstance(body, list) or not body or not all(isinstance(x, str) for x in body):
            raise MemoryError(f"Invalid summary: {path.name}")
        if not isinstance(words, list) or not all(isinstance(x, str) for x in words):
            raise MemoryError(f"Invalid hitwords: {path.name}")
        status = value.get("status", "ACTIVE")
        if not isinstance(status, str) or not status.strip() or len(status) > 64:
            raise MemoryError(f"Invalid status: {path.name}")
        output.append({"id": path.stem, "topic_id": f"Topic#{number:02d}", "number": number, "epoch": epoch,
                       "project": project, "title": str(value.get("title", "")), "summary": body, "hitwords": words,
                       "status": status, "file": path.relative_to(root).as_posix(),
                       "source": value.get("source", ""), "supersedes": value.get("supersedes", []),
                       "request_id": value.get("request_id"), "request_digest": value.get("request_digest")})
    identities = [(c["epoch"], c["number"]) for c in output]
    if len(identities) != len(set(identities)):
        raise MemoryError("Duplicate topic number within an epoch")
    by_id = {c["id"]: c for c in output}
    request_ids, replaced_ids = set(), set()
    for c in output:
        if not isinstance(c["source"], str) or not isinstance(c["supersedes"], list):
            raise MemoryError(f"Invalid provenance: {c['id']}")
        request_id = c["request_id"]
        if request_id is not None:
            if (not isinstance(request_id, str) or not 1 <= len(request_id) <= 120
                    or not isinstance(c["request_digest"], str) or request_id in request_ids):
                raise MemoryError(f"Invalid or duplicate request_id: {c['id']}")
            request_ids.add(request_id)
        for old_id in c["supersedes"]:
            old = by_id.get(old_id) if isinstance(old_id, str) else None
            if (old is None or old["project"] != c["project"]
                    or (old["epoch"], old["number"]) >= (c["epoch"], c["number"])
                    or old_id in replaced_ids):
                raise MemoryError(f"Invalid supersession in {c['id']}: {old_id}")
            replaced_ids.add(old_id)
    return output

def _digest(root, overrides=None):
    overrides = overrides or {}
    files = {"directory.md", "config.json"}
    files.update(p.relative_to(root).as_posix() for p in (root / "chunks").glob("*.json"))
    files.update(k for k in overrides if k.startswith("chunks/") or k in files)
    digest = hashlib.sha256()
    for relative in sorted(files):
        path = _safe_path(root, relative)
        if relative in overrides:
            content = overrides[relative].encode("utf-8")
        elif path.is_file():
            content = path.read_bytes()
        else:
            continue
        digest.update(relative.encode("utf-8") + b"\0" + content + b"\0")
    return digest.hexdigest()

def _inline(text):
    return re.sub(r"[\r\n`]+", " ", text).replace("[", "(").replace("]", ")").strip()

def _render_summaries(chunks, epoch):
    result = f"# HFA epoch_{epoch:02d} — generated from chunks/; edit L1, then run summarize\n"
    replacements = {old: c["id"] for c in chunks for old in c.get("supersedes", [])}
    for c in sorted((c for c in chunks if c["epoch"] == epoch), key=lambda c: c["number"]):
        result += (f"\n---\n\n### [Topic #{c['number']:02d}] {_inline(c['title'])} "
                   f"`[命中词: {', '.join(_inline(w) for w in c['hitwords'])}]`\n"
                   f"* **ID**: {_inline(c['id'])} | Project: {_inline(c['project'])}\n")
        # Every L1-derived field is untrusted data: flatten it so it cannot forge headings.
        status = ('历史记录；已被 ' + _inline(replacements[c['id']]) + ' 替代' if c['id'] in replacements
                  else _inline(str(c['status'])))
        result += f"* **状态**: {status}\n"
        if c.get("source"):
            result += f"* **来源指针（未自动核实）**: {_inline(c['source'])}\n"
        result += "".join(f"* **核心结论**：{_inline(line)}\n" for line in c["summary"])
    return result

def _summary_updates(root, chunks, epoch):
    relative = f"history/epoch_{epoch:02d}/summaries.md"
    content = _render_summaries(chunks, epoch)
    result = {relative: content}
    previous = root / relative
    if previous.is_file():
        old = _read_utf8(previous)
        if old != content and not old.startswith(f"# HFA epoch_{epoch:02d} — generated from chunks/"):
            # Preserve pre-2.1, potentially Markdown-only records before regenerating.
            digest = hashlib.sha256(old.encode("utf-8")).hexdigest()
            result[f"history/legacy-backups/epoch_{epoch:02d}-{digest}.md"] = old
    return result

_DERIVED_CACHE: dict[tuple[str, str], dict[str, str | bytes]] = {}

def _derived(root, state, chunks, overrides=None):
    if overrides and "directory.md" in overrides:
        state = parse_directory_nodes(root / "directory.md", overrides["directory.md"])
    digest = _digest(root, overrides)
    config = _config(root)
    cache_key = (digest, _json(config))
    cached = _DERIVED_CACHE.get(cache_key)
    if cached is not None:
        return cached.copy()
    entries, projects = {}, {}
    for tier, group in state["nodes"].items():
        for node in group:
            projects[node["id"]] = {"tier": tier, "path": node["path"], "status": node["status"]}
            for word in node["hit_words"]:
                entries.setdefault(_normalize_for_match(word), []).append({"kind": "node", "id": node["id"]})
    for chunk in chunks:
        for word in chunk["hitwords"]:
            entries.setdefault(_normalize_for_match(word), []).append({"kind": "chunk", "id": chunk["id"]})
    documents = {c["id"]: {"heading": sorted(_search_terms(c["title"] + " " + " ".join(c["hitwords"]), config)),
                            "terms": sorted(_search_terms(c["title"] + " " + " ".join(c["hitwords"] + c["summary"]), config))}
                 for c in chunks}
    result = {"index/search.json": _json({"version": 1, "source_digest": digest, "documents": documents}),
              "index/hitwords.json": _json({"version": 1, "source_digest": digest, "entries": entries}),
              "index/projects.json": _json({"version": 1, "source_digest": digest, "entries": projects}),
              "index/fts.sqlite": _build_fts_cached(digest, _json(documents))}
    if len(_DERIVED_CACHE) >= 32:
        _DERIVED_CACHE.clear()
    _DERIVED_CACHE[cache_key] = result
    return result.copy()

def _chunk_text(chunk):
    return (f"[{chunk['id']} | {chunk['topic_id']} | epoch_{chunk['epoch']:02d} | {chunk['project']}] "
            f"{chunk['title']}\n" + "\n".join(chunk["summary"])
            + ("\nSource: " + chunk["source"] if chunk.get("source") else ""))

def _guard_mainline(state, mainline):
    valid = [n for n in state["nodes"]["MAX"] if n["status"] == "ACTIVE"]
    if len(valid) != 1:
        raise MemoryError("L0 must contain exactly one ACTIVE mainline")
    if valid[0]["id"] != mainline or not valid[0]["exists"]:
        raise MemoryError(f"Active mainline {mainline!r} is not present and readable")
    return valid[0]

def _guard_write_project(state, project, main):
    """New topics may target the active mainline, a live branch or another mainline node."""
    if project == main["id"]:
        return
    found = next(((tier, n) for tier, group in state["nodes"].items() for n in group if n["id"] == project), None)
    if found is None:
        raise MemoryError(f"Unknown project {project!r}; add it to L0 first")
    tier, node = found
    if tier not in ("MAX", "MEDIUM"):
        raise MemoryError(f"Project {project!r} is {tier}; archived, profile and history nodes cannot take new topics")
    if not node["exists"]:
        raise MemoryError(f"Project {project!r} target file is missing: {node['path']}")

def _framework_update(root, epoch, status):
    relative = f"history/epoch_{epoch:02d}/framework.md"
    path = root / relative
    text = _read_utf8(path) if path.is_file() else f"# epoch_{epoch:02d}\n"
    if re.search(r"^Status:[ \t]*\S+", text, re.M):
        text = re.sub(r"^Status:[ \t]*\S+", f"Status: {status}", text, count=1, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n\nStatus: {status}\n"
    return {relative: text}

def cmd_init(target_dir, force=False, quiet=False):
    target_dir = resolve_default_memory_dir(target_dir).resolve()
    if target_dir.exists() and not target_dir.is_dir():
        raise MemoryError("Initialization target must be a directory")
    if target_dir.exists() and any(target_dir.iterdir()) and not force:
        raise MemoryError("Target not empty; --force only fills missing files")
    target_dir.mkdir(parents=True, exist_ok=True)
    with _hfa_lock(target_dir):
        updates = {p.relative_to(TEMPLATE_MEMORY_DIR).as_posix(): p.read_bytes()
                   for p in TEMPLATE_MEMORY_DIR.rglob("*")
                   if p.is_file() and not p.name.startswith(".hfa") and "__pycache__" not in p.parts
                   and not (target_dir / p.relative_to(TEMPLATE_MEMORY_DIR)).exists()}
        _commit(target_dir, updates)
    cmd_reindex(target_dir, quiet=quiet)
    if not quiet:
        print(f"[OK] Initialized memory at {target_dir}")
    return 0

def cmd_status(root, as_json=False):
    with _hfa_lock(root):
        state = parse_directory_nodes(root / "directory.md")
        chunks = _chunks(root, state)
        epoch = int(state["active_epoch"].split("_")[1])
        state["topic_count"] = sum(c["epoch"] == epoch for c in chunks)
        state["total_topics"] = len(chunks)
        expected = _derived(root, state, chunks)
        state["stale_indexes"] = [p for p, content in expected.items() if not _file_matches(root / p, content)]
        state["stale_summaries"] = [f"history/epoch_{e:02d}/summaries.md" for e in sorted({c["epoch"] for c in chunks})
                                    if not (root / f"history/epoch_{e:02d}/summaries.md").is_file()
                                    or _read_utf8(root / f"history/epoch_{e:02d}/summaries.md") != _render_summaries(chunks, e)]
        problems = state["missing_targets"] + state["stale_indexes"] + state["stale_summaries"]
        try:
            _guard_mainline(state, _config(root)["active_mainline"])
        except MemoryError as exc:
            problems.append(str(exc))
        if not state["within_budget"]:
            problems.append("Router exceeds measured budget or conservative upper bound")
        state["problems"] = problems
    if as_json:
        print(_json(state), end="")
    else:
        print(f"HFA status: {'PASS' if not problems else 'FAIL'}")
        print(f"L0: {state['routing_tokens']}/{state['router_token_budget']} ({state['token_measurement']['method']})")
        print(f"Epoch: {state['active_epoch']}; topics: {state['topic_count']}; total: {len(chunks)}")
        for problem in problems:
            print(f"[ERROR] {problem}")
    return 1 if problems else 0

def cmd_reindex(root, verify=False, quiet=False):
    with _hfa_lock(root):
        state = parse_directory_nodes(root / "directory.md")
        chunks = _chunks(root, state)
        updates = {"config.json": _json(_config(root))}
        updates.update(_derived(root, state, chunks, updates))
        for epoch in sorted({c["epoch"] for c in chunks}):
            updates.update(_summary_updates(root, chunks, epoch))
        differences = [p for p, content in updates.items() if not _file_matches(root / p, content)]
        if verify:
            print(_json({"consistent": not differences, "differences": differences}), end="")
            return 1 if differences else 0
        _commit(root, updates)
    if not quiet:
        print("[OK] Rebuilt indexes and Markdown views from L0/L1")
    return 0

def cmd_add_topic(root, title, hit_words, conclusion, milestone_log=None, project=None,
                  source="", supersedes=None, request_id=None, as_json=False):
    title = " ".join(title.split())
    lines = [ln.strip() for ln in conclusion.splitlines() if ln.strip()]
    words = list(dict.fromkeys(w.strip() for w in re.split(r"[,，、/]+", hit_words) if w.strip()))
    if not title or len(title) > 120:
        raise MemoryError("Title must contain 1–120 characters")
    if not 1 <= len(lines) <= 3 or len(conclusion.strip()) > 360:
        raise MemoryError("Conclusion must have 1–3 nonempty lines and at most 360 characters")
    if not 1 <= len(words) <= 8 or any(len(w) > 64 or "\n" in w or "\r" in w for w in words):
        raise MemoryError("Provide 1–8 single-line hitwords, each at most 64 characters")
    if not isinstance(source, str) or len(source) > 240 or "\n" in source or "\r" in source:
        raise MemoryError("Source must be a single-line evidence pointer of at most 240 characters")
    if supersedes is None:
        supersedes = []
    if not isinstance(supersedes, list) or len(supersedes) > 16 or any(not isinstance(x, str) for x in supersedes):
        raise MemoryError("supersedes must be a list of up to 16 chunk IDs")
    supersedes = sorted(set(supersedes))
    if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 120 or not request_id.strip()):
        raise MemoryError("request_id must contain 1–120 characters")
    with _hfa_lock(root):
        state, config = parse_directory_nodes(root / "directory.md"), _config(root)
        # L0 must stay healthy (exactly one ACTIVE mainline matching config) for any
        # write; the *target* project may then be that mainline or a live branch.
        main = _guard_mainline(state, config["active_mainline"])
        project = project or main["id"]
        _guard_write_project(state, project, main)
        chunks = _chunks(root, state)
        fingerprint = hashlib.sha256(_json({"title": title, "hitwords": words, "summary": lines,
            "source": source, "supersedes": supersedes, "project": project, "milestone": milestone_log}).encode("utf-8")).hexdigest()
        if request_id:
            previous = next((c for c in chunks if c["request_id"] == request_id), None)
            if previous:
                if previous["request_digest"] != fingerprint:
                    raise MemoryError("request_id already used with different content; use a new ID")
                result = {"id": previous["id"], "duplicate": True, "path": previous["file"]}
                print(_json(result) if as_json else f"[OK] Already stored {previous['id']}", end="" if as_json else "\n")
                return 0
        by_id = {c["id"]: c for c in chunks}
        replaced = {old for c in chunks for old in c["supersedes"]}
        if any(x not in by_id or by_id[x]["project"] != project or by_id[x]["status"] != "ACTIVE"
               or x in replaced for x in supersedes):
            raise MemoryError("Can only supersede current ACTIVE chunks of the same project")
        epoch = int(state["active_epoch"].split("_")[1])
        epoch_chunks = [c for c in chunks if c["epoch"] == epoch]
        updates = {"config.json": _json(config)}
        if len(epoch_chunks) >= config["max_topics_per_epoch"]:
            previous = epoch
            epoch = max([epoch] + [c["epoch"] for c in chunks]) + 1
            epoch_chunks = []
            directory = _read_utf8(root / "directory.md")
            directory = re.sub(r"history/epoch_\d+/summaries\.md", f"history/epoch_{epoch:02d}/summaries.md", directory)
            directory = re.sub(r"活跃纪元:\s*`epoch_\d+`", f"活跃纪元: `epoch_{epoch:02d}`", directory)
            updates["directory.md"] = directory
            # Update only the status line; keep any human-written notes in framework.md.
            updates.update(_framework_update(root, previous, "SEALED"))
            updates.update(_framework_update(root, epoch, "ACTIVE"))
        ordinals = [int(m.group(1)) for p in (root / "chunks").glob("chunk_*.json") if p.is_file() and (m := re.fullmatch(r"chunk_(\d+)\.json", p.name))]
        chunk_id = f"chunk_{max(ordinals, default=0) + 1:04d}"
        number = max((c["number"] for c in epoch_chunks), default=0) + 1
        chunk = {"id": chunk_id, "topic_id": f"Topic#{number:02d}", "number": number, "epoch": epoch,
                 "project": project, "title": title, "summary": lines, "hitwords": words, "status": "ACTIVE",
                 "source": source, "supersedes": supersedes, "request_id": request_id, "request_digest": fingerprint,
                 "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        if measure_tokens(_chunk_text(chunk))["count"] > config["recall_token_budget"]:
            raise MemoryError("Complete topic exceeds recall budget; shorten title and conclusion")
        relative = f"chunks/{chunk_id}.json"
        if (root / relative).exists():
            raise MemoryError(f"Refusing to overwrite existing L1: {relative}")
        updates[relative] = _json(chunk)
        chunks.append({**chunk, "file": relative})
        updates.update(_summary_updates(root, chunks, epoch))
        for previous_epoch in {by_id[old]["epoch"] for old in supersedes} - {epoch}:
            updates.update(_summary_updates(root, chunks, previous_epoch))
        if milestone_log:
            log = root / "history/stream.md"
            now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            updates["history/stream.md"] = (_read_utf8(log) if log.exists() else "") + f"- {now} [{chunk_id}] {_inline(milestone_log)}\n"
        updates.update(_derived(root, state, chunks, updates))
        _commit(root, updates)
    if as_json:
        print(_json({"id": chunk_id, "path": relative, "duplicate": False, "supersedes": supersedes}), end="")
    else:
        print(f"[OK] Added {chunk_id} / epoch_{epoch:02d} / Topic#{number:02d}")
    return 0

def cmd_tombstone(root, branch_id, hit_words=None, reason=None):
    with _hfa_lock(root):
        state = parse_directory_nodes(root / "directory.md")
        branch = next((n for n in state["nodes"]["MEDIUM"] if n["id"] == branch_id), None)
        if branch is None:
            raise MemoryError(f"Active branch not found: {branch_id}")
        words = extract_hit_words(f"[命中词: {hit_words}]") if hit_words else branch["hit_words"]
        words = [_inline(w) for w in words if _inline(w)]
        if not words or any("\n" in w or "\r" in w or len(w) > 64 for w in words):
            raise MemoryError("Invalid tombstone hitwords")
        if re.search(r"➔\s*`[^`]+`", branch["raw"]):
            line = re.sub(r"➔\s*`[^`]+`", "➔ `TOMBSTONED`", branch["raw"], count=1)
        else:
            line = re.sub(re.escape(branch["link"]), branch["link"] + " ➔ `TOMBSTONED`", branch["raw"], count=1)
        if re.search(r"\[命中词[:：][^\]]*\]", line):
            line = re.sub(r"\[命中词[:：][^\]]*\]", f"[命中词: {', '.join(words)}]", line, count=1)
        else:
            line = line.rstrip() + f" | `[命中词: {', '.join(words)}]`"
        raw_directory = _read_utf8(root / "directory.md")
        if branch["raw"] not in raw_directory or line == branch["raw"]:
            raise MemoryError(f"Failed to update L0 entry for branch: {branch_id}")
        directory = raw_directory.replace(branch["raw"], line, 1)
        log = root / "tombstones.jsonl"
        event = {"id": branch_id, "path": branch["path"], "reason": _inline(reason or "completed"), "hitwords": words,
                 "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        updates = {"directory.md": directory, "config.json": _json(_config(root)),
                   "tombstones.jsonl": (_read_utf8(log) if log.exists() else "") + json.dumps(event, ensure_ascii=False) + "\n"}
        state["nodes"]["MEDIUM"].remove(branch)
        state["nodes"]["TOMBSTONED"].append({**branch, "status": "TOMBSTONED", "hit_words": words, "raw": line})
        updates.update(_derived(root, state, _chunks(root, state), updates))
        _commit(root, updates)
    print(f"[OK] Tombstoned {branch_id}; audit event committed")
    return 0

def _append_jsonl(path, event):
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)

def cmd_route(root, query, active_mainline=None, unlock=None, mode="hybrid"):
    if not isinstance(query, str) or not query.strip() or len(query) > 8000:
        raise MemoryError("Query must contain 1–8000 characters")
    if mode not in ("hybrid", "keyword"):
        raise MemoryError("Unknown retrieval mode")
    with _hfa_lock(root):
        state, config = parse_directory_nodes(root / "directory.md"), _config(root)
        active_mainline = active_mainline or config["active_mainline"]
        main = _guard_mainline(state, active_mainline)
        if state["missing_targets"]:
            raise MemoryError("Missing L0 targets: " + ", ".join(state["missing_targets"]))
        if not state["within_budget"]:
            raise MemoryError("L0 exceeds measured router budget; shorten it or change config explicitly")
        chunks = _chunks(root, state)
        expected = _derived(root, state, chunks)
        for relative, content in expected.items():
            if not _file_matches(root / relative, content):
                raise MemoryError(f"Stale index: {relative}; run reindex")
        index = json.loads(_read_utf8(root / "index/hitwords.json"))["entries"]
        positive, refs, matched = _positive_query(query), [], []
        scores, kw_terms, content_terms = {}, {}, {}
        for word, references in index.items():
            if _keyword_matches(word, positive):
                refs.extend(references)
                matched.append({"word": word, "references": references})
                for ref in references:
                    key = (ref["kind"], ref["id"])
                    scores[key] = scores.get(key, 0) + 100
                    kw_terms.setdefault(key, set()).add(word)
        if mode == "hybrid":
            documents = json.loads(_read_utf8(root / "index/search.json"))["documents"]
            fts_bytes = expected.get("index/fts.sqlite")
            for chunk_id, score, terms in _rank_content(positive, documents, config, fts_bytes=fts_bytes):
                key = ("chunk", chunk_id)
                scores[key] = scores.get(key, 0) + score
                content_terms[key] = set(terms)
                refs.append({"kind": "chunk", "id": chunk_id})
        nodes = {n["id"]: (tier, n) for tier, group in state["nodes"].items() for n in group}
        by_id = {c["id"]: c for c in chunks}
        replaced = {old for c in chunks for old in c["supersedes"]}
        candidates, selected, deferred, seen = [], [], [], set()
        for ref in refs:
            identity = (ref["kind"], ref["id"])
            if identity in seen:
                continue
            seen.add(identity)
            match_info = {"method": "keyword" if identity in kw_terms else "content",
                          "terms": sorted(kw_terms.get(identity, set()) | content_terms.get(identity, set()))}
            if ref["kind"] == "chunk":
                c = by_id[ref["id"]]
                if c["id"] in replaced:
                    deferred.append({"id": c["id"], "reason": "superseded; historical evidence only"})
                    continue
                permitted = c["project"] == active_mainline and c["status"] == "ACTIVE"
                candidate = {"kind": "chunk", "id": c["id"], "path": c["file"], "text": _chunk_text(c),
                             "permitted": permitted, "epoch": c["epoch"], "number": c["number"],
                             "match": match_info, "score": round(scores[identity], 6),
                             "source": c["source"], "supersedes": c["supersedes"]}
            else:
                tier, node = nodes[ref["id"]]
                # Only the active mainline and HIGH profile nodes are resident;
                # inactive/planned MAX nodes (e.g. M2) require --unlock like branches.
                permitted = tier == "HIGH" or (tier == "MAX" and node["id"] == active_mainline)
                candidate = {"kind": "node", "id": node["id"], "path": node["path"],
                             "tier": tier, "status": node["status"], "permitted": permitted,
                             "match": match_info, "score": round(scores[identity], 6),
                             "source": "", "supersedes": []}
            candidates.append(candidate)
        # Higher relevance first; newer records win ties, never file/index order.
        candidates.sort(key=lambda c: (c.get("score", 0), c.get("epoch", 0), c.get("number", 0)), reverse=True)
        payload = ""
        for candidate in candidates:
            if candidate["kind"] == "node" and candidate["permitted"]:
                continue  # Main/profile are references, not silently injected text.
            if not candidate["permitted"] and not (unlock and unlock.strip()):
                deferred.append({"id": candidate["id"], "reason": "explicit --unlock reason required"})
                continue
            if candidate["kind"] == "node":
                candidate["text"] = _read_utf8(_safe_path(root, candidate["path"]))
            addition = payload + ("\n\n" if payload else "") + candidate["text"]
            if len(selected) >= config["max_recall_topics"] or measure_tokens(addition)["count"] > config["recall_token_budget"]:
                deferred.append({"id": candidate["id"], "reason": "recall budget or item limit"})
                continue
            payload = addition
            selected.append({k: v for k, v in candidate.items() if k != "permitted"})
        profile_drift = _profile_drift(query)
        selected_ids = {s["id"] for s in selected}
        skipped_tombstones = [
            n["id"] for n in state["nodes"]["TOMBSTONED"]
            if n["id"] not in selected_ids and any(
                (c["kind"] == "node" and c["id"] == n["id"])
                or (c["kind"] == "chunk" and by_id[c["id"]]["project"] == n["id"] and c["id"] not in selected_ids)
                for c in candidates
            )
        ]
        result = {"query": query, "retrieval_mode": mode, "active_mainline_lock": active_mainline, "routed_files": [s["path"] for s in selected],
                  "selected": selected, "resident_references": [main["path"]] + [n["path"] for n in state["nodes"]["HIGH"]],
                  "router_text": _read_utf8(root / "directory.md"), "recall_text": payload,
                  "router_tokens": state["token_measurement"], "recall_tokens": measure_tokens(payload),
                  "router_token_budget": config["router_token_budget"], "recall_token_budget": config["recall_token_budget"],
                  "deferred": deferred, "matched_hit_words": matched, "profile_drift": profile_drift,
                  "lateral_attention_triggered": any(not c["permitted"] for c in candidates) or profile_drift,
                  "skipped_tombstones": skipped_tombstones,
                  "unlock_reason": unlock}
        event = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                 "selected": [s["id"] for s in selected], "unlock_reason": unlock, "profile_drift": profile_drift}
        _append_jsonl(_safe_path(root, "history/access.jsonl"), event)
    print(_json(result), end="")
    return 0

def _ensure_memory_dir(explicit: Path | None, auto_init: bool = False) -> Path:
    target = resolve_default_memory_dir(explicit)
    should_init = auto_init or os.environ.get("HFA_AUTO_INIT", "").strip().lower() in ("1", "true", "yes")
    if should_init and not (target / "directory.md").is_file():
        if not target.exists() or (target.is_dir() and not any(target.iterdir())):
            cmd_init(target, force=True, quiet=True)
    return target

def main():
    parser = argparse.ArgumentParser(description="HFA local file-memory manager (2.2)")
    subs = parser.add_subparsers(dest="command", required=True)
    init = subs.add_parser("init", help="Initialize; --force preserves existing files and fills gaps")
    init.add_argument("memory_dir", nargs="?", type=Path, default=None, help="Target memory directory (defaults to HFA_MEMORY_DIR or ./memory)")
    init.add_argument("--force", action="store_true")
    status = subs.add_parser("status", help="Audit L0, L1, views and full-text token budget")
    status.add_argument("memory_dir", nargs="?", type=Path, default=None, help="Target memory directory (defaults to HFA_MEMORY_DIR or ./memory)")
    status.add_argument("--json", action="store_true")
    status.add_argument("--auto-init", action="store_true", help="Initialize memory directory if not yet initialized")
    topic = subs.add_parser("add-topic", help="Transactionally store L1 and rebuild views")
    topic.add_argument("memory_dir", nargs="?", type=Path, default=None, help="Target memory directory (defaults to HFA_MEMORY_DIR or ./memory)")
    for arg in ("title", "hit-words", "conclusion"):
        topic.add_argument("--" + arg, required=True)
    topic.add_argument("--milestone")
    topic.add_argument("--project")
    topic.add_argument("--source", default="", help="Evidence pointer; stored as data, never fetched")
    topic.add_argument("--supersedes", action="append", default=[], help="Explicit old chunk ID; repeatable")
    topic.add_argument("--request-id", help="Stable event ID for retry-safe writes")
    topic.add_argument("--json", action="store_true")
    topic.add_argument("--auto-init", action="store_true", help="Initialize memory directory if not yet initialized")
    tomb = subs.add_parser("tombstone", help="Logically archive a branch with an audit event")
    tomb.add_argument("memory_dir", nargs="?", type=Path, default=None, help="Target memory directory (defaults to HFA_MEMORY_DIR or ./memory)")
    tomb.add_argument("--id", required=True)
    tomb.add_argument("--hit-words")
    tomb.add_argument("--reason")
    for name in ("route", "recall"):
        route = subs.add_parser(name, help="Return bounded L1 snippets; side branches need --unlock")
        route.add_argument("memory_dir", nargs="?", type=Path, default=None, help="Target memory directory (defaults to HFA_MEMORY_DIR or ./memory)")
        route.add_argument("--query", required=True)
        route.add_argument("--mainline")
        route.add_argument("--mode", choices=("hybrid", "keyword"), default="hybrid")
        route.add_argument("--unlock", help="Explicit reason for reading non-mainline or tombstoned memory")
        route.add_argument("--auto-init", action="store_true", help="Initialize memory directory if not yet initialized")
    for name in ("reindex", "summarize"):
        rebuild = subs.add_parser(name, help="Rebuild indexes and Markdown views from L0/L1")
        rebuild.add_argument("memory_dir", nargs="?", type=Path, default=None, help="Target memory directory (defaults to HFA_MEMORY_DIR or ./memory)")
        rebuild.add_argument("--verify", "--check", dest="verify", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "init":
            return cmd_init(resolve_default_memory_dir(args.memory_dir), args.force)
        target_dir = _ensure_memory_dir(args.memory_dir, getattr(args, "auto_init", False))
        if args.command == "status":
            return cmd_status(target_dir, args.json)
        if args.command == "add-topic":
            return cmd_add_topic(target_dir, args.title, args.hit_words, args.conclusion, args.milestone, args.project,
                                 args.source, args.supersedes, args.request_id, args.json)
        if args.command == "tombstone":
            return cmd_tombstone(target_dir, args.id, args.hit_words, args.reason)
        if args.command in ("route", "recall"):
            return cmd_route(target_dir, args.query, args.mainline, args.unlock, args.mode)
        return cmd_reindex(target_dir, args.verify)
    except (MemoryError, OSError, ValueError, KeyError) as exc:
        print(_json({"error": str(exc), "command": args.command}), end="")
        return 1

if __name__ == "__main__":
    sys.exit(main())
