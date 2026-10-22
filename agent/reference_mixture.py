"""Mix a small supplementary wheat route bank using completed public trades."""
import gzip
import hashlib
import heapq
import json
import math
from asset_reference import validate as validate_assets
from relative_grain import apply as apply_grain, observation_key
from resource_quantity import apply as apply_resource


def load_model(directory, record, training_count, training_order, grain_model):
    packed = (directory/record['file']).read_bytes()
    if hashlib.sha256(packed).hexdigest() != record['sha256']:
        raise ValueError('Supplementary reference hash mismatch')
    model = json.loads(gzip.decompress(packed))
    if model['schema'] != 'public-resource-reference-mixture-v2':
        raise ValueError('Unsupported supplementary reference schema')
    if model['base_training_count'] != training_count or model['training_order_sha256'] != training_order:
        raise ValueError('Supplementary references use a different base bank')
    count = model['added_count']
    width = model['embedding_width']
    if count != 63 or set(model['days']) != {str(day) for day in range(6, 28)}:
        raise ValueError('Supplementary reference dimensions differ')
    coefficients=model['fertilizer_coefficient']
    if len(coefficients)!=3 or any(len(row)!=12 or not all(math.isfinite(value) for value in row) for row in coefficients):
        raise ValueError('Invalid supplementary fertilizer coefficients')
    model['fertilizer_model']=dict(coefficient=[[[0.]*12,row] for row in coefficients])
    for data in model['days'].values():
        if len(data['vectors']) != count or len(data['trades']) != count:
            raise ValueError('Supplementary day dimensions differ')
        if any(len(row) != width or not all(math.isfinite(value) for value in row) for row in data['vectors']):
            raise ValueError('Invalid supplementary embeddings')
        if any(len(row) != 6 or not all(math.isfinite(value) for value in row) for row in data['trades']):
            raise ValueError('Invalid supplementary wheat trades')
        fertilizer=data['fertilizer_trades']
        if len(fertilizer)!=count or any(len(row)!=6 or not all(math.isfinite(value) for value in row) for row in fertilizer):
            raise ValueError('Invalid supplementary fertilizer trades')
        goods=data['goods_trades']
        if model['goods_reference_weight']!=.25 or len(goods)!=count or any(len(slots)!=6 or any(len(row)!=7 or any(not math.isfinite(value) or value<0 for value in row) for row in slots) for slots in goods):
            raise ValueError('Invalid supplementary goods trades')
        targets = []
        for slots,fert_slots,goods_slots in zip(data['trades'],fertilizer,goods):
            row = [0.]*63
            row[0] = sum(slots)
            for si, value in enumerate(slots):
                row[9+si*9] = value
            row[8]=sum(fert_slots)
            for si,value in enumerate(fert_slots):
                row[17+si*9]=value
            for pi in range(1,8):
                row[pi]=sum(values[pi-1] for values in goods_slots)
                for si,values in enumerate(goods_slots):row[9+si*9+pi]=values[pi-1]
            targets.append(tuple(row))
        data['targets'] = targets
    bank = model['grain_bank']
    if len(bank) != count*30*2 or any(not isinstance(value, int) or value < 0 for value in bank):
        raise ValueError('Invalid supplementary grain bank')
    if model['prior'] != .25 or model['history_days'] != 3 or model['loss_scale'] != 6:
        raise ValueError('Supplementary mixture update contract differs')
    model['expanded_grain'] = dict(grain_model, bank_values=grain_model['bank_values']+tuple(bank))
    validate_assets(model)
    return model


class ReferenceMixture:
    def __init__(self, model):
        self.model = model
        self.reset()

    def reset(self):
        self.day = None
        self.losses = []
        self.pending = None
        self.updates = 0
        self.fertilizer_losses=[]
        self.fertilizer_pending=None
        self.fertilizer_updates=0

    def observe(self, observation):
        step = int(observation['step'])
        day = step//24
        if step%24 or self.day == day:
            return
        if self.day is not None and day < self.day:
            raise ValueError('Reset reference mixture before starting another game')
        flow = observation.get('_public_rival_cargo', {}).get('resource_flow')
        eligible = bool(flow and flow.get('origin') == step and flow.get('started_at_zero')
                        and flow.get('window_steps') == 24 and flow['observed_steps_last24'][0] == 24)
        windows = flow.get('completed_wheat_windows') if eligible else None
        if self.pending is not None and self.pending['day'] == day-1 and windows is not None:
            if len(windows) != 3:
                raise ValueError('Reference mixture requires4/8/24 completed windows')
            old, new = self.pending['old'], self.pending['new']
            gains = tuple((abs(old[hi]-target)-abs(new[hi]-target))/self.model['loss_scale'] for hi, target in enumerate(windows))
            self.losses.append((day-1, gains))
            self.updates += 1
        self.losses = [(when, value) for when, value in self.losses if when >= day-self.model['history_days']]
        self.pending = None
        fert_eligible=bool(flow and flow.get('origin')==step and flow.get('started_at_zero') and flow.get('window_steps')==24 and flow['observed_steps_last24'][1]==24)
        fert_windows=flow.get('completed_fertilizer_windows') if fert_eligible else None
        if self.fertilizer_pending is not None and self.fertilizer_pending['day']==day-1 and fert_windows is not None:
            if len(fert_windows)!=3:
                raise ValueError('Fertilizer mixture requires4/8/24 completed windows')
            old,new=self.fertilizer_pending['old'],self.fertilizer_pending['new']
            gains=tuple((abs(old[hi]-value)-abs(new[hi]-value))/self.model['loss_scale'] for hi,value in enumerate(fert_windows))
            self.fertilizer_losses.append((day-1,gains))
            self.fertilizer_updates+=1
        self.fertilizer_losses=[(when,value) for when,value in self.fertilizer_losses if when>=day-self.model['history_days']]
        self.fertilizer_pending=None
        self.day = day

    def weights(self, day):
        prior = math.log(self.model['prior']/(1-self.model['prior']))
        values = []
        for hi in range(3):
            odds = prior+sum(gain[hi] for when, gain in self.losses if day-self.model['history_days'] <= when < day)
            values.append(1/(1+math.exp(-max(-40., min(40., odds)))))
        return tuple(values)

    def fertilizer_weights(self,day):
        prior=math.log(self.model['prior']/(1-self.model['prior']))
        values=[]
        for hi in range(3):
            odds=prior+sum(gain[hi] for when,gain in self.fertilizer_losses if day-self.model['history_days']<=when<day)
            values.append(1/(1+math.exp(-max(-40.,min(40.,odds)))))
        return tuple(values)

    def remember(self, quantity, day):
        if self.day != day:
            raise ValueError('Observe actual dawn before registering reference predictions')
        if quantity is not None and quantity.get('reference_mixture_status') == 'applied':
            experts = quantity['reference_mixture_experts']
            self.pending = dict(day=day, old=tuple(experts['old']), new=tuple(experts['new']))
        if quantity is not None and quantity.get('reference_fertilizer_status')=='applied':
            experts=quantity['reference_fertilizer_experts']
            self.fertilizer_pending=dict(day=day,old=tuple(experts['old']),new=tuple(experts['new']))

    def state(self):
        return dict(day=self.day, losses=self.losses, pending=self.pending, updates=self.updates, fertilizer_losses=self.fertilizer_losses, fertilizer_pending=self.fertilizer_pending, fertilizer_updates=self.fertilizer_updates)

    def apply(self, quantity, rows, neighbors, distances, public, observation, day, feature):
        if quantity is None or int(observation['step'])%24:
            return
        if observation_key(observation) is None or quantity.get('relative_grain_status') != 'applied':
            quantity['reference_mixture_status'] = 'unavailable_public_grain'
            quantity['reference_fertilizer_status']='unavailable_public_grain'
            return
        if not len(rows) == len(neighbors) == len(distances) == 8:
            raise ValueError('Supplementary references require the original eight nearest routes')
        if len(feature) != self.model['embedding_width']:
            raise ValueError('Supplementary query embedding width differs')
        data = self.model['days'][str(day)]
        base_count = self.model['base_training_count']
        candidates = list(zip(distances, neighbors))
        candidates.extend((round(math.dist(feature, other)**2, 10), base_count+i) for i, other in enumerate(data['vectors']))
        nearest = heapq.nsmallest(8, candidates)
        weights = [1/(1+distance) for distance, _ in nearest]
        total = sum(weights)
        weights = [weight/total for weight in weights]
        chosen = [index for _, index in nearest]
        original = quantity['point_estimate']
        old = tuple(row[0] for row in original)
        added = sum(index >= base_count for index in chosen)
        if added:
            lookup = dict(zip(neighbors, rows))
            selected = [lookup[index] if index < base_count else data['targets'][index-base_count] for index in chosen]
            mean = [list(row) for row in quantity['raw_neighbor_mean']]
            for hi, slots in enumerate((1, 2, 6)):
                mean[hi][0] = sum(weight*sum(row[9+slot*9] for slot in range(slots)) for row, weight in zip(selected, weights))
            temporary = dict(point_estimate=original, raw_neighbor_mean=mean)
            apply_grain(self.model['expanded_grain'], temporary, selected, chosen, weights, public, observation, day)
            new = tuple(row[0] for row in temporary['point_estimate'])
        else:
            new = old
        mixing = self.weights(day)
        output = [list(row) for row in original]
        for hi in range(3):
            output[hi][0] = (1-mixing[hi])*old[hi]+mixing[hi]*new[hi]
        quantity['point_estimate'] = tuple(tuple(row) for row in output)
        quantity['reference_mixture_status'] = 'applied'
        quantity['reference_mixture_experts'] = dict(old=old, new=new)
        quantity['reference_mixture_weights'] = mixing
        quantity['reference_mixture_added_neighbors'] = added
        quantity['reference_mixture_boundary'] = 'Only wheat net quantities change. Original retrieval and other heads stay fixed. Mixture updates require complete public prior-day transactions; hypothetical forecasts do not learn.'
        old_fertilizer=tuple(row[8] for row in original)
        if added:
            for hi,slots in enumerate((1,2,6)):
                mean[hi][8]=sum(weight*sum(row[17+slot*9] for slot in range(slots)) for row,weight in zip(selected,weights))
            temporary=dict(point_estimate=original,raw_neighbor_mean=mean)
            apply_resource(self.model['fertilizer_model'],temporary,selected,weights,public,day)
            new_fertilizer=tuple(row[8] for row in temporary['point_estimate'])
        else:
            new_fertilizer=old_fertilizer
        fert_mixing=self.fertilizer_weights(day)
        for hi in range(3):
            output[hi][8]=old_fertilizer[hi]+fert_mixing[hi]*(new_fertilizer[hi]-old_fertilizer[hi])
        quantity['point_estimate']=tuple(tuple(row) for row in output)
        quantity['reference_fertilizer_status']='applied'
        quantity['reference_fertilizer_experts']=dict(old=old_fertilizer,new=new_fertilizer)
        quantity['reference_fertilizer_weights']=fert_mixing
        quantity['reference_fertilizer_boundary']='Only fertilizer net quantities change; reuse wheat supplementary neighbors. Feedback is restricted to complete publicly identified past days.'
        return tuple(chosen), tuple(weights)
