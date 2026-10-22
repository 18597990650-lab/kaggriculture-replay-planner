"""One native call for biological supply, sale windows and funded space allocation."""
import ctypes
from functools import lru_cache
import struct

from . import rules
from .native_sale import library, Parameters, _parameters
from . import PLANNER_BACKEND

if PLANNER_BACKEND == 'compiled':
    from .settlement_packet import pack_inputs

_PRODUCTS = tuple(rules.PRODUCTS)
_INDEX = {item: index for index, item in enumerate(_PRODUCTS)}
_ANIMAL_PRODUCT = {item: _INDEX[data['product']] for item, data in rules.ANIMALS.items()}
_HEADER = struct.Struct('<iiIiII2d18d36I')
_ASSET = struct.Struct('<8i')
_DISTANCE = tuple(tuple(min(abs(x-a)+abs(y-b) for a,b in ((4,4),(4,5),(5,4),(5,5)))
                       for x in range(10)) for y in range(10))


class SaleRecord(ctypes.Structure):
    _fields_ = [('item', ctypes.c_uint32), ('quantity', ctypes.c_uint32),
                ('gain', ctypes.c_int64), ('own', ctypes.c_int64),
                ('batch', ctypes.c_int64), ('other', ctypes.c_int64),
                ('rival', ctypes.c_int64), ('window', ctypes.c_int64)]


class SaleResult(ctypes.Structure):
    _fields_ = [('amounts', ctypes.c_uint32 * 9), ('count', ctypes.c_uint32),
                ('cash', ctypes.c_double), ('records', SaleRecord * 9)]


if _HEADER.size != 328 or ctypes.sizeof(SaleRecord) != 56 or ctypes.sizeof(SaleResult) != 552:
    raise RuntimeError('Native settlement ABI layout mismatch')


@lru_cache(maxsize=4)
def _bundle(parameters):
    return (Parameters * 9)(*(_parameters(*p) for p in parameters))


@lru_cache(maxsize=1)
def _entry():
    module = library()
    module.fv_settlement_abi_version.argtypes = []
    module.fv_settlement_abi_version.restype = ctypes.c_uint32
    if module.fv_settlement_abi_version() != 1:
        raise RuntimeError('Native settlement ABI version mismatch')
    call = module.fv_sale_caps
    call.argtypes = [ctypes.POINTER(Parameters), ctypes.c_void_p, ctypes.c_size_t,
                     ctypes.POINTER(SaleResult)]
    call.restype = ctypes.c_int32
    return call


@lru_cache(maxsize=512)
def _evaluate(parameters, payload):
    result = SaleResult()
    code = _entry()(_bundle(parameters), payload, len(payload), ctypes.byref(result))
    if code:
        raise ValueError(f'Native settlement failed ({code})')
    records = tuple((r.item, r.quantity, r.gain, r.own, r.batch, r.other, r.rival, r.window)
                    for r in result.records[:result.count])
    return tuple(result.amounts), result.cash, records


def clear_result_cache():
    _evaluate.cache_clear()
    _evaluate_prediction.cache_clear()


def result_cache_info():
    return _evaluate.cache_info()


@lru_cache(maxsize=64)
def _shop_consumption(shops):
    consumption = [0.0] * 9
    for shop in shops:
        products = rules.SHOPS[shop]
        for item in products:
            consumption[_INDEX[item]] += 2 if len(products) == 1 else 1
    return tuple(consumption)


def _reference_sale_caps(observation, farm, private, market, reserves, cash_required, *, incoming_purchase_units=0):
    amounts = tuple(max(0, private['shed'].get(p, 0)-reserves.get(p, 0)) for p in _PRODUCTS)
    start = int(observation['step'])
    end = min(718, start+144)
    eligible = {i for i, q in enumerate(amounts) if q and 0 < i < 8}
    capacity = max(0, 100-sum(sum(bag.values()) for bag in private['inventories'])
                   -sum(reserves.values())-sum(q for p, q in private['shed'].items() if p not in _INDEX)
                   -incoming_purchase_units)
    if not eligible or end <= start or not capacity:
        return dict(zip(_PRODUCTS, amounts)), None
    params = market.get('params') or rules.MARKET_PARAMS
    parameters = tuple((p['base'], p['I0'], p['T'], p['below_target'], p['above_target'],
                        p['below_func'], p['above_func']) for p in (params[item] for item in _PRODUCTS))
    consumption = list(_shop_consumption(tuple(observation['town']['unlocked_shops'])))
    for p, q in observation['town'].get('_forecast_shop_consumption', {}).items():
        if p in _INDEX:
            consumption[_INDEX[p]] += q
    bags = [0] * 9
    for bag in private['inventories']:
        for p, q in bag.items():
            if p in _INDEX:
                bags[_INDEX[p]] += q
    cargo = observation.get('_public_rival_cargo')
    quantities = cargo['quantities'] if cargo else {}
    upper = cargo.get('uncertain_upper', {}) if cargo else {}
    flags = (int(bool(quantities)) | (int(bool(quantities or upper)) << 1)) if cargo else 0
    assets = []
    day = start//24
    for side, source in enumerate((farm, observation['farms'][1-int(observation['player'])])):
        for y, row in enumerate(source['tiles']):
            for x, tile in enumerate(row):
                if not isinstance(tile, dict):
                    continue
                animal = tile.get('animal')
                product = _ANIMAL_PRODUCT[animal] if animal else _INDEX.get(tile.get('crop'), -1)
                if product not in eligible:
                    continue
                age = day-tile['placed_day' if animal else 'planted_day']
                assets.append(_ASSET.pack(product, age, tile.get('yield_units', 0),
                    tile.get('pending_care_bonus', 0) if animal else 0,
                    0 if animal else max(-1, tile.get('fertilized_until_day', -1)-day),
                    int(bool(tile.get('watered_today'))) if not animal else 0, _DISTANCE[y][x], side))
    payload = _HEADER.pack(start, end, capacity, cargo['origin'] if cargo else 0, flags, len(assets),
        farm['money'], cash_required, *(market['inventory'][p] for p in _PRODUCTS), *consumption,
        *amounts, *bags, *(quantities.get(p, 0) for p in _PRODUCTS), *(upper.get(p, 0) for p in _PRODUCTS))
    selected, cash, rows = _evaluate(parameters, payload+b''.join(assets))
    if isinstance(farm['money'], int):
        cash = int(cash)
    records = [dict(item=_PRODUCTS[i], quantity=q, declared_gain=gain, own_gain=own,
                    held_batch_gain=batch, own_other_batch_premium=other,
                    rival_premium=rival, window=window)
               for i, q, gain, own, batch, other, rival, window in rows]
    return dict(zip(_PRODUCTS, selected)), dict(step=start, held=records, space_limit=capacity,
        cash_floor=cash_required, projected_cash_after_sales=cash) if records else None


def sale_caps(observation, farm, private, market, reserves, cash_required, *, incoming_purchase_units=0):
    if PLANNER_BACKEND != 'compiled':
        return _reference_sale_caps(observation, farm, private, market, reserves, cash_required,
            incoming_purchase_units=incoming_purchase_units)
    amounts,payload,capacity=pack_inputs(observation,farm,private,market,reserves,cash_required,incoming_purchase_units)
    if payload is None:
        return dict(zip(_PRODUCTS,amounts)),None
    params=market.get('params') or rules.MARKET_PARAMS
    parameters=tuple((p['base'],p['I0'],p['T'],p['below_target'],p['above_target'],
        p['below_func'],p['above_func']) for p in (params[item] for item in _PRODUCTS))
    selected,cash,rows=_evaluate(parameters,payload)
    if isinstance(farm['money'],int):cash=int(cash)
    records=[dict(item=_PRODUCTS[i],quantity=q,declared_gain=gain,own_gain=own,
        held_batch_gain=batch,own_other_batch_premium=other,rival_premium=rival,window=window)
        for i,q,gain,own,batch,other,rival,window in rows]
    return dict(zip(_PRODUCTS,selected)),dict(step=int(observation['step']),held=records,space_limit=capacity,
        cash_floor=cash_required,projected_cash_after_sales=cash) if records else None


@lru_cache(maxsize=1)
def _prediction_entry():
    module=library()
    module.fv_settlement_prediction_abi_version.argtypes=[]
    module.fv_settlement_prediction_abi_version.restype=ctypes.c_uint32
    if module.fv_settlement_prediction_abi_version()!=1:
        raise RuntimeError('Native sale-prediction ABI version mismatch')
    call=module.fv_sale_caps_prediction
    call.argtypes=[ctypes.POINTER(Parameters),ctypes.c_void_p,ctypes.c_size_t,
                  ctypes.POINTER(SaleResult),ctypes.c_void_p,ctypes.c_size_t]
    call.restype=ctypes.c_int32
    return call


@lru_cache(maxsize=512)
def _evaluate_prediction(parameters,payload,prediction):
    result=SaleResult()
    code=_prediction_entry()(_bundle(parameters),payload,len(payload),ctypes.byref(result),
                             prediction,len(prediction))
    if code:
        raise ValueError(f'Native predicted settlement failed ({code})')
    records=tuple((r.item,r.quantity,r.gain,r.own,r.batch,r.other,r.rival,r.window)
                  for r in result.records[:result.count])
    return tuple(result.amounts),result.cash,records


_PREDICTION_HEADER=struct.Struct('<ii')
_PREDICTION_ROW=struct.Struct('<9I')


def sale_caps_with_prediction(observation,farm,private,market,reserves,cash_required,prediction,*,incoming_purchase_units=0):
    if PLANNER_BACKEND!='compiled':
        raise RuntimeError('Predicted native settlement requires the compiled input packer')
    amounts,payload,capacity=pack_inputs(observation,farm,private,market,reserves,cash_required,incoming_purchase_units)
    if payload is None:
        return dict(zip(_PRODUCTS,amounts)),None
    start=int(observation['step'])
    stop=min(718,prediction['end']-1)
    flow=[[0]*9 for _ in range(stop-start+1)]
    for tick,batch in prediction['flow'].items():
        if not start<=tick<=stop:
            continue
        for item,value in batch.items():
            if item not in _INDEX or not 0<_INDEX[item]<8 or value>0 or int(value)!=value:
                raise ValueError('Sale context requires integer sale-only quantities')
            flow[tick-start][_INDEX[item]]=-int(value)
    forecast=_PREDICTION_HEADER.pack(start,stop)+b''.join(_PREDICTION_ROW.pack(*row) for row in flow)
    params=market.get('params') or rules.MARKET_PARAMS
    parameters=tuple((p['base'],p['I0'],p['T'],p['below_target'],p['above_target'],p['below_func'],p['above_func'])
        for p in (params[item] for item in _PRODUCTS))
    selected,cash,rows=_evaluate_prediction(parameters,payload,forecast)
    if isinstance(farm['money'],int):cash=int(cash)
    records=[dict(item=_PRODUCTS[i],quantity=q,declared_gain=gain,own_gain=own,
        held_batch_gain=batch,own_other_batch_premium=other,rival_premium=rival,window=window)
        for i,q,gain,own,batch,other,rival,window in rows]
    return dict(zip(_PRODUCTS,selected)),dict(step=start,held=records,space_limit=capacity,
        cash_floor=cash_required,projected_cash_after_sales=cash) if records else None
