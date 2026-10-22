"""Cache immutable source code and decoded JSON bytes, never live policy state."""
import ast
import base64
import builtins
from functools import lru_cache
import hashlib
import importlib.util
import itertools
from pathlib import Path
import zlib

EXPECTED_SHA256 = 'a2047ebd8ca5720221e1421529655d9c67a7b2fedb74e874c7d3c55a8970ac7e'
_NAMES = itertools.count()


@lru_cache(maxsize=4)
def _module_code(source_bytes, filename):
    if hashlib.sha256(source_bytes).hexdigest()!=EXPECTED_SHA256:
        raise ValueError('Only the audited immutable source is supported')
    tree = ast.parse(source_bytes,filename=filename)
    assert not any(isinstance(n,ast.Name) and n.id=='_CACHED_PAYLOAD_BYTES' for n in ast.walk(tree))
    assignments = [n for n in tree.body if isinstance(n,ast.Assign)
                   and len(n.targets)==1 and isinstance(n.targets[0],ast.Name) and n.targets[0].id=='_PAYLOAD']
    assert len(assignments)==1
    expression = assignments[0].value
    assert isinstance(expression,ast.Call) and ast.unparse(expression.func)=='json.loads'
    decompression = expression.args[0]
    assert isinstance(decompression,ast.Call) and ast.unparse(decompression.func)=='zlib.decompress'
    decoding = decompression.args[0]
    assert isinstance(decoding,ast.Call) and ast.unparse(decoding.func)=='base64.b85decode'
    assert len(decoding.args)==1 and isinstance(decoding.args[0],ast.Constant)
    payload = zlib.decompress(base64.b85decode(decoding.args[0].value))
    expression.args[0] = ast.copy_location(ast.Name(id='_CACHED_PAYLOAD_BYTES',ctx=ast.Load()),decompression)
    ast.fix_missing_locations(tree)
    return compile(tree,filename,'exec',dont_inherit=True),payload


@lru_cache(maxsize=8)
def _embedded_code(text):
    if not isinstance(text,str):
        raise TypeError('The audited embedded source must be text')
    return compile(text,'<string>','exec',dont_inherit=True)


def _execute_embedded(text, namespace, local_namespace=None):
    if not isinstance(namespace,dict):
        raise TypeError('The audited embedded source requires an explicit namespace')
    builtins.exec(_embedded_code(text),namespace,local_namespace)


def load_source(path):
    path = Path(path)
    code,payload = _module_code(path.read_bytes(),str(path))
    spec = importlib.util.spec_from_file_location('cached_rival_source_'+str(next(_NAMES)),path)
    module = importlib.util.module_from_spec(spec)
    module.__dict__.update(_CACHED_PAYLOAD_BYTES=payload,exec=_execute_embedded)
    builtins.exec(code,module.__dict__)
    return module
