"""Opt-in local E5 retrieval with a disposable, content-addressed vector cache.

No network/client SDK is used. Standard-library imports remain sufficient until
the caller explicitly constructs the local ONNX encoder. Cache is not truth.
"""
import hashlib
import json
import math
from pathlib import Path
import sys

DIMENSIONS = 384
MODEL = 'intfloat/multilingual-e5-small'
REVISION = '614241f622f53c4eeff9890bdc4f31cfecc418b3'
PINNED = {'model.onnx':'dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88',
          'tokenizer.json':'0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39'}


def digest(value):
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf8')
    return hashlib.sha256(raw).hexdigest()


def model_identity(directory):
    directory=Path(directory).resolve(strict=True)
    manifest=json.loads((directory/'manifest.json').read_text(encoding='utf8'))
    if manifest.get('model')!=MODEL or manifest.get('revision')!=REVISION:
        raise ValueError('Unsupported semantic model/revision; use the documented pinned local E5 files')
    for name,expected in PINNED.items():
        if hashlib.sha256((directory/name).read_bytes()).hexdigest()!=expected:
            raise ValueError('Semantic model checksum mismatch: '+name)
    return {'model':MODEL,'revision':REVISION,'files':PINNED.copy(),
            'pipeline':'e5-query-passage-mean-l2-512-v1','dimensions':DIMENSIONS}


def snapshot(root, chunks, mainline):
    replaced={old for c in chunks for old in c['supersedes']}
    docs=[{'id':c['id'],'text':c['title']+'\n'+'\n'.join(c['summary'])}
          for c in chunks if c['project']==mainline and c['status']=='ACTIVE' and c['id'] not in replaced]
    docs.sort(key=lambda d:d['id'])
    if len({d['id'] for d in docs})!=len(docs):raise ValueError('Duplicate semantic document ID')
    return {'scope':digest(str(Path(root).resolve())),'mainline':mainline,'documents':docs,
            'fingerprint':digest({'mainline':mainline,'docs':docs})}


def _vector(value):
    if (not isinstance(value,list) or len(value)!=DIMENSIONS
            or any(type(x) not in (int,float) or not math.isfinite(x) for x in value)):
        raise ValueError('Invalid semantic vector shape or nonfinite number')
    norm=sum(x*x for x in value)
    if not .99<=norm<=1.01:raise ValueError('Semantic vector is not L2 normalized')
    return value


def validate_cache(cache):
    if not isinstance(cache,dict) or cache.get('schema')!=1:
        raise ValueError('Unsupported semantic cache schema')
    payload={k:v for k,v in cache.items() if k!='checksum'}
    if cache.get('checksum')!=digest(payload):raise ValueError('Semantic cache checksum mismatch')
    if not isinstance(cache.get('rows'),dict):raise ValueError('Invalid semantic rows')
    for key,row in cache['rows'].items():
        if (not isinstance(key,str) or not isinstance(row,dict)
                or not isinstance(row.get('source_sha256'),str) or len(row['source_sha256'])!=64):
            raise ValueError('Invalid semantic cache row')
        _vector(row.get('vector'))
    return cache


def prepare_cache(state, previous, identity, encode):
    """Only changed eligible texts are encoded. Inputs/old cache are not mutated."""
    prior={}
    status='created'
    if previous is not None:
        try:
            checked=validate_cache(previous)
            if (checked['model']==identity and checked['scope']==state['scope']
                    and checked['mainline']==state['mainline']):
                prior=checked['rows']
                status='updated'
            else:status='rebuilt_identity_or_scope'
        except (ValueError,KeyError,TypeError,OverflowError):status='rebuilt_corrupt'
    rows={}
    missing=[]
    for doc in state['documents']:
        source=digest(doc['text'])
        old=prior.get(doc['id'])
        if old and old['source_sha256']==source:
            rows[doc['id']]={'source_sha256':source,'vector':old['vector'][:]}
        else:missing.append(doc)
    if missing:
        vectors=encode([d['text'] for d in missing])
        if len(vectors)!=len(missing):raise ValueError('Encoder returned the wrong vector count')
        for doc,vector in zip(missing,vectors):
            rows[doc['id']]={'source_sha256':digest(doc['text']),'vector':_vector(vector)}
    payload={'schema':1,'scope':state['scope'],'mainline':state['mainline'],'model':identity,
             'fingerprint':state['fingerprint'],'rows':dict(sorted(rows.items()))}
    current={**payload,'checksum':digest(payload)}
    if current==previous:status='reused'
    return current,{'cache_status':status,'encoded_documents':len(missing),'reused_documents':len(rows)-len(missing),
                    'removed_documents':len(set(prior)-set(rows)),'eligible_documents':len(rows)}


def cache_consistent(cache, state, identity):
    try:
        cache=validate_cache(cache)
        return (cache['model']==identity and cache['scope']==state['scope'] and cache['mainline']==state['mainline']
                and cache['fingerprint']==state['fingerprint'] and set(cache['rows'])=={d['id'] for d in state['documents']}
                and all(cache['rows'][d['id']]['source_sha256']==digest(d['text']) for d in state['documents']))
    except (ValueError,KeyError,TypeError,OverflowError):return False


def validate_options(min_score, min_margin, limit=2):
    if (type(min_score) not in (int,float) or not math.isfinite(min_score) or not -1<=min_score<=1
            or type(min_margin) not in (int,float) or not math.isfinite(min_margin) or not 0<=min_margin<=2
            or type(limit) is not int or limit<1):raise ValueError('Invalid semantic score, margin or limit')


def rank(cache, vector, min_score=.875, min_margin=0, limit=2):
    validate_options(min_score,min_margin,limit)
    validate_cache(cache)
    _vector(vector)
    scored=sorted(((key,sum(a*b for a,b in zip(row['vector'],vector))) for key,row in cache['rows'].items()),
                  key=lambda x:(-x[1],x[0]))
    if not scored or scored[0][1]<min_score:return []
    if len(scored)>1 and scored[0][1]-scored[1][1]<min_margin:return []
    return [(key,score) for key,score in scored[:limit] if score>=min_score]


class EncoderSession:
    """One serial caller, one pinned model, no retained queries or memory documents.

    The manager verifies pinned file hashes on EVERY request before using this
    session. A failed inference discards the encoder; the next call can reload.
    """
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self._encoder = None
        self.loads = 0

    @property
    def loaded(self):
        return self._encoder is not None

    def encode(self, texts, kind="passage"):
        try:
            if self._encoder is None:
                self._encoder = LocalEncoder(self.directory)
                self.loads += 1
            return self._encoder.encode(texts, kind)
        except Exception:
            self._encoder = None
            raise


class LocalEncoder:
    def __init__(self, directory):
        if sys.version_info < (3,12):
            raise ValueError('The optional pinned semantic runtime requires Python 3.12+; the core remains Python 3.10+')
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise ValueError('Local semantic dependencies missing; install the semantic extra in a dedicated environment') from exc
        self.np=np
        options=ort.SessionOptions()
        options.intra_op_num_threads=2
        options.inter_op_num_threads=1
        try:
            self.session=ort.InferenceSession(str(Path(directory)/'model.onnx'),sess_options=options,providers=['CPUExecutionProvider'])
            self.tokenizer=Tokenizer.from_file(str(Path(directory)/'tokenizer.json'))
        except Exception as exc:
            raise ValueError('Could not load the pinned local ONNX model; check model files/runtime compatibility') from exc
        self.tokenizer.enable_padding(pad_id=1,pad_token='<pad>')
        self.tokenizer.enable_truncation(max_length=512)

    def encode(self, texts, kind='query'):
        if kind not in ('query','passage'):raise ValueError('E5 prefix must be query or passage')
        np=self.np
        result=[]
        try:
            for start in range(0,len(texts),8):
                encoded=self.tokenizer.encode_batch([kind+': '+x for x in texts[start:start+8]])
                ids=np.asarray([x.ids for x in encoded],dtype=np.int64)
                mask=np.asarray([x.attention_mask for x in encoded],dtype=np.int64)
                available={'input_ids':ids,'attention_mask':mask,'token_type_ids':np.zeros_like(ids)}
                states=self.session.run(None,{x.name:available[x.name] for x in self.session.get_inputs()})[0]
                if states.ndim!=3 or states.shape[:2]!=ids.shape or states.shape[2]!=DIMENSIONS:
                    raise ValueError('Unexpected E5 output shape')
                pooled=(states*mask[...,None]).sum(axis=1)/mask.sum(axis=1,keepdims=True).clip(1)
                pooled/=np.linalg.norm(pooled,axis=1,keepdims=True).clip(1e-12)
                result.extend(_vector(x) for x in pooled.astype(np.float32).tolist())
        except Exception as exc:
            raise ValueError('Local embedding inference failed; no result was produced. Check the pinned model/runtime') from exc
        return result
