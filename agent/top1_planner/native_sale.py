"""Batch sale-valuation arrays across the native boundary once per product."""
import ctypes
import struct
from functools import lru_cache
import os
from pathlib import Path
import platform

from .sale_ledger import fixed_flow_values


class Parameters(ctypes.Structure):
    _fields_ = [('base', ctypes.c_double), ('anchor', ctypes.c_double),
                ('threshold', ctypes.c_double), ('below_target', ctypes.c_double),
                ('above_target', ctypes.c_double), ('below_curve', ctypes.c_uint32),
                ('above_curve', ctypes.c_uint32)]


class Event(ctypes.Structure):
    _fields_ = [('tick', ctypes.c_int32), ('own', ctypes.c_uint32),
                ('rival', ctypes.c_uint32), ('demand', ctypes.c_double)]


_EVENT_PACK = struct.Struct('<iII4xd')
if ctypes.sizeof(Event) != _EVENT_PACK.size or tuple(
        getattr(Event, name).offset for name, _ in Event._fields_) != (0, 4, 8, 16):
    raise RuntimeError('Native event packing ABI mismatch')


_CURVES = {'linear': 0, 'sq': 1, 'sqrt': 2, 'log': 3, 'log10': 4, 'hinge': 5}
_BACKEND = os.environ.get('FARM_SALE_BACKEND', 'rust')
if _BACKEND not in ('rust', 'python'):
    raise ValueError('FARM_SALE_BACKEND must be rust or python')


@lru_cache(maxsize=1)
def library():
    machine = platform.machine().lower()
    if machine not in ('amd64', 'x86_64'):
        raise RuntimeError(f'Native sale valuation has no build for {machine}')
    system = platform.system()
    filename = {'Windows': 'farm_sale.dll', 'Linux': 'libfarm_sale.so'}.get(system)
    if filename is None:
        raise RuntimeError(f'Native sale valuation has no build for {system}')
    path = Path(__file__).resolve().parent.parent / 'native_sale' / 'bin' / filename
    if not path.is_file():
        raise RuntimeError(f'Required native sale library is missing: {path}')
    module = ctypes.CDLL(str(path))
    module.fv_abi_version.argtypes = []
    module.fv_abi_version.restype = ctypes.c_uint32
    if module.fv_abi_version() != 1:
        raise RuntimeError('Native sale valuation ABI mismatch')
    module.fv_evaluate.argtypes = [ctypes.POINTER(Parameters), ctypes.c_double,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32, ctypes.POINTER(Event), ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t), ctypes.c_size_t, ctypes.POINTER(ctypes.c_int32), ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_int64), ctypes.c_size_t, ctypes.POINTER(ctypes.c_int64), ctypes.c_size_t]
    module.fv_evaluate.restype = ctypes.c_int32
    module.fv_clear_cache.argtypes = []
    module.fv_clear_cache.restype = None
    return module


@lru_cache(maxsize=36)
def _parameters(base, anchor, threshold, below_target, above_target, below_curve, above_curve):
    return Parameters(base, anchor, threshold, below_target, above_target,
                      _CURVES.get(below_curve, 0), _CURVES.get(above_curve, 0))


def evaluate(params, inventory, quantity, max_hold, scenarios, windows, start, *, include_tables=False):
    """Return the exact conservative offer arrays, and optionally all window rows."""
    if not isinstance(quantity, int) or not isinstance(max_hold, int) or not 0 <= max_hold <= quantity <= 10000:
        raise ValueError('Invalid current sale quantity or hold bound')
    if len(scenarios) != 2:
        raise ValueError('Sale admission requires exactly two declared scenarios')
    parameters = (params['base'], params['I0'], params['T'], params['below_target'],
                  params['above_target'], params['below_func'], params['above_func'])
    offsets = [0]
    rows = []
    for scenario in scenarios:
        rows.extend(scenario)
        offsets.append(len(rows))
    payload = b''.join(_EVENT_PACK.pack(*row) for row in rows)
    offsets, windows = tuple(offsets), tuple(windows)
    if not include_tables:
        arrays = _offer_arrays(parameters, inventory, quantity, max_hold, payload, offsets, windows, start)
        return [list(column) for column in arrays]
    return _evaluate_native(parameters, inventory, quantity, max_hold, payload, offsets, windows,
                            start, include_tables=True)


@lru_cache(maxsize=4096)
def _offer_arrays(parameters, inventory, quantity, max_hold, payload, offsets, windows, start):
    """Share exact inputs across candidates; cached results remain immutable."""
    arrays = _evaluate_native(parameters, inventory, quantity, max_hold, payload, offsets, windows, start)
    return tuple(tuple(column) for column in arrays)


def clear_result_cache():
    _offer_arrays.cache_clear()


def result_cache_info():
    return _offer_arrays.cache_info()


def _evaluate_native(parameters, inventory, quantity, max_hold, payload, offsets, windows, start,
                     *, include_tables=False):
    p = _parameters(*parameters)
    events = (Event * (len(payload) // _EVENT_PACK.size)).from_buffer_copy(payload)
    scenario_count = len(offsets) - 1
    offset_array = (ctypes.c_size_t * len(offsets))(*offsets)
    window_array = (ctypes.c_int32 * len(windows))(*windows)
    holds = max_hold + 1
    offers = (ctypes.c_int64 * (holds * 6))()
    table_count = scenario_count * len(windows) * holds * 3 if include_tables else 0
    table_array = (ctypes.c_int64 * table_count)() if table_count else None
    code = library().fv_evaluate(ctypes.byref(p), inventory, quantity, max_hold, start,
        events, len(events), offset_array, scenario_count, window_array, len(windows),
        table_array, table_count, offers, len(offers))
    if code:
        meanings = {1: 'invalid price parameters', 2: 'invalid dimensions or input values',
                    3: 'missing release event', 5: 'native panic'}
        raise ValueError(f'Native sale valuation failed ({code}): {meanings.get(code, "unknown error")}')
    arrays = [[offers[hold * 6 + column] for hold in range(holds)] for column in range(6)]
    if not include_tables:
        return arrays
    tables = []
    for index in range(scenario_count):
        values = {}
        for w, tick in enumerate(windows):
            base = (index * len(windows) + w) * holds * 3
            values[tick] = [tuple(table_array[base + hold * 3 + j] for j in range(3)) for hold in range(holds)]
        tables.append(values)
    return arrays, tables


def reference(ladder, inventory, quantity, max_hold, scenarios, windows, start):
    tables = fixed_flow_values(ladder.quote, inventory, quantity, max_hold, scenarios, windows,
                               sum_quotes=ladder.sum_counts)
    gains, ticks = [0] * (max_hold + 1), [start] * (max_hold + 1)
    own, rival, batch, other = ([0] * (max_hold + 1) for _ in range(4))
    first, second = tables
    for tick in windows:
        for hold, (a, b) in enumerate(zip(first[tick][1:], second[tick][1:]), 1):
            score = a[0] + a[1] - a[2]
            other_score = b[0] + b[1] - b[2]
            selected = a
            if other_score < score:
                score, selected = other_score, b
            if score > gains[hold]:
                gains[hold], ticks[hold] = score, tick
                batch[hold], other[hold], rival[hold] = selected
                own[hold] = selected[0] + selected[1]
    return [gains, ticks, own, rival, batch, other]


def waiting_values(ladder, params, inventory, quantity, max_hold, scenarios, windows, start):
    if _BACKEND == 'python':
        return reference(ladder, inventory, quantity, max_hold, scenarios, windows, start)
    return evaluate(params, inventory, quantity, max_hold, scenarios, windows, start)
