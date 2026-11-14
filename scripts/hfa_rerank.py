"""Explicit local pair-relevance fallback; similarity is not factual support.

Only prefiltered active-mainline candidates reach this module. No model download,
network, memory writes, answer generation, or unbounded candidate expansion.
"""
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys

MODEL = 'cross-encoder/mmarco-mMiniLMv2-L12-H384-v1'
REVISION = '1427fd652930e4ba29e8149678df786c240d8825'
PINNED = {'model.onnx': '6c2513767fb63d008a4377bef7a7a3555433d9436342bb53e35a3a72ffc52d4b',
          'tokenizer.json': '62c24cdc13d4c9952d63718d6c9fa4c287974249e16b7ade6d5a85e7bbb75626'}
CANDIDATES_PER_QUERY = 8
MAX_PAIRS = 24
MIN_DENSE_SCORE = .75
MIN_SCORE = .6
MIN_MARGIN = .2


def _binding_module():
    spec = importlib.util.spec_from_file_location('hfa_local_binding', Path(__file__).with_name('hfa_binding.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def model_identity(directory):
    directory = Path(directory).resolve(strict=True)
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf8'))
    if manifest.get('model') != MODEL or manifest.get('revision') != REVISION:
        raise ValueError('Unsupported local reranker model/revision')
    for name, checksum in PINNED.items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != checksum:
            raise ValueError('Local reranker checksum mismatch: ' + name)
    return {'model': MODEL, 'revision': REVISION, 'files': PINNED.copy(),
            'pipeline': 'pair-longest-first-512-singleton-v1'}


def fields_present(query, body):
    """Conservative finite missing-field veto, shared with hfa_binding."""
    return _binding_module().fields_present(query, body)


def select_candidate(query, candidates, reference_date=None):
    """At most one candidate; never promote a weaker hit after a binding veto."""
    if not candidates:
        return None
    if any(type(c['pair_score']) not in (int, float) or not math.isfinite(c['pair_score'])
           or not 0 <= c['pair_score'] <= 1
           or ('similarity' in c and (type(c['similarity']) not in (int, float)
                                      or not math.isfinite(c['similarity']) or not -1 <= c['similarity'] <= 1))
           for c in candidates):
        raise ValueError('Invalid local pair relevance score')
    ordered = sorted(candidates, key=lambda c: (-c['pair_score'], c['id']))
    best = ordered[0]
    if best.get('similarity', 1.) < MIN_DENSE_SCORE or best['pair_score'] < MIN_SCORE:
        return None
    if len(ordered) > 1 and best['pair_score'] - ordered[1]['pair_score'] < MIN_MARGIN:
        return None
    binding = _binding_module()
    parsed_date = binding.parse_reference_date(reference_date)
    return best if binding.binding_support(query, best['text'], reference_date=parsed_date)['allowed'] else None


class PairSession:
    """Retain only loaded weights. Manager verifies pinned hashes per request."""
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self._model = None
        self.loads = 0

    @property
    def loaded(self):
        return self._model is not None

    def score_pairs(self, pairs):
        if (not isinstance(pairs, list) or len(pairs) > MAX_PAIRS
                or any(not isinstance(pair, (tuple, list)) or len(pair) != 2
                       or any(not isinstance(text, str) for text in pair) for pair in pairs)):
            raise ValueError('Invalid bounded local reranker input')
        if not pairs:
            return []
        try:
            if self._model is None:
                self._model = LocalPairModel(self.directory)
                self.loads += 1
            return self._model.score_pairs(pairs)
        except Exception:
            self._model = None
            raise


class LocalPairModel:
    def __init__(self, directory):
        if sys.version_info < (3, 12):
            raise ValueError('Optional local reranking requires Python 3.12+')
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise ValueError('Local reranker needs the existing semantic extra') from exc
        self.np = np
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(Path(directory) / 'model.onnx'), sess_options=options,
                                            providers=['CPUExecutionProvider'])
        self.tokenizer = Tokenizer.from_file(str(Path(directory) / 'tokenizer.json'))
        self.tokenizer.enable_padding(pad_id=1, pad_token='<pad>')
        self.tokenizer.enable_truncation(max_length=512)

    def score_pairs(self, pairs):
        result = []
        for pair in pairs:
            # Singleton inference prevents quantized batch-composition drift.
            encoded = self.tokenizer.encode_batch([tuple(pair)])
            available = {'input_ids': self.np.asarray([e.ids for e in encoded], dtype=self.np.int64),
                         'attention_mask': self.np.asarray([e.attention_mask for e in encoded], dtype=self.np.int64),
                         'token_type_ids': self.np.asarray([e.type_ids for e in encoded], dtype=self.np.int64)}
            logits = self.session.run(None, {i.name: available[i.name] for i in self.session.get_inputs()})[0]
            if logits.shape != (1, 1) or not self.np.isfinite(logits).all():
                raise ValueError('Invalid local reranker output shape or score')
            score = 1 / (1 + math.exp(-max(-60., min(60., float(logits[0, 0])))))
            result.append(score)
        return result
