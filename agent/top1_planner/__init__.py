"""Complete-programme planning tools independent of the live PDR route library."""
import hashlib as _hashlib
import json as _json
import os as _os
from pathlib import Path as _Path
import platform as _platform
import sys as _sys

_backend = _os.environ.get('FARM_PLANNER_BACKEND', 'auto')
if _backend not in ('auto', 'python', 'compiled'):
    raise ValueError('FARM_PLANNER_BACKEND must be auto, python or compiled')
_supported = (_sys.implementation.name == 'cpython' and _sys.version_info[:2] == (3, 11)
              and _sys.platform == 'linux' and _platform.machine().lower() in ('x86_64', 'amd64'))
if _backend == 'compiled' and not _supported:
    raise RuntimeError('Compiled planner requires Linux x86_64 CPython 3.11')
PLANNER_BACKEND = 'python'
if _backend != 'python' and _supported:
    _root = _Path(__file__).resolve().parent.parent
    _directory = _root/'native_planner/cpython-311-linux-x86_64'
    _manifest = _json.loads((_directory/'BUILD.json').read_bytes())
    for _relative, _expected in _manifest['sources'].items():
        if _hashlib.sha256((_root/_relative).read_bytes()).hexdigest() != _expected:
            raise RuntimeError('Compiled planner source changed; rebuild required: '+_relative)
    for _name, _expected in _manifest['artifacts'].items():
        if _hashlib.sha256((_directory/_name).read_bytes()).hexdigest() != _expected:
            raise RuntimeError('Compiled planner artifact mismatch: '+_name)
    __path__.insert(0, str(_directory))
    PLANNER_BACKEND = 'compiled'
