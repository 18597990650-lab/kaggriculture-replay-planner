"""Shared paths and exact, offline replay reconstruction."""
from copy import deepcopy
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


class Attr(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def __setattr__(self, key, value):
        self[key] = value


def load_json(path):
    path = Path(path)
    raw = path.read_bytes()
    if path.suffix == '.gz':
        raw = gzip.decompress(raw)
    return json.loads(raw)


def write_json(path, value):
    path = Path(path)
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def read_replay(path):
    path = Path(path)
    raw = path.read_bytes()
    if path.suffix == '.gz':
        raw = gzip.decompress(raw)
    replay = json.loads(raw)
    if len(replay['steps']) != 720 or replay['statuses'] != ['DONE', 'DONE']:
        raise ValueError('Expected a complete 720-frame, two-player DONE replay')
    return replay, hashlib.sha256(raw).hexdigest()


def observation(replay, tick, seat):
    result = deepcopy(dict(replay['steps'][tick][0]['observation'],
                           **replay['steps'][tick][seat]['observation']))
    result['step'] = tick
    result['player'] = seat
    return result


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_engine():
    utils = load_module(ROOT / 'reference/seed_utils.py', '_replay_seed_utils')
    package = ModuleType('kaggle_environments')
    shim = ModuleType('kaggle_environments.utils')
    shim.resolve_episode_seed = utils.resolve_episode_seed
    names = ('kaggle_environments', 'kaggle_environments.utils')
    previous = {name: sys.modules.get(name) for name in names}
    sys.modules[names[0]] = package
    sys.modules[names[1]] = shim
    try:
        return load_module(ROOT / 'reference/kaggriculture.py', '_replay_rules')
    finally:
        for name, old in previous.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


def initial_state(replay, rules):
    cfg = Attr(deepcopy(replay['configuration']))
    cfg.pop('seed', None)
    env = SimpleNamespace(configuration=cfg, info=deepcopy(replay['info']), done=False)
    states = [Attr(action={'farmer': ['PASS'], 'hands': [], 'market': []}, reward=None,
                   status='ACTIVE', observation=Attr(step=0, player=side))
              for side in (0, 1)]
    rules._initialize(states, env)
    return states, env


def verify_frame(states, replay, tick):
    for seat in (0, 1):
        expected = observation(replay, tick, seat)
        for key in ('farms', 'private', 'market', 'town', 'day', 'hour', 'player'):
            if states[seat].observation[key] != expected[key]:
                raise AssertionError(('REPLAY_STATE_MISMATCH', tick, seat, key))

