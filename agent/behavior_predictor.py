"""Small public-state nearest-neighbor forecasts with ordinary-step preparation."""
from dataclasses import replace
from reference_mixture import ReferenceMixture, load_model as load_reference_mixture
from adaptive_sale import AdaptiveSale, load_model as load_adaptive_sale, apply as apply_adaptive_sale
from grain_buy import load_model as load_grain_buy, apply as apply_grain_buy
from relative_grain import load_model as load_relative_grain, apply as apply_relative_grain, observation_key as grain_key
from resource_quantity import load_model as load_resource_quantity, apply as calibrate_resource_quantity, observation_key as resource_quantity_key
from current_day_capacity import harvest_upper, constrain as constrain_mature_quantity
from quantity_calibration import load_model as load_quantity_calibration, apply as calibrate_quantities
from quantity_forecast import project as project_quantities
import gzip
import hashlib
import heapq
import json
import math
import struct
from pathlib import Path
import time

from public_features import snapshot
from phase_features import extra_features
from timing_tools import market_batches
from metric_features import embedding
from behavior_arithmetic import weighted_targets
from land_forecast import load_prior as load_land_prior, apply as project_land
from event_calibration import load_model as load_calibration, apply as calibrate_growth
from asset_forecast import load_bank, project as project_assets
from asset_reference import project as project_reference_assets
from goods_reference_runtime import apply as apply_goods_reference
from cadence_prior import load_model as load_cadence,apply as apply_cadence,observation_key as cadence_key
from compact_goods import load_model as load_compact_goods,apply as apply_compact_goods,observation_key as compact_goods_key
from goods_events import load_model as load_goods_events,apply as apply_goods_events
from funding_goods_events import load_model as load_funding_goods_events,apply as apply_funding_goods_events,observation_key as funding_goods_key
from adaptive_goods_events_runtime import AdaptiveGoodsEvents,load_model as load_adaptive_goods_events,apply as apply_adaptive_goods_events
from adaptive_goods_stock import AdaptiveGoodsStock,load_model as load_adaptive_goods,apply as apply_adaptive_goods
from conversion_forecast import load_model as load_conversion, project as project_conversion
from pressure_forecast import load_model as load_pressure, project as project_pressure, observation_key as pressure_key
from hour6_pressure import load_model as load_pressure6, project as project_pressure6, observation_key as pressure6_key
from inventory_effect import load_model as load_inventory_effect, project as project_inventory_effect
from inventory_limits import upper_limits, constrain_inventory, constrain_trades
from stock_forecast import load_bank as load_stock, project as project_stock, observation_key as stock_key

SCALES = {'assets': 4, 'shops': 1, 'history': 4, 'quadrants': 4, 'maturity': 4,
          'ready': 12, 'stress': 4, 'prices': 1}
GROUPS = ('assets', 'shops', 'history', 'quadrants', 'maturity', 'ready', 'stress', 'prices')


GROUPS = (*GROUPS, *('phase', 'care_bonus', 'fertility', 'competitor', 'liquidity', 'space'))
SCALES.update({'phase': 4, 'care_bonus': 8, 'fertility': 4, 'competitor': 4, 'liquidity': 1, 'space': 1})

def flatten(row, weights):
    values = []
    for key in GROUPS:
        if key not in weights:
            continue
        multiplier = math.sqrt(weights[key]) / SCALES[key]
        if key == 'prices':
            values.extend(math.log1p(v)/math.log(2)*multiplier for v in row[key])
        else:
            values.extend(v*multiplier for v in row[key])
    return tuple(values)


class Predictor:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.manifest = json.loads((self.directory/'MODEL.json').read_bytes())
        assert self.manifest['schema'] == 'public-behavior-metric-v1'
        self.product_weights = self.manifest['product_mixture_weights']
        assert set(self.product_weights)==set(self.manifest['products'])
        assert all(0<=v<=1 for v in self.product_weights.values())
        self.asset_bank = None
        self.stock_bank = None
        self.quantity_calibration = None
        self.resource_quantity_calibration = None
        self.relative_grain = None
        self.grain_buy = None
        self.adaptive_sale = None
        self.adaptive_goods = None
        self.cadence_model = None
        self.compact_goods_model = None
        self.goods_events_model = None
        self.funding_goods_model = None
        self.adaptive_goods_events = None
        self.reference_mixture = None
        self.event_calibration = None
        self.land_prior = None
        self.conversion_model = None
        self.pressure_model = None
        self.hour6_pressure_model = None
        self.inventory_effect_model = None
        self.history = {}
        self.last_step = None
        self.player = None
        self.loaded = {}
        self.memo = {}
        self.stats = dict(loads=0, load_seconds=0., queries=0, query_seconds=0.,
                          cache_hits=0, missing_history=0, ordinary_step_loads=0,
                          asset_query_seconds=0., asset_queries=0,
                          pressure_query_seconds=0., pressure_queries=0,
                          pressure6_query_seconds=0., pressure6_queries=0,
                          inventory_query_seconds=0., inventory_queries=0,
                          observe_seconds=0., blend_seconds=0., peak_blend_seconds=0.)

    def prepare(self, day):
        if day in self.loaded or not 6 <= day < 28:
            return
        started = time.perf_counter()
        if self.asset_bank is None:
            self.asset_bank=load_bank(self.directory,self.manifest['asset_targets'])
        if self.event_calibration is None:
            self.event_calibration=load_calibration(self.directory,self.manifest['event_calibrator'])
        if self.land_prior is None:
            self.land_prior=load_land_prior(self.directory,self.manifest['land_prior'])
        if self.conversion_model is None:
            self.conversion_model=load_conversion(self.directory,self.manifest['conversion_forecast'])
        if self.pressure_model is None:
            self.pressure_model=load_pressure(self.directory,self.manifest['pressure_model'])
        if self.hour6_pressure_model is None:
            self.hour6_pressure_model=load_pressure6(self.directory,self.manifest['hour6_pressure_model'],self.manifest['training_samples'])
        if self.inventory_effect_model is None:
            self.inventory_effect_model=load_inventory_effect(self.directory,self.manifest['inventory_effect_model'],self.manifest['training_samples'])
        if self.stock_bank is None:
            self.stock_bank=load_stock(self.directory,self.manifest['stock_targets'],self.manifest['training_samples'])
        if self.quantity_calibration is None:
            self.quantity_calibration=load_quantity_calibration(self.directory,self.manifest['quantity_calibrator'],self.manifest['training_samples'],self.manifest['stock_targets']['training_order_sha256'])
        if self.resource_quantity_calibration is None:
            self.resource_quantity_calibration=load_resource_quantity(self.directory,self.manifest['resource_quantity_calibrator'])
        if self.relative_grain is None:
            self.relative_grain = load_relative_grain(self.directory, self.manifest['relative_grain'], self.manifest['training_samples'], self.manifest['stock_targets']['training_order_sha256'])
        if self.grain_buy is None:
            self.grain_buy = load_grain_buy(self.directory, self.manifest['grain_buy'])
        if self.adaptive_goods_events is None:
            self.adaptive_goods_events=AdaptiveGoodsEvents(load_adaptive_goods_events(self.directory,self.manifest['adaptive_goods_events']))
        if self.funding_goods_model is None:
            self.funding_goods_model=load_funding_goods_events(self.directory,self.manifest['funding_goods_events'])
        if self.goods_events_model is None:
            self.goods_events_model=load_goods_events(self.directory,self.manifest['goods_events'])
        if self.compact_goods_model is None:
            self.compact_goods_model=load_compact_goods(self.directory,self.manifest['compact_goods'])
        if self.cadence_model is None:
            self.cadence_model=load_cadence(self.directory,self.manifest['cadence_prior'],self.manifest['training_samples'],self.manifest['stock_targets']['training_order_sha256'])
        if self.adaptive_goods is None:
            self.adaptive_goods=AdaptiveGoodsStock(load_adaptive_goods(self.directory,self.manifest['adaptive_goods_stock']))
        if self.adaptive_sale is None:
            self.adaptive_sale = AdaptiveSale(load_adaptive_sale(self.directory,self.manifest['adaptive_sale']))
        if self.reference_mixture is None:
            self.reference_mixture = ReferenceMixture(load_reference_mixture(self.directory,self.manifest['reference_mixture'],self.manifest['training_samples'],self.manifest['stock_targets']['training_order_sha256'],self.relative_grain))
        record = self.manifest['days'][str(day)]
        raw = (self.directory/record['file']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == record['sha256'], 'Predictor data hash mismatch'
        payload = gzip.decompress(raw)
        magic, encoded_day, n, width, horizon = struct.unpack_from('<4s4I', payload)
        assert magic == b'BHC1' and encoded_day == day and n == self.manifest['training_samples']
        assert horizon == 30-day and len(payload) == 20+n*width*8+n*horizon*63*2
        x = struct.unpack_from(f'<{n*width}d', payload, 20)
        y = struct.unpack_from(f'<{n*horizon*63}h', payload, 20+n*width*8)
        data = dict(day=day, vectors=[x[i*width:(i+1)*width] for i in range(n)],
                    targets=[y[i*horizon*63:(i+1)*horizon*63] for i in range(n)])
        self.loaded[day] = data
        self.loaded = {k: v for k, v in self.loaded.items() if k >= day-1}
        self.stats['loads'] += 1
        self.stats['load_seconds'] += time.perf_counter()-started

    def observe(self, observation):
        started = time.perf_counter()
        step = int(observation['step'])
        player = int(observation['player'])
        if self.last_step is None or step < self.last_step or player != self.player:
            self.history.clear()
            if self.adaptive_sale is not None:
                self.adaptive_sale.reset()
            if self.adaptive_goods is not None:
                self.adaptive_goods.reset()
            if self.adaptive_goods_events is not None:
                self.adaptive_goods_events.reset()
            if self.reference_mixture is not None:
                self.reference_mixture.reset()
            self.loaded.clear()
            self.memo.clear()
        self.last_step = step
        self.player = player
        day = step//24
        if step % 24 == 0:
            self.history[day] = snapshot(observation, 1-player)
            self.memo.clear()
        if step % 24 == 0 and 6 <= day < 28:
            self.prepare(day)
            self.adaptive_sale.observe(observation)
            self.reference_mixture.observe(observation)
            self.adaptive_goods.observe(observation)
            self.adaptive_goods_events.observe(observation)
            actual = self.forecast(observation)
            self.adaptive_sale.remember(actual['net_trade_forecast'] if actual is not None else None,day)
            self.reference_mixture.remember(actual['net_trade_quantity'] if actual is not None else None,day)
            self.adaptive_goods.remember(actual['net_trade_quantity'] if actual is not None else None,day)
            self.adaptive_goods_events.remember(actual['net_trade_forecast'] if actual is not None else None,day)
        if step % 24 == 23 and 6 <= day+1 < 28:
            prior = self.stats['loads']
            self.prepare(day+1)
            self.stats['ordinary_step_loads'] += self.stats['loads']-prior
        self.stats['observe_seconds'] += time.perf_counter()-started

    def forecast(self, observation):
        day = int(observation['step'])//24
        if not 6 <= day < 28:
            return None
        lags = [max(0, day-lag) for lag in (1, 3, 6)]
        if 'history' in self.manifest['parameters']['weights'] and any(d not in self.history for d in lags):
            self.stats['missing_history'] += 1
            return None
        current = snapshot(observation, 1-int(observation['player']))
        current.update(extra_features(observation, 1-int(observation['player'])))
        if 'history' in self.manifest['parameters']['weights']:
            current['history'] = [v for d in lags for v in self.history[d]['assets']]
        feature = embedding(current, day, self.manifest, flatten)
        inventory_limits=tuple(tuple(row) for row in upper_limits(observation)) if int(observation['step'])%24==0 else None
        harvest=harvest_upper(observation)
        key = (day, feature, int(observation['step'])%24==0, pressure_key(observation), pressure6_key(observation), inventory_limits, stock_key(observation),harvest,resource_quantity_key(current) if int(observation['step'])%24==0 else None, grain_key(observation), self.adaptive_sale.weights(day) if self.adaptive_sale is not None and int(observation['step'])%24==0 else None, self.reference_mixture.weights(day) if self.reference_mixture is not None and int(observation['step'])%24==0 else None, self.reference_mixture.fertilizer_weights(day) if self.reference_mixture is not None and int(observation['step'])%24==0 else None,self.adaptive_goods.weights(day) if self.adaptive_goods is not None and int(observation['step'])%24==0 else None,cadence_key(observation),compact_goods_key(current,self.history,observation,day),self.adaptive_goods_events.weights(day) if self.adaptive_goods_events is not None and int(observation['step'])%24==0 else None,funding_goods_key(current,self.history,observation,day))
        if key in self.memo:
            self.stats['cache_hits'] += 1
            return self.memo[key]
        self.prepare(day)
        data = self.loaded[day]
        started = time.perf_counter()
        nearest = heapq.nsmallest(self.manifest['parameters']['k'],
            ((round(math.dist(feature, other)**2, 10), i) for i, other in enumerate(data['vectors'])))
        weights = [1/(1+d) for d, i in nearest]
        total = sum(weights)
        weights = [w/total for w in weights]
        selected = [data['targets'][i] for _,i in nearest]
        mixed = weighted_targets(selected, weights)
        assert len(mixed)==(30-day)*63
        forecast = tuple(mixed[(d+1)*63:(d+1)*63+9] for d in range(29-day))
        slots = tuple(tuple(mixed[(d+1)*63+9+b*9:(d+1)*63+18+b*9] for b in range(6))
                      for d in range(29-day))
        value = dict(sales=forecast, slots=slots, current_sales=mixed[:9],
                     current_slots=tuple(mixed[9+b*9:18+b*9] for b in range(6)),neighbors=[i for _, i in nearest],
                     distances=[d for d, _ in nearest], day=day)
        value['asset_forecast']=None
        if int(observation['step'])%24==0:
            asset_started=time.perf_counter()
            value['asset_forecast']=project_assets(self.asset_bank,value['neighbors'],weights,current,day)
            calibrate_growth(self.event_calibration,day,value['distances'],weights,value['asset_forecast'])
            project_land(self.asset_bank,value['neighbors'],weights,current,day,value['asset_forecast'],self.land_prior)
            value['asset_forecast']['same_tile_conversion']=project_conversion(self.conversion_model,self.asset_bank,value['neighbors'],weights,current,day)
            self.stats['asset_query_seconds']+=time.perf_counter()-asset_started
            self.stats['asset_queries']+=1
        value['stock_forecast']=project_stock(self.stock_bank,value['neighbors'],weights,observation,day)
        value['net_trade_quantity']=project_quantities(value['current_slots'],current,value['stock_forecast'],observation)
        calibrate_quantities(self.quantity_calibration,value['net_trade_quantity'],selected,weights,value['neighbors'])
        constrain_mature_quantity(value['net_trade_quantity'],harvest,value['stock_forecast'])
        calibrate_resource_quantity(self.resource_quantity_calibration,value['net_trade_quantity'],selected,weights,current,day)
        apply_relative_grain(self.relative_grain,value['net_trade_quantity'],selected,value['neighbors'],weights,current,observation,day)
        value['net_trade_forecast']=None
        if int(observation['step'])%24==0:
            pressure_started=time.perf_counter()
            value['net_trade_forecast']=project_pressure(self.pressure_model,selected,weights,value['distances'],observation,day,value['stock_forecast'])
            apply_grain_buy(self.grain_buy,value['net_trade_forecast'],value['net_trade_quantity'],self.pressure_model,selected,weights,value['distances'],current,observation,day)
            apply_adaptive_sale(self.adaptive_sale,value['net_trade_forecast'],value['net_trade_quantity'],self.pressure_model,selected,weights,value['distances'],current,observation,day)
            self.stats['pressure_query_seconds']+=time.perf_counter()-pressure_started
            self.stats['pressure_queries']+=int(value['net_trade_forecast'] is not None)
        if int(observation['step'])%24==6:
            pressure_started=time.perf_counter()
            value['net_trade_forecast']=project_pressure6(self.hour6_pressure_model,value['neighbors'],weights,value['distances'],observation,current,day)
            self.stats['pressure6_query_seconds']+=time.perf_counter()-pressure_started
            self.stats['pressure6_queries']+=int(value['net_trade_forecast'] is not None)
        value['inventory_effect_forecast']=None
        if int(observation['step'])%24==0:
            effect_started=time.perf_counter()
            value['inventory_effect_forecast']=project_inventory_effect(self.inventory_effect_model,value['neighbors'],weights,value['distances'],observation,day,value['stock_forecast'])
            self.stats['inventory_query_seconds']+=time.perf_counter()-effect_started
            self.stats['inventory_queries']+=int(value['inventory_effect_forecast'] is not None)
        constrain_inventory(value['inventory_effect_forecast'],observation)
        constrain_trades(value['net_trade_forecast'])
        supplementary = self.reference_mixture.apply(value['net_trade_quantity'],selected,value['neighbors'],value['distances'],current,observation,day,feature)
        project_reference_assets(self.reference_mixture.model,self.asset_bank,value['asset_forecast'],current,day,supplementary)
        apply_goods_reference(self.reference_mixture.model,self.quantity_calibration,value['net_trade_quantity'],selected,value['neighbors'],supplementary,day)
        apply_cadence(self.cadence_model,self.quantity_calibration,value['net_trade_quantity'],selected,value['neighbors'],weights,observation,day)
        apply_adaptive_goods(self.adaptive_goods,value['net_trade_quantity'],value['stock_forecast'],observation,day)
        apply_compact_goods(self.compact_goods_model,value['net_trade_quantity'],value['stock_forecast'],current,self.history,observation,day)
        apply_funding_goods_events(self.funding_goods_model,self.goods_events_model,value['net_trade_forecast'],value['net_trade_quantity'],value['stock_forecast'],current,self.history,observation,day,selected)
        apply_adaptive_goods_events(self.adaptive_goods_events,value['net_trade_forecast'],day)
        if len(self.memo) >= 8:
            self.memo.clear()
        self.memo[key] = value
        self.stats['queries'] += 1
        self.stats['query_seconds'] += time.perf_counter()-started
        return value

    def blend(self, observation, worlds, weight=.35):
        if weight!=.35:raise ValueError("This candidate uses the frozen product-specific mixture")
        product_weights=tuple(self.product_weights[p] for p in self.manifest["products"])
        started = time.perf_counter()
        prediction = self.forecast(observation)
        if prediction is None:
            elapsed = time.perf_counter()-started
            self.stats['blend_seconds'] += elapsed
            self.stats['peak_blend_seconds'] = max(self.stats['peak_blend_seconds'], elapsed)
            return worlds
        day = int(observation['step'])//24
        start = int(observation['step'])
        products=self.manifest['products']
        prices=[observation['market']['prices'][p] for p in products]
        predicted_batches=[]
        resources=set(self.manifest['current_day_products'])
        current_weight=self.manifest['current_day_weight']
        if start%24==0:
            mask=[p in resources for p in products]
            quantities=[q if keep else 0. for q,keep in zip(prediction['current_sales'],mask)]
            slots=[[q if keep else 0. for q,keep in zip(row,mask)] for row in prediction['current_slots']]
            for hour,values in market_batches(slots,quantities,prices,current_weight):
                predicted_batches.append((day*24+hour,tuple((p,q) for p,q in zip(products,values) if q)))
        for offset,quantities in enumerate(prediction['sales']):
            for hour,values in market_batches(prediction['slots'][offset],quantities,prices,product_weights):
                tick=(day+1+offset)*24+hour
                if start<=tick<719:
                    predicted_batches.append((tick,tuple((p,q) for p,q in zip(products,values) if q)))
        result=[]
        for world in worlds:
            flow={tick:dict(batch) for tick,batch in world.external_flow.items()}
            for tick,batch in flow.items():
                if tick//24>day:
                    for product,amount in list(batch.items()):batch[product]=int(round(amount*(1-self.product_weights[product])))
                elif start%24==0 and tick//24==day:
                    for product,amount in list(batch.items()):
                        if product in resources:batch[product]=int(round(amount*(1-current_weight)))
            for tick,values in predicted_batches:
                for product,quantity in values:
                    batch=flow.setdefault(tick,{})
                    batch[product]=batch.get(product,0)+quantity
            result.append(replace(world,external_flow=flow,label=world.label+'/public_behavior_current_resources'))
        elapsed = time.perf_counter()-started
        self.stats['blend_seconds'] += elapsed
        self.stats['peak_blend_seconds'] = max(self.stats['peak_blend_seconds'], elapsed)
        return tuple(result)
