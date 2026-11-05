"""Opt-in, local-only experimental candidate retrieval. No storage or model API."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import time

import numpy as np


def eligible_documents(documents, mainline="M1"):
    documents = deepcopy(documents)
    ids = [doc["id"] for doc in documents]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate document ID")
    replaced = {old for doc in documents for old in doc.get("supersedes", [])}
    return [doc for doc in documents if doc["project"]==mainline
            and doc["status"]=="ACTIVE" and doc["id"] not in replaced]


class LocalEncoder:
    def __init__(self, directory, threads=2):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        directory = Path(directory)
        manifest = json.loads((directory/"manifest.json").read_text(encoding="utf8"))
        for name in ("model.onnx", "tokenizer.json"):
            entry = next(item for item in manifest["files"] if item["local"]==name)
            digest = hashlib.sha256((directory/name).read_bytes()).hexdigest()
            if digest != entry["sha256"]:
                raise ValueError("Model/tokenizer checksum changed: "+name)
        self.manifest = manifest
        started = time.perf_counter()
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(directory/"model.onnx"), sess_options=options,
                                           providers=["CPUExecutionProvider"])
        self.tokenizer = Tokenizer.from_file(str(directory/"tokenizer.json"))
        self.tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
        self.tokenizer.enable_truncation(max_length=512)
        self.load_seconds = time.perf_counter()-started

    def encode(self, texts, kind="query", batch_size=8):
        if kind not in ("query", "passage"):
            raise ValueError("E5 needs query or passage prefix")
        result = []
        for start in range(0, len(texts), batch_size):
            encodings = self.tokenizer.encode_batch([kind+": "+text for text in texts[start:start+batch_size]])
            ids = np.asarray([item.ids for item in encodings], dtype=np.int64)
            mask = np.asarray([item.attention_mask for item in encodings], dtype=np.int64)
            available = {"input_ids":ids, "attention_mask":mask,
                         "token_type_ids":np.zeros_like(ids)}
            inputs = {item.name:available[item.name] for item in self.session.get_inputs()}
            states = self.session.run(None, inputs)[0]
            if states.ndim != 3:
                raise ValueError("Expected E5 token states for masked average pooling")
            pooled = (states*mask[...,None]).sum(axis=1)/mask.sum(axis=1,keepdims=True).clip(1)
            pooled /= np.linalg.norm(pooled,axis=1,keepdims=True).clip(1e-12)
            if pooled.shape[1] != 384 or not np.isfinite(pooled).all():
                raise ValueError("Invalid E5 embedding shape or nonfinite values")
            result.append(pooled.astype(np.float32))
        return np.concatenate(result) if result else np.empty((0,384),dtype=np.float32)


class SemanticCandidates:
    def __init__(self, encoder, documents, mainline="M1"):
        # Filter BEFORE embedding: no branch, inactive or replaced body is encoded.
        self.documents = eligible_documents(documents, mainline)
        self.ids = [doc["id"] for doc in self.documents]
        self.vectors = encoder.encode([doc["title"]+"\n"+doc["body"] for doc in self.documents], "passage")

    def ranked(self, query_vector):
        scores = self.vectors@query_vector
        return sorted(zip(self.ids,map(float,scores)),key=lambda pair:(-pair[1],pair[0]))


def accepted_dense(ranked, threshold, margin, limit=2):
    if (not math.isfinite(threshold) or not -1<=threshold<=1 or not math.isfinite(margin)
            or not 0<=margin<=2 or type(limit) is not int or limit<1
            or any(not math.isfinite(score) for _,score in ranked)):
        raise ValueError("Invalid threshold, margin, limit or similarity score")
    if not ranked or ranked[0][1]<threshold:
        return []
    if len(ranked)>1 and ranked[0][1]-ranked[1][1]<margin:
        return []
    return [key for key,score in ranked[:limit] if score>=threshold]


def reciprocal_rank_fusion(lexical, dense):
    scores = {}
    for items in (lexical,dense):
        for rank,key in enumerate(items,1):
            scores[key] = scores.get(key,0)+1/(60+rank)
    positions = {key:rank for rank,key in enumerate(lexical)}
    return sorted(scores,key=lambda key:(-scores[key],positions.get(key,999),key))
