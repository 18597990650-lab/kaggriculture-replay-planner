"""Pack the exact sale ABI directly from rule-owned state with bounded storage."""
from libc.stdint cimport int32_t, uint32_t
from libc.string cimport memcpy
from cpython.bytes cimport PyBytes_FromStringAndSize, PyBytes_AS_STRING

from . import rules

_PRODUCTS=tuple(rules.PRODUCTS)
_INDEX={item:index for index,item in enumerate(_PRODUCTS)}
_ANIMAL_PRODUCT={item:_INDEX[data['product']] for item,data in rules.ANIMALS.items()}
_DISTANCE=tuple(tuple(min(abs(x-a)+abs(y-b) for a,b in ((4,4),(4,5),(5,4),(5,5)))
    for x in range(10)) for y in range(10))

cdef struct State:
    int32_t start
    int32_t end
    uint32_t capacity
    int32_t cargo_origin
    uint32_t cargo_flags
    uint32_t asset_count
    double money
    double cash_floor
    double inventory[9]
    double consumption[9]
    uint32_t amounts[9]
    uint32_t bags[9]
    uint32_t cargo[9]
    uint32_t upper[9]

cdef struct Asset:
    int32_t product
    int32_t age
    int32_t held
    int32_t bonus
    int32_t fertilized
    int32_t watered
    int32_t distance
    int32_t side

if sizeof(State)!=328 or sizeof(Asset)!=32:
    raise RuntimeError('Native sale packet layout mismatch')


def pack_inputs(observation,farm,private,market,reserves,cash_required,incoming_purchase_units):
    cdef State state
    cdef Asset assets[200]
    cdef int i,product,side,x,y,day,capacity,count=0
    cdef uint32_t eligible=0
    cdef object item,q,bag,tile,animal,source,row,shop,products
    cdef object shed=private['shed']
    cdef object amount
    cdef object occupied=0
    cdef object reserved=0
    cdef object other_stock=0
    cdef list amounts=[]
    cdef bytes payload
    cdef char* target
    state.start=int(observation['step'])
    state.end=min(718,state.start+144)
    for i in range(9):
        item=_PRODUCTS[i]
        amount=max(0,shed.get(item,0)-reserves.get(item,0))
        state.amounts[i]=amount
        state.bags[i]=0
        amounts.append(amount)
        if amount and 0<i<8:eligible|=1<<i
    for bag in private['inventories']:
        for item,q in bag.items():
            occupied+=q
            product=_INDEX.get(item,-1)
            if product>=0:state.bags[product]+=q
    for q in reserves.values():reserved+=q
    for item,q in shed.items():
        if item not in _INDEX:other_stock+=q
    capacity=max(0,100-occupied-reserved-other_stock-incoming_purchase_units)
    if eligible==0 or state.end<=state.start or capacity==0:
        return tuple(amounts),None,capacity
    state.capacity=capacity
    state.money=farm['money']
    state.cash_floor=cash_required
    cargo=observation.get('_public_rival_cargo')
    quantities=cargo['quantities'] if cargo else {}
    upper=cargo.get('uncertain_upper',{}) if cargo else {}
    state.cargo_origin=cargo['origin'] if cargo else 0
    state.cargo_flags=(int(bool(quantities))|(int(bool(quantities or upper))<<1)) if cargo else 0
    for i in range(9):
        item=_PRODUCTS[i]
        state.inventory[i]=market['inventory'][item]
        state.consumption[i]=0.
        state.cargo[i]=quantities.get(item,0)
        state.upper[i]=upper.get(item,0)
    for shop in observation['town']['unlocked_shops']:
        products=rules.SHOPS[shop]
        for item in products:
            state.consumption[_INDEX[item]]+=2. if len(products)==1 else 1.
    for item,q in observation['town'].get('_forecast_shop_consumption',{}).items():
        product=_INDEX.get(item,-1)
        if product>=0:state.consumption[product]+=q
    day=state.start//24
    for side,source in enumerate((farm,observation['farms'][1-int(observation['player'])])):
        for y,row in enumerate(source['tiles']):
            for x,tile in enumerate(row):
                if not isinstance(tile,dict):continue
                animal=tile.get('animal')
                product=_ANIMAL_PRODUCT[animal] if animal else _INDEX.get(tile.get('crop'),-1)
                if product<0 or not (eligible&(1<<product)):continue
                if count>=200:raise ValueError('Sale packet exceeds two legal farm boards')
                assets[count].product=product
                assets[count].age=day-tile['placed_day' if animal else 'planted_day']
                assets[count].held=tile.get('yield_units',0)
                assets[count].bonus=tile.get('pending_care_bonus',0) if animal else 0
                assets[count].fertilized=0 if animal else max(-1,tile.get('fertilized_until_day',-1)-day)
                assets[count].watered=int(bool(tile.get('watered_today'))) if not animal else 0
                assets[count].distance=_DISTANCE[y][x]
                assets[count].side=side
                count+=1
    state.asset_count=count
    payload=PyBytes_FromStringAndSize(NULL,sizeof(State)+count*sizeof(Asset))
    target=PyBytes_AS_STRING(payload)
    memcpy(target,&state,sizeof(State))
    memcpy(target+sizeof(State),assets,count*sizeof(Asset))
    return tuple(amounts),payload,capacity
