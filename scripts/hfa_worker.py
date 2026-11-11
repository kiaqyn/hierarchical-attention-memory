"""Private serial recall worker: retain one encoder, preserve process isolation.

Only explicit semantic MCP mode uses this worker. The memory/model paths are
fixed at launch; requests cannot choose paths, modes, thresholds or commands.
An interrupted request is never replayed automatically: it may have committed
turn accounting before its response was lost. Retrying must keep real turn IDs.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

MAX_REQUEST = 128 * 1024
MAX_RESPONSE = 16 * 1024 * 1024
ALLOWED = {"query", "unlock", "conversation_id", "turn_id", "max_topics", "refinement_queries"}


def validate_request(arguments):
    if not isinstance(arguments, dict) or set(arguments) - ALLOWED:
        raise ValueError("Unsupported recall worker arguments")
    if any(value is not None and not isinstance(value, str)
           for key, value in arguments.items() if key not in ("max_topics", "refinement_queries")):
        raise ValueError("Recall worker arguments must be strings or null")
    limit = arguments.get("max_topics")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("max_topics must be a positive integer")
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > 8000:
        raise ValueError("Query must contain 1–8000 characters")
    refinements = arguments.get("refinement_queries")
    if refinements is not None and (not isinstance(refinements, list) or len(refinements) > 3
            or any(not isinstance(q, str) or not q.strip() or len(q) > 256 for q in refinements)):
        raise ValueError("Invalid bounded refinement_queries")


class WorkerError(RuntimeError):
    pass


class RecallWorker:
    """Lazy child with bounded queue/execution wait; no shell or shared stdout."""
    def __init__(self, root, model, timeout=120, *, reranker_model=None):
        self.root = Path(root).resolve()
        self.model = Path(model).resolve()
        self.reranker_model = Path(reranker_model).resolve() if reranker_model is not None else None
        self.timeout = timeout
        self._process = None
        self._lock = threading.Lock()
        self._sequence = 0
        self._closed = False

    def _start(self):
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        self._process = subprocess.Popen(
            [sys.executable, "-B", str(Path(__file__).resolve()), "--memory-dir", str(self.root),
             "--model-dir", str(self.model)] + (["--reranker-model", str(self.reranker_model)] if self.reranker_model is not None else []),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _discard(self):
        process, self._process = self._process, None
        if process is None:
            return
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout):
            stream.close()

    def close(self):
        with self._lock:
            self._closed = True
            self._discard()

    def recall(self, **arguments):
        validate_request(arguments)
        deadline = time.monotonic() + self.timeout
        if not self._lock.acquire(timeout=self.timeout):
            raise WorkerError("Semantic recall queue timed out; no request was sent")
        try:
            if self._closed:
                raise WorkerError("Semantic worker has been closed")
            self._sequence += 1
            request = {"id": self._sequence, "arguments": arguments}
            raw = (json.dumps(request, ensure_ascii=False, allow_nan=False) + "\n").encode("utf8")
            if len(raw) > MAX_REQUEST:
                raise WorkerError("Semantic recall request is too large")
            if self._process is None or self._process.poll() is not None:
                self._discard()
                self._start()
            process = self._process
            response_queue = queue.Queue(maxsize=1)

            def exchange():
                try:
                    process.stdin.write(raw)
                    process.stdin.flush()
                    data = process.stdout.readline(MAX_RESPONSE + 1)
                    response_queue.put(data)
                except (OSError, ValueError) as exc:
                    response_queue.put(exc)

            reader = threading.Thread(target=exchange, daemon=True)
            reader.start()
            try:
                data = response_queue.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as exc:
                self._discard()
                reader.join(timeout=1)
                raise WorkerError("Semantic recall timed out; worker stopped. Retry with the SAME conversation_id and turn_id") from exc
            if isinstance(data, Exception) or not data or len(data) > MAX_RESPONSE or not data.endswith(b"\n"):
                self._discard()
                raise WorkerError("Semantic worker stopped or returned an invalid response; retry with the SAME turn IDs")
            try:
                response = json.loads(data)
                if not isinstance(response, dict) or response.get("id") != request["id"]:
                    raise ValueError("Mismatched response")
                if type(response.get("ok")) is not bool:
                    raise ValueError("Missing status")
                if response["ok"] and not isinstance(response.get("result"), dict):
                    raise ValueError("Invalid result")
                if not response["ok"] and not isinstance(response.get("error"), str):
                    raise ValueError("Invalid error")
            except (ValueError, UnicodeError) as exc:
                self._discard()
                raise WorkerError("Invalid semantic worker response; retry with the SAME turn IDs") from exc
            if not response["ok"]:
                raise WorkerError(response["error"])
            return response["result"]
        finally:
            self._lock.release()


def serve(root, model, reranker_model=None):
    # Script execution resolves these siblings in source and installed wheels.
    import hfa_manager as manager
    runtime = manager._semantic_module().EncoderSession(model)
    pair_runtime = manager._rerank_module().PairSession(reranker_model) if reranker_model is not None else None
    while True:
        line = sys.stdin.buffer.readline(MAX_REQUEST + 1)
        if not line:
            return 0
        if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
            return 2
        request_id = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or set(request) != {"id", "arguments"} or type(request["id"]) is not int:
                raise ValueError("Invalid worker envelope")
            request_id = request["id"]
            validate_request(request["arguments"])
            result = manager.cmd_route(root, mode="semantic", semantic_model=model,
                                       semantic_runtime=runtime, semantic_reranker=reranker_model,
                                       reranker_runtime=pair_runtime, return_result=True, **request["arguments"])
            response = {"id": request_id, "ok": True, "result": result}
        except (manager.MemoryError, OSError, ValueError, KeyError) as exc:
            response = {"id": request_id, "ok": False, "error": str(exc)}
        except Exception:
            # Native crashes/unknown exceptions must not become ordinary zero hits.
            response = {"id": request_id, "ok": False, "error": "Unexpected semantic worker failure; inspect the local installation"}
        output = (json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n").encode("utf8")
        if len(output) > MAX_RESPONSE:
            output = (json.dumps({"id": request_id, "ok": False, "error": "Semantic response exceeds transport limit; reduce memory budgets"}) + "\n").encode("utf8")
        sys.stdout.buffer.write(output)
        sys.stdout.buffer.flush()


def main():
    parser = argparse.ArgumentParser(description="Private HFA semantic recall worker")
    parser.add_argument("--memory-dir", required=True, type=Path)
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--reranker-model", type=Path)
    args = parser.parse_args()
    return serve(args.memory_dir.resolve(strict=True), args.model_dir.resolve(strict=True),
                 args.reranker_model.resolve(strict=True) if args.reranker_model is not None else None)


if __name__ == "__main__":
    sys.exit(main())
