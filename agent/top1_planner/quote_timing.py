"""Recent complete quote costs, normalized by the remaining simulation horizon."""
from collections import deque
import math

_samples={name:deque(maxlen=8) for name in ('full','screen','reference')}


def _kind(settlement):
    return 'full' if settlement is True else 'screen' if settlement is False else 'reference'


def reset():
    for samples in _samples.values():
        samples.clear()


def observe(step,settlement,seconds):
    if not math.isfinite(seconds) or seconds<0:
        raise ValueError('Invalid measured quote duration')
    horizon=max(24,719-int(step))
    _samples[_kind(settlement)].append(seconds/horizon)


def estimate(step,settlement=True):
    horizon=max(24,719-int(step))
    samples=_samples[_kind(settlement)]
    if samples:
        return max(.015,max(samples)*horizon)
    return (.0045*horizon+.18) if settlement is True else (.0007*horizon+.04)


def snapshot(step):
    return {kind:dict(samples=len(samples),seconds=estimate(step,
        True if kind=='full' else False if kind=='screen' else None))
        for kind,samples in _samples.items()}
