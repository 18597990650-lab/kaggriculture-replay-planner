"""Boey calendars on the retained P110 public forecast and physical engine."""
import importlib.util
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location('boey02_native_policy', HERE / 'native_policy.py')
_RUNTIME = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = _RUNTIME
spec.loader.exec_module(_RUNTIME)

from top3_supply import blend as _supply_blend
from p110_integration import Integration

_SUPPLY_MODEL = json.loads((HERE / 'TOP3_SUPPLY_PRIOR.json').read_bytes())
_PUBLIC_WORLDS = _RUNTIME.public_worlds
_INTEGRATION_CONFIG = json.loads((HERE / 'P110_INTEGRATION.json').read_bytes())
_P110 = Integration(HERE / 'behavior_model', _INTEGRATION_CONFIG['mode'], _RUNTIME._TRACE)
_RUNTIME.observe_public_prediction = _P110.observe
_RUNTIME.reset_public_prediction = _P110.reset


def _learned_supply_worlds(observation, **kwargs):
    worlds = _supply_blend(observation, _PUBLIC_WORLDS(observation, **kwargs), _SUPPLY_MODEL, 0.35)
    return _P110.worlds(observation, worlds)


_RUNTIME.public_worlds = _learned_supply_worlds


def get_trace_records():
    return _RUNTIME.get_trace_records()


def agent(observation, configuration):
    return _RUNTIME.agent(observation, configuration)
