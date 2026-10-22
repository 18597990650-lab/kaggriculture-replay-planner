"""Batch an accepted trade tape into the native exact marginal repricer."""
import ctypes
from functools import lru_cache
import struct
from .native_sale import library, Parameters, _parameters

_TRADE = struct.Struct('<4i')


@lru_cache(maxsize=1)
def _entry():
    call = library().fv_reprice
    call.argtypes = [ctypes.POINTER(Parameters), ctypes.c_double, *([ctypes.c_int32]*8),
        ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t,
        ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)]
    call.restype = ctypes.c_int32
    return call


def reprice(params, inventory, start, seat, known_rate, unlock, unknown, interval, center,
            has_center, probabilities, events):
    p = _parameters(params['base'], params['I0'], params['T'], params['below_target'],
                    params['above_target'], params['below_func'], params['above_func'])
    rates = (ctypes.c_int32*len(probabilities))(*(rate for rate, _ in probabilities))
    weights = (ctypes.c_double*len(probabilities))(*(weight for _, weight in probabilities))
    rows = [_TRADE.pack(tick, index, side, sign) for tick, rounds in sorted(events.items())
            for index, trades in enumerate(rounds) for side, sign in trades]
    payload = b''.join(rows)
    output = (ctypes.c_double*5)()
    code = _entry()(ctypes.byref(p), inventory, start, seat, known_rate, unlock, unknown, interval,
                    center, int(has_center), rates, weights, len(rates), payload, len(rows), output)
    if code:
        raise ValueError(f'Native marginal repricing failed ({code})')
    return (output[0], output[1]), output[2], int(output[3]), int(output[4])
