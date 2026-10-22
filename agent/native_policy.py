"""Paid production continuations with warehouse-aware settlement."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import time

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from top1_planner.programmes import ProductionProgramme
from top1_planner.recipe import RecipeCompiler
from top1_planner.worlds import public_worlds
from top1_planner.search import choose_programme
from top1_planner.task_allocation import allocation_event
from top1_planner.allocation_admission import repair_allocation
from top1_planner.late_allocation import reallocate_current
from top1_planner.corrected_supply import DualSupplyBelief
from opponent_market.source_consumption import SourceConsumption
from top1_planner.families import family_bases
from top1_planner.zone_transfer import second_wave_variants
from top1_planner.paid_exit import paid_herd_continuations
from top1_planner.planning_budget import remaining as remaining_budget,optional_charge,decision_window
from top1_planner import quote_timing
from top1_planner.terminal_projects import close_day
from top1_planner.opening_work import optimize_opening_work
from top1_planner.survival_admission import SurvivalAdmission
from top1_planner.biological_cache import ForecastPreparation
from top1_planner.native_sale import _BACKEND as _SALE_BACKEND

_BASES=[ProductionProgramme.from_dict(json.loads(path.read_bytes()))
        for path in sorted((HERE/'programmes').glob('boey02-*.json'))]
_FAMILIES=json.loads((HERE/'programmes/FAMILIES.json').read_bytes())
_SECOND_WAVE_CONTEXTS=json.loads((HERE/'programmes/SECOND_WAVE_CONTEXTS.json').read_bytes())
_SUPPLY=None
_CONSUMPTION=None
_CURRENT=None
_CONTROLLER=None
_TRACE=[]
_PREVIOUS_INPUT=None
_PREVIOUS_ACTION=None
_QUOTED_DAY=None
_PLANNING_ALLOWANCE=42.0
_START_OVERAGE=None
_USED_OVERAGE=0.0
_OPTIONAL_ALLOWANCE=18.0
_START_OPTIONAL=0.0
_USED_OPTIONAL=0.0
_REPAIR_USED=0.0
_REPAIR_DAY=None
_SURVIVAL_ALLOWANCE=0.0
_START_SURVIVAL=0.0
_USED_SURVIVAL=0.0
_FORECAST_PREPARATION=None
observe_public_prediction=None
reset_public_prediction=None
ordinary_service_response=None

def get_trace_records():
    return _TRACE


def _remaining(observation,started,base,optional_duration=0.0):
    elapsed=time.perf_counter()-started
    pending=max(0.0,elapsed-base)
    pending_optional=optional_charge(elapsed=elapsed,base=base,optional_duration=optional_duration,
        optional_remaining=max(0.0,_START_OPTIONAL-_USED_OPTIONAL))
    return remaining_budget(core_limit=_START_OVERAGE,optional_limit=_START_OPTIONAL,
        spent=max(0.0,_USED_OVERAGE-_USED_SURVIVAL)+pending,optional_spent=_USED_OPTIONAL+pending_optional,
        framework_remaining=float(observation['remainingOverageTime']) if 'remainingOverageTime' in observation else None)

def agent(observation,configuration):
    global _SUPPLY,_CONSUMPTION,_CURRENT,_CONTROLLER,_PREVIOUS_INPUT,_PREVIOUS_ACTION,_QUOTED_DAY,_START_OVERAGE,_USED_OVERAGE
    global _REPAIR_USED,_REPAIR_DAY
    global _START_OPTIONAL,_USED_OPTIONAL
    global _START_SURVIVAL,_USED_SURVIVAL
    global _FORECAST_PREPARATION
    obs=dict(observation)
    tick=obs.get('step')
    if tick is None:
        tick=int(obs['day'])*24+int(obs['hour'])
        obs['step']=tick
    if obs==_PREVIOUS_INPUT:
        return deepcopy(_PREVIOUS_ACTION)
    raw_obs=deepcopy(obs)
    started=time.perf_counter()
    optional_duration=0.0
    reset = tick==0 or _CONTROLLER is None or (_PREVIOUS_INPUT is not None and (tick < _PREVIOUS_INPUT["step"] or int(obs["player"]) != int(_PREVIOUS_INPUT["player"])))
    if reset:
        quote_timing.reset()
        if not callable(observe_public_prediction) or not callable(reset_public_prediction):
            raise RuntimeError("P110 public-history callbacks are required")
        reset_public_prediction()
        _SUPPLY=DualSupplyBelief(configuration)
        _CONSUMPTION=SourceConsumption(HERE/'rival_sources/public_v5.py',dict(configuration))
        if tick!=0:
            _CONSUMPTION.active=False
            _CONSUMPTION.hypothesis=None
            _CONSUMPTION.rejection=dict(step=tick,reason='Missing continuous opening after reset')
        _CURRENT=_BASES[0]
        _CONTROLLER=RecipeCompiler(_CURRENT,dict(configuration),settlement=True,bind_herd=True)
        _TRACE.clear()
        _QUOTED_DAY=None
        _START_OVERAGE=min(_PLANNING_ALLOWANCE,float(obs['remainingOverageTime'])) if 'remainingOverageTime' in obs else _PLANNING_ALLOWANCE
        _USED_OVERAGE=0.0
        available=float(obs['remainingOverageTime']) if 'remainingOverageTime' in obs else _PLANNING_ALLOWANCE+_OPTIONAL_ALLOWANCE+_SURVIVAL_ALLOWANCE
        _START_OPTIONAL=min(_OPTIONAL_ALLOWANCE,max(0.0,available-_START_OVERAGE))
        _USED_OPTIONAL=0.0
        _START_SURVIVAL=min(_SURVIVAL_ALLOWANCE,max(0.0,available-_START_OVERAGE-_START_OPTIONAL))
        _USED_SURVIVAL=0.0
        _REPAIR_USED=0.0
        _REPAIR_DAY=None
        _FORECAST_PREPARATION=ForecastPreparation() if _SALE_BACKEND == 'python' else None
    obs['_public_rival_cargo']=_SUPPLY.observe(obs)
    observe_public_prediction(obs)
    _CONSUMPTION.observe(obs)
    day=tick//24
    if tick%24==0 and tick<120:
        _CONTROLLER,opening=optimize_opening_work(obs,dict(configuration),_CONTROLLER,
            max(0.0,min(.65,float(configuration['actTimeout'])-.20-(time.perf_counter()-started))))
        _CURRENT=_CONTROLLER.programme
        if opening is not None:
            _TRACE.append(opening)
    if tick%72==0 and tick>=72 and day<=24 and _QUOTED_DAY!=day:
        _QUOTED_DAY=day
        remaining,optional_remaining,total_remaining=_remaining(obs,started,float(configuration['actTimeout']))
        window=decision_window(step=tick,total_remaining=total_remaining,
            free_seconds=max(0.0,float(configuration['actTimeout'])-(time.perf_counter()-started)-.15),
            quote_seconds=quote_timing.estimate(tick),projects=tick>=216)
        limit,optional_limit=window['core_seconds'],window['optional_seconds']
        previous=_CURRENT.name
        candidates=[]
        regional_candidates=()
        scope='unpaid_changes_within_current_production_tours'
        family=None
        if tick in (72,144):
            family,candidates=family_bases(obs,_BASES,_FAMILIES)
            scope='public_shop_family_before_expansion_payment'
            _CURRENT=candidates[0]
            _CONTROLLER=RecipeCompiler(_CURRENT,dict(configuration),settlement=True,bind_herd=True)
        if tick==216:
            regional_candidates=second_wave_variants(_CURRENT,obs,_BASES,_SECOND_WAVE_CONTEXTS)
            scope='unpaid_sw_region_with_paid_site_work_and_calendar_continuation'
        retention_candidates=paid_herd_continuations(_CURRENT,obs)
        selected,record=choose_programme(obs,dict(configuration),_CONTROLLER,candidates,public_worlds(obs,future_model='uniform_mean'),limit,
            extra_variants=(*regional_candidates,*retention_candidates),redeployment_seconds=optional_limit)
        optional_duration=record['redeployment_seconds']
        record['paid_retention_proposals']=[p.name for p in retention_candidates]
        record['regional_proposals']=[p.name for p in regional_candidates]
        record['proposal_scope']=scope
        record['public_shop_family']=family
        record['whole_demonstration_switch_after_expansion_allowed']=False
        record['decision_window']=window
        selected_mode=record['selected_adaptive_crops']
        if selected != _CURRENT or selected_mode != _CONTROLLER.adaptive_crops:
            if selected_mode==_CONTROLLER.adaptive_crops and _CONTROLLER.can_continue(selected):
                _CONTROLLER=_CONTROLLER.fork(programme=selected,settlement=True)
            else:
                _CONTROLLER=RecipeCompiler(selected,dict(configuration),settlement=True,bind_herd=True,adaptive_crops=selected_mode)
            _CURRENT=selected
        _TRACE.append(dict(step=tick,kind='GENERATED_PRODUCTION_SELECTION',previous=previous,
            selected=_CURRENT.name,**record,effective_overage_before=remaining,allocated_seconds=limit,
            optional_allocated_seconds=optional_limit,optional_overage_before=optional_remaining,
            budget_source='framework_and_internal' if 'remainingOverageTime' in obs else 'internal_allowance_only',
            evidence='Screen values are not admission values. Switching compares expected fixed-trade cash minus declared rival market receipts. Feasible settlement tapes are repriced over the uniform future-shop distribution; this is not adaptive-policy expected profit or a solvency guarantee in every future. Existing visible rival assets follow declared service hypotheses; new rival investments are unknown.'))
    if tick%24==0 and 25<=day<=27:
        _,_,total_remaining=_remaining(obs,started,float(configuration['actTimeout']),optional_duration)
        window=decision_window(step=tick,total_remaining=total_remaining,
            free_seconds=max(0.0,float(configuration['actTimeout'])-(time.perf_counter()-started)-.15),
            quote_seconds=quote_timing.estimate(tick))
        budget=window['total_seconds']
        _CONTROLLER,allocation=reallocate_current(obs,dict(configuration),_CONTROLLER,
            public_worlds(obs,future_model='uniform_mean')[1],budget)
        _CURRENT=_CONTROLLER.programme
        allocation['decision_window']=window
        _TRACE.append(allocation)
    if tick==696:
        _,_,total_remaining=_remaining(obs,started,float(configuration['actTimeout']),optional_duration)
        window=decision_window(step=tick,total_remaining=total_remaining,
            free_seconds=max(0.0,float(configuration['actTimeout'])-(time.perf_counter()-started)-.15),
            quote_seconds=quote_timing.estimate(tick))
        budget=window['total_seconds']
        _CONTROLLER,terminal_plan=close_day(obs,dict(configuration),_CONTROLLER,
            public_worlds(obs,future_model='uniform_mean')[1],budget)
        _CURRENT=_CONTROLLER.programme
        terminal_plan['decision_window']=window
        _TRACE.append(terminal_plan)
    if _REPAIR_DAY!=day and _REPAIR_USED<7.0:
        event=allocation_event(obs,_CONTROLLER)
        if event is not None:
            _REPAIR_DAY=day
            remaining,_,_=_remaining(obs,started,float(configuration['actTimeout']),optional_duration)
            budget=min(7.0-_REPAIR_USED,max(0.0,remaining-1.0)+float(configuration['actTimeout']))
            _CONTROLLER,repair=repair_allocation(obs,dict(configuration),_CONTROLLER,
                public_worlds(obs,future_model='uniform_mean')[1],budget)
            _REPAIR_USED+=repair['seconds']
            _CURRENT=_CONTROLLER.programme
            repair['event']=event
            _TRACE.append(repair)
    if callable(ordinary_service_response):
        _,_,shared_remaining=_remaining(obs,started,float(configuration['actTimeout']),optional_duration)
        _CONTROLLER,deferred=ordinary_service_response(obs,dict(configuration),_CONTROLLER,
            free_seconds=max(0.,float(configuration['actTimeout'])-(time.perf_counter()-started)-.15),
            shared_remaining=shared_remaining,worlds=public_worlds)
        _CURRENT=_CONTROLLER.programme
        if deferred is not None:
            _TRACE.append(deferred)
    before_dispatch=len(_CONTROLLER.dispatch_records)
    before_deadline=len(_CONTROLLER.deadline_records)
    before_terminal=len(_CONTROLLER.terminal_records)
    before=len(_CONTROLLER.settlement_records)
    base_deadline=started+float(configuration['actTimeout'])-.10
    extra=max(0.0,_START_SURVIVAL-_USED_SURVIVAL)
    if 'remainingOverageTime' in obs:
        extra=min(extra,max(0.0,float(obs['remainingOverageTime'])-max(0.0,time.perf_counter()-started-float(configuration['actTimeout']))))
    admission=SurvivalAdmission(obs,dict(configuration),_CONTROLLER,base_deadline+extra)
    _CONTROLLER.survival_admission=admission
    try:
        action=_CONTROLLER.action(obs)
    finally:
        _CONTROLLER.survival_admission=None
    if len(_CONTROLLER.settlement_records)>before:
        if _TRACE and _TRACE[-1]['step']==tick:
            _TRACE[-1]['settlement']=_CONTROLLER.settlement_records[-1]
        else:
            _TRACE.append(dict(kind='WAREHOUSE_WAIT_ALLOCATION',**_CONTROLLER.settlement_records[-1]))
    terminal=_CONTROLLER.terminal_records[before_terminal:]
    if terminal or tick==718:
        detail=dict(actor_changes=terminal,last_market=tick==718)
        if _TRACE and _TRACE[-1]['step']==tick:
            _TRACE[-1]['terminal_delivery']=detail
        else:
            _TRACE.append(dict(step=tick,kind='TERMINAL_DELIVERY',terminal_delivery=detail))
    dispatch=_CONTROLLER.dispatch_records[before_dispatch:]
    if dispatch:
        _TRACE.append(dict(step=tick,kind='PAID_WORK_DISPATCH',transfers=dispatch))
    deadline_changes=_CONTROLLER.deadline_records[before_deadline:]
    if deadline_changes:
        if _TRACE and _TRACE[-1]['step']==tick:
            _TRACE[-1]['deadline_reorders']=deadline_changes
        else:
            _TRACE.append(dict(step=tick,kind='CROP_DEADLINE_REORDER',deadline_reorders=deadline_changes))
    sale_deadline=min(started+float(configuration['actTimeout'])-0.05,time.perf_counter()+0.80)
    action,consumption=_CONSUMPTION.apply(obs,dict(configuration),action,_CONTROLLER,_SUPPLY,deadline=sale_deadline)
    if consumption is not None:
        if _TRACE and _TRACE[-1]['step']==tick:
            _TRACE[-1]['consumption_wait']=consumption
        else:
            _TRACE.append(dict(step=tick,kind='SOURCE_CONSUMPTION_WAIT',consumption_wait=consumption))
    _SUPPLY.remember_action(action)
    if tick%24==0:
        if _TRACE and _TRACE[-1]['step']==tick:
            _TRACE[-1]['public_supply']=obs['_public_rival_cargo']
        else:
            _TRACE.append(dict(kind='PUBLIC_SUPPLY_ESTIMATE',step=tick,public_supply=obs['_public_rival_cargo']))
    _PREVIOUS_INPUT,_PREVIOUS_ACTION=raw_obs,deepcopy(action)
    if _FORECAST_PREPARATION is not None:
        preparation = _FORECAST_PREPARATION.advance(obs,
            min(started+float(configuration['actTimeout'])-.15, time.perf_counter()+.20))
        if preparation is not None and preparation['evaluated']:
            _TRACE.append(preparation)
    elapsed=time.perf_counter()-started
    optional_spent=optional_charge(elapsed=elapsed,base=float(configuration['actTimeout']),
        optional_duration=optional_duration,optional_remaining=max(0.0,_START_OPTIONAL-_USED_OPTIONAL))
    survival_spent=min(max(0.0,elapsed-float(configuration['actTimeout'])-optional_spent),
        sum(event['seconds'] for event in admission.events),max(0.0,_START_SURVIVAL-_USED_SURVIVAL))
    _USED_OPTIONAL+=optional_spent
    _USED_SURVIVAL+=survival_spent
    _USED_OVERAGE+=max(0.0,elapsed-float(configuration['actTimeout']))
    if tick==718:
        _TRACE.append(dict(step=tick,kind='PLANNING_ALLOWANCE_LEDGER',
            core_limit=_START_OVERAGE,optional_limit=_START_OPTIONAL,survival_limit=_START_SURVIVAL,
            core_spent=_USED_OVERAGE-_USED_OPTIONAL-_USED_SURVIVAL,optional_spent=_USED_OPTIONAL,
            survival_spent=_USED_SURVIVAL,
            total_spent=_USED_OVERAGE,
            allocation_policy='shared_total_with_soft_core_optional_targets',
            core_borrowed=max(0.0,_USED_OVERAGE-_USED_OPTIONAL-_USED_SURVIVAL-_START_OVERAGE),
            evidence='Agent-internal overage ledger. Runner call timings remain the runtime certification source.'))
    if admission.events:
        if _TRACE and _TRACE[-1]['step']==tick:
            _TRACE[-1]['survival_admissions']=admission.events
        else:
            _TRACE.append(dict(step=tick,kind='SURVIVAL_ADMISSION_BATCH',survival_admissions=admission.events))
    return action
