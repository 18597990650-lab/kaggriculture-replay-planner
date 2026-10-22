"""Admit crop allocation and delay revisions using complete paired native tails."""
from dataclasses import asdict,replace
import time

from .available_crop_admission import cash_value,quote_summary
from .task_allocation import allocation_variants,delay_variants,apply_revision,QuotePrices,Revision
from .programmes import ProductionProgramme
from .recipe import compile_recipe
from . import rules,quote_timing
from .available_crop_plan import available_crop_variants
from .expanded_crop_plan import with_output_deliveries
from .redeployment import redeployment_variants
from .macro_projects import project_variants
from .composition import compose_service
from .candidate_frontier import successor_screens, distinct_first, smaller_cohorts
from .reference_settlement import ReferenceSettlement


def _available_group(programme,observation,controller):
    """Keep the incumbent two-site family alongside general replacement lots."""
    tick=int(observation['step'])
    proposals=[]
    for item in ('MELON','TOMATO','STRAWBERRY','CARROT'):
        for raw in available_crop_variants(programme,observation,controller,crop=item,sizes=(2,)):
            candidate,meta=with_output_deliveries(programme,raw,observation)
            lots=[dict(**p,lot=f'{tick}:{p["point"][0]}:{p["point"][1]}:{p["item"]}') for p in meta['added_plantings']]
            identities={(tuple(p['point']),p['item'],p['generation']):p['lot'] for p in lots}
            days=[]
            for actors in candidate.days:
                days.append({a:tuple(replace(t,lot=identities[(t.point,t.item,t.generation)])
                    if (t.point,t.item,t.generation) in identities and t.operation in ('PLANT','WATER','FERTILIZE','HARVEST')
                    else t for t in seq) for a,seq in actors.items()})
            tagged=ProductionProgramme(candidate.name+'-group-lots',tuple(days),candidate.workers)
            priority=len(lots)*(rules.CROPS[item]['max_yield']*observation['market']['prices'][item]-rules.CROPS[item]['seed'])
            detail=dict(item=item,point=lots[0]['point'],points=[p['point'] for p in lots],lots=lots,
                generation=tick//24,mode='existing_labor_group',harvest_timing='incumbent_group_calendar',
                added_plantings=meta['added_plantings'],added_deposits=meta['added_deposits'],
                marginal_wages=0,estimate=priority,removed_tasks=[],work=[],paid_old_crop=None,
                price_basis='Incumbent family priority only; full native settlement determines admission')
            proposals.append(Revision(tagged,detail))
    return max(proposals,key=lambda r:(r.detail['estimate'],r.detail['item']),default=None)


def revision_patch(parent,revision):
    data=asdict(revision.programme)
    return dict(parent=parent.name,name=revision.programme.name,
        days={str(d):actors for d,actors in enumerate(data['days']) if revision.programme.days[d]!=parent.days[d]},
        workers=data['workers'],detail=revision.detail)


def restore_revision(parent,patch):
    if parent.name!=patch['parent']:
        raise ValueError('A task revision belongs to its quoted parent')
    data=asdict(parent)
    data['days']=list(data['days'])
    data['name']=patch['name']
    for day,actors in patch['days'].items():
        data['days'][int(day)]=actors
    data['workers']=patch['workers']
    # from_dict validates external archives, including internal clearance identities.
    data['days']=[{str(a):seq for a,seq in actors.items()} for actors in data['days']]
    return Revision(ProductionProgramme.from_dict(data),patch['detail'])


def _project_screens(projects):
    """Retain family coverage and distinct retirement scales before full quotes."""
    heads=[]
    families={}
    for revision in projects:
        detail=revision.detail
        family=(detail['mode'],detail['item'],tuple(sorted({r['item'] for r in detail.get('retired_animals',())})))
        if family not in families:
            heads.append(revision)
            families[family]=[]
        families[family].append(revision)
    # Start each leading herd family at its smallest funded exposure. Larger
    # cohorts can lose more existing production even with higher gross receipts.
    selected=[]
    for head in heads[:2]:
        detail=head.detail
        family=(detail['mode'],detail['item'],tuple(sorted({r['item'] for r in detail.get('retired_animals',())})))
        group=families[family]
        selected.append(min(group,key=lambda r:(len(r.detail['points']),-r.detail['estimate']))
                        if detail['mode']=='inherited_herd_project' else head)
    names={r.programme.name for r in selected}
    for group in families.values():
        if not any(r.programme.name in names for r in group) or group[0].detail['mode']!='inherited_herd_project':
            continue
        sizes = sorted({len(r.detail['points']) for r in group})
        midpoint = min(sizes, key=lambda size: (abs(2*size-sizes[0]-sizes[-1]), size))
        scale_order = tuple(dict.fromkeys((midpoint, sizes[-1], *sizes)))
        ordered = sorted(group,key=lambda r:(-r.detail['estimate'],r.programme.name))
        # Compare small, intermediate and large investments before filling
        # additional layouts at a size already represented in the frontier.
        for size in scale_order:
            representative = next((r for r in ordered
                if len(r.detail['points']) == size and r.programme.name not in names), None)
            if representative is not None:
                selected.append(representative)
                names.add(representative.programme.name)
        for revision in ordered:
            if revision.programme.name not in names:
                selected.append(revision)
                names.add(revision.programme.name)
    selected.extend(r for r in projects if r.programme.name not in names)
    return tuple(selected)


def _unproduced_lots(compiled, revision):
    """Identify promised production absent from the executed coarse continuation."""
    lots={row['lot'] for row in revision.detail.get('lots',())}
    if revision.detail.get('lot'):
        lots.add(revision.detail['lot'])
    harvested=compiled.result.get('lot_harvest',{})
    return tuple(sorted(lot for lot in lots if harvested.get(lot,0)<=0))


def _comparison(compiled,baseline,observation,revision):
    new=quote_summary(compiled.result,observation)
    more=new['critical']-baseline['critical']
    earlier={str(p):step for p,step in new['missing'].items() if step<baseline['missing'].get(p,720)}
    detail=revision.detail
    declared=[]
    farm=observation['farms'][int(observation['player'])]
    for old in detail.get('retired_animals',()):
        p=tuple(old['point'])
        tile=farm['tiles'][p[1]][p[0]]
        if not isinstance(tile,dict) or (tile.get('animal'),tile.get('placed_day'))!=(old['item'],old['generation']):
            raise ValueError('Retirement quote does not match the paid animal')
        step=new['missing'].get(p,720)
        if step>=old['release_day']*24:
            earlier.pop(str(p),None)
            declared.append(dict(point=p,step=step,item=old['item']))
    activations=[]
    for lot in detail.get('added_plantings',()):
        x,y=lot['point']
        binding=compiled.result.get('lot_bindings',{}).get(lot.get('lot'))
        bound_required=bool(lot.get('lot','').startswith('redeploy:') and 'lot_bindings' in compiled.result)
        active=any(isinstance(s['farm']['tiles'][y][x],dict) and
                   s['farm']['tiles'][y][x].get('crop')==lot['item'] and
                   (s['farm']['tiles'][y][x].get('planted_day',-1)==binding[2] if bound_required and binding else
                    not bound_required and s['farm']['tiles'][y][x].get('planted_day',-1)>=lot['generation'])
                   for s in compiled.result['day_states'])
        activations.append(active)
    lost_new=[]
    for lot in detail.get('added_herds',()):
        x,y=lot['point']
        born=None
        for state in compiled.result['day_states']:
            tile=state['farm']['tiles'][y][x]
            same=isinstance(tile,dict) and tile.get('animal')==lot['item'] and tile.get('placed_day',-1)>=lot['generation']
            if same and born is None:
                born=tile['placed_day']
            if born is not None and (not same or tile['placed_day']!=born) and state['step']<lot['end']*24:
                lost_new.append(dict(lot=lot['lot'],step=state['step']))
                break
        activations.append(born is not None)
    lot_ids=[p['lot'] for p in detail.get('lots',())] or ([detail['lot']] if detail.get('lot') else [])
    harvested_by_lot={lot:compiled.result.get('lot_harvest',{}).get(lot,0) for lot in lot_ids}
    harvested=sum(harvested_by_lot.values())
    changed=not activations or all(activations) and harvested_by_lot and all(harvested_by_lot.values())
    row=dict(world=1,own=new['own'],rival=new['rival'],relative=new['relative'],
        value_basis=new['value_basis'],raw_own=new['raw_own'],raw_rival=new['raw_rival'],
        own_delta=new['own']-baseline['own'],relative_delta=new['relative']-baseline['relative'],
        additional_critical=dict(more),earlier_paid_animal_loss=earlier,
        declared_paid_retirements=declared,earlier_new_animal_loss=lost_new,
        actual_new_plantings=sum(activations[:len(detail.get('added_plantings',()))]),
        actual_new_herds=sum(activations[len(detail.get('added_plantings',())):]),
        lot_harvest=harvested,harvested_by_lot=harvested_by_lot)
    reasons=[]
    if not changed: reasons.append('NEW_PRODUCTION_NOT_COMPLETED')
    if row['own_delta']<0: reasons.append('OWN_CASH_DECREASE')
    if row['relative_delta']<=0: reasons.append('RELATIVE_VALUE_NOT_IMPROVED')
    if more: reasons.append('ADDITIONAL_CRITICAL_RESOURCE_FAILURE')
    if earlier: reasons.append('UNDECLARED_PAID_ANIMAL_LOSS')
    if lost_new: reasons.append('PREMATURE_NEW_ANIMAL_LOSS')
    if detail.get('kind')=='productive_redeployment':
        from .redeployment import delivery_uncertainty_buffer
        buffer=delivery_uncertainty_buffer(observation,compiled.result,detail)
        row['delivery_uncertainty_allowance']=buffer
        if row['own_delta']<buffer or row['relative_delta']<=buffer:
            reasons.append('DELIVERY_ALLOWANCE_NOT_COVERED')
    row['rejection_reasons']=reasons
    row['admitted']=not reasons
    if 'economy' in new and 'economy' in baseline:
        before,after=baseline['economy'],new['economy']
        delta={key:{item:after[key].get(item,0)-before[key].get(item,0)
                    for item in sorted(set(before[key])|set(after[key]))
                    if after[key].get(item,0)!=before[key].get(item,0)}
               for key in ('product_market','harvest','sales_net','purchases_net')}
        delta['outside_product_market']=after['outside_product_market']-before['outside_product_market']
        # Fixed product trades include their price impact on existing output;
        # the remaining cash change includes labor, animals, land and seeds.
        delta['cash_sum']=sum(delta['product_market'].values())+delta['outside_product_market']
        row['economic_delta']=delta
    return row


def extend(observation,controller,programme,adaptive,world_indices,worlds,baseline,quote,deadline,
           estimated_full,*,baseline_compiled,optional_seconds=0.0):
    """Keep the complete narrow branch eligible before widening its first layer."""
    begin=time.perf_counter()
    end=deadline+optional_seconds
    cache={(programme.name,adaptive,id(worlds[1]),True):(programme,baseline_compiled)}
    cache_hits=0
    completed_quotes=0
    def cached(candidate,mode,world,settlement):
        nonlocal cache_hits,completed_quotes
        key=(candidate.name,mode,id(world),settlement) if isinstance(settlement,bool) else None
        if key is not None and key in cache:
            previous,result=cache[key]
            if previous.days!=candidate.days or previous.workers!=candidate.workers:
                raise ValueError('A quote identity refers to different work plans')
            cache_hits+=1
            return result
        result=quote(candidate,mode,world,settlement)
        if settlement is True:
            completed_quotes+=1
        if key is not None:
            cache[key]=(candidate,result)
        return result

    def prepare_unseen(programmes, mode, world, settlement):
        prepare = getattr(quote, 'prefetch', None)
        if not callable(prepare):
            return False
        pending = [candidate for candidate in programmes
            if (candidate.name, mode, id(world), settlement) not in cache]
        return prepare(pending, mode, world, settlement)
    def ready_cached(candidate, mode, world, settlement):
        if (candidate.name, mode, id(world), settlement) in cache:
            return True
        ready = getattr(quote, 'ready', None)
        return callable(ready) and ready(candidate, mode, world, settlement)
    cached.prefetch = prepare_unseen
    cached.ready = ready_cached

    protected=_extend_branch(observation,controller,programme,adaptive,world_indices,worlds,
        baseline,cached,deadline,estimated_full,baseline_compiled=baseline_compiled,
        optional_seconds=optional_seconds,incumbent_limits=(4,2))
    selected,record,rows=protected
    value=rows[0]['relative'] if rows else baseline[1]['relative']
    branches=[dict(kind='protected',selected=selected.name,score=value,
                   changed=record['changed'],seconds=time.perf_counter()-begin)]
    protected_score=value
    protected_name=selected.name
    cost=quote_timing.estimate(observation['step'],True)
    remaining=end-time.perf_counter()
    if remaining>=4*cost*1.25+.4:
        # A wider first layer must leave time to price its successor as well.
        reserve=max(2*cost*1.25+.15,remaining*.4)
        wide_start=time.perf_counter()
        wider=_extend_branch(observation,controller,programme,adaptive,world_indices,worlds,
            baseline,cached,end-reserve,cost,baseline_compiled=baseline_compiled,
            optional_seconds=reserve)
        candidate,detail,quoted=wider
        candidate_value=quoted[0]['relative'] if quoted else baseline[1]['relative']
        branches.append(dict(kind='wider',selected=candidate.name,score=candidate_value,
            changed=detail['changed'],seconds=time.perf_counter()-wide_start))
        if candidate_value>value:
            selected,record,rows=wider
            value=candidate_value
    record=dict(record,allocation_branches=branches,protected_score=protected_score,
                protected_selected=protected_name,quote_cache_hits=cache_hits,
                quotes=completed_quotes,
                seconds=time.perf_counter()-begin,
                redeployment_seconds=max(0.0,time.perf_counter()-deadline))
    return selected,record,rows


def _extend_branch(observation,controller,programme,adaptive,world_indices,worlds,baseline,quote,deadline,
           estimated_full,*,baseline_compiled,optional_seconds=0.0,incumbent_limits=None):
    """Complete an allocation and its successors within one absolute window."""
    begin=time.perf_counter()
    completed={programme.name:baseline_compiled}
    def remember(candidate,mode,world,settlement):
        nonlocal estimated_full
        started=time.perf_counter()
        result=quote(candidate,mode,world,settlement)
        if settlement is True and world is worlds[1]:
            completed[candidate.name]=result
            estimated_full=quote_timing.estimate(observation['step'],True)
        return result
    remember.prefetch = getattr(quote, 'prefetch', None)
    remember.ready = getattr(quote, 'ready', None)
    parent,original,original_rows=_incumbent_allocation(observation,controller,programme,
        adaptive,world_indices,worlds,baseline,remember,deadline,estimated_full,
        baseline_compiled=baseline_compiled,limits=incumbent_limits)
    optional_start=time.perf_counter()
    unused_core_seconds=max(0.0,deadline-optional_start)
    deadline=deadline+optional_seconds
    search=dict(parent=parent.name,proposals=[],screen=[],results=[],quotes=0,changed=False)
    search['reused_core_seconds']=unused_core_seconds
    original['redeployment_search']=search
    def retain(reason):
        search['reason']=reason
        original['seconds']=time.perf_counter()-begin
        duration=time.perf_counter()-optional_start
        search['elapsed_seconds']=duration
        original['redeployment_seconds']=max(0.0,duration-unused_core_seconds)
        return parent,original,original_rows
    if time.perf_counter()+estimated_full*1.15+.2>deadline:
        return retain('No complete successor quote fits after incumbent allocation')
    base=completed[parent.name]
    reference=quote_summary(base.result,observation)
    screen_settlement=ReferenceSettlement(base)
    price=QuotePrices(observation,base)
    projects=project_variants(parent,observation,controller,
        price=price,day_states=base.result['day_states'])
    services=[r for r in projects if r.detail.get('mode')=='guarded_cycle_service']
    composed=[]
    for revision in projects:
        for service in services:
            merged=compose_service(parent,revision,service,controller)
            if merged is not None:
                composed.append(merged)
    # A funded one-quote decision must be able to buy the complete compatible
    # portfolio. Standalone plans remain available as alternatives.
    projects=tuple(sorted((*composed,*projects),key=lambda r:(-r.detail['estimate'],r.programme.name)))
    successors=redeployment_variants(parent,observation,controller,
        price=price,day_states=base.result['day_states'],limit=3)
    variants=(*projects,*successors)
    search['proposals']=[r.detail for r in variants]
    if not variants:
        return retain('No positive occupied-site successor in the biological shortlist')
    screen_cost=quote_timing.estimate(observation['step'],screen_settlement)
    screened=[]
    screen_cash={}
    screen_missing={}
    # Compare complete project families on their executed tails before spending
    # the full settlement budget. Gross product estimates omit displaced sales.
    screening=successor_screens(_project_screens(projects),successors)
    search['screening_frontier']=[r.programme.name for r in screening]
    # Protect two complete decisions before spending spare time on more coarse
    # candidates; a long screen list must not consume the second full quote.
    # Compare the leading project scales and the distinct local alternative
    # before reserving a second settlement of an already sampled family.
    minimum_screens=min(4,len(screening))
    full_slots=2 if time.perf_counter()+minimum_screens*screen_cost+2*estimated_full*1.15+.08<=deadline else 1
    search['reserved_full_quotes']=full_slots
    pending=list(screening)
    search['scale_followups']=[]
    while pending:
        revision=pending.pop(0)
        if len(screened)>=minimum_screens and full_slots==1:
            if time.perf_counter()+2*estimated_full*1.15+.08<=deadline:
                full_slots=2
        if time.perf_counter()+screen_cost+full_slots*estimated_full*1.15+.08>deadline:
            if (full_slots==2 and len(screened)<minimum_screens
                    and time.perf_counter()+screen_cost+estimated_full*1.15+.08<=deadline):
                # A revised cost estimate may consume the intended fourth
                # reference. Preserve one complete decision and compare the
                # missing family before a second settlement of earlier ones.
                full_slots=1
                search['coverage_reservation_revised']=True
                search['reserved_full_quotes']=full_slots
            else:
                break
        before=time.perf_counter()
        result=remember(revision.programme,adaptive,worlds[1],screen_settlement)
        screen_cost=quote_timing.estimate(observation['step'],screen_settlement)
        own,rival=cash_value(result.result)
        screened.append((own-rival,revision))
        screen_cash[revision.programme.name]=own
        screen_missing[revision.programme.name]=_unproduced_lots(result,revision)
        if screen_missing[revision.programme.name]:
            pending,followups=smaller_cohorts(revision,pending)
            if followups:
                search['scale_followups'].append(dict(failed=revision.programme.name,
                    unproduced_lots=screen_missing[revision.programme.name],prioritized=followups))
    search['screen']=[dict(programme=r.programme.name,own=screen_cash[r.programme.name],relative=value,
        production_complete=not screen_missing[r.programme.name],
        settlement_basis='completed_parent_storage_with_candidate_funding_and_capacity',
        unproduced_lots=screen_missing[r.programme.name]) for value,r in screened]
    if screened:
        # The complete decision maximizes relative cash subject to an own-cash
        # floor. Screen in that same order; income-first ordering must not
        # consume the only full quote on a different optimization objective.
        # Missing production keeps a later opportunity because full settlement
        # can change funding and execution.
        complete=[row for row in screened if not screen_missing[row[1].programme.name]]
        incomplete=[row for row in screened if screen_missing[row[1].programme.name]]
        ranked=[]
        for group in (complete,incomplete):
            if not group:
                continue
            ranked.extend(r for value,r in sorted(group,
                key=lambda row:(-row[0],-screen_cash[row[1].programme.name],row[1].programme.name)))
    else:
        ranked=(*screening,*variants)
    selected=distinct_first(ranked)
    winner=None
    for position, revision in enumerate(selected):
        marginal_reference=None
        if winner is not None:
            combined=compose_service(parent,winner[0],revision,controller)
            if combined is not None:
                marginal_reference=quote_summary(completed[winner[0].programme.name].result,observation)
                revision=combined
        ready = remember.ready
        prepared = callable(ready) and ready(revision.programme, adaptive, worlds[1], True)
        if not prepared and time.perf_counter()+estimated_full*1.15+.08>deadline:
            break
        prepare = remember.prefetch
        if (not prepared and callable(prepare) and position+1 < len(selected)
                and time.perf_counter()+2*estimated_full*1.25+.20 <= deadline):
            following = selected[position+1]
            if (compose_service(parent, revision, following, controller) is None
                    and (winner is None or compose_service(parent, winner[0], following, controller) is None)):
                prepare([revision.programme, following.programme], adaptive, worlds[1], True)
        before=time.perf_counter()
        result=remember(revision.programme,adaptive,worlds[1],True)
        estimated_full=quote_timing.estimate(observation['step'],True)
        row=_comparison(result,reference,observation,revision)
        if marginal_reference is not None:
            marginal=_comparison(result,marginal_reference,observation,revision)
            row['composition_comparison']=dict(programme=winner[0].programme.name,
                own_delta=marginal['own_delta'],relative_delta=marginal['relative_delta'],
                admitted=marginal['admitted'],rejection_reasons=marginal['rejection_reasons'])
            if not marginal['admitted']:
                row['admitted']=False
                row['rejection_reasons'].append('COMPOSITION_DOES_NOT_IMPROVE_COMPLETED_WINNER')
        row.update(programme=revision.programme.name,comparison_programme=parent.name,
            completed_before_deadline=time.perf_counter()<=deadline)
        row['admitted']=bool(row['admitted'] and row['completed_before_deadline'])
        if not row['completed_before_deadline']:
            row['rejection_reasons'].append('INCOMPLETE_WITHIN_BUDGET')
        search['results'].append(row)
        search['quotes']+=1
        if row['admitted'] and (winner is None or row['relative']>winner[1]['relative']):
            winner=revision,row
    if winner is None:
        original['quotes']+=search['quotes']
        return retain('Retain completed incumbent; no quoted successor improves its full tail'
            if search['quotes'] else 'Retain completed incumbent; no complete successor quote fits')
    revision,row=winner
    search.update(changed=True,reason='Successor improves the complete incumbent expansion')
    duration=time.perf_counter()-optional_start
    search['elapsed_seconds']=duration
    record=dict(original)
    record.update(revision.detail)
    record.update(kind='task_allocation',changed=True,selected=revision.programme.name,
        quotes=original['quotes']+search['quotes'],seconds=time.perf_counter()-begin,
        redeployment_seconds=max(0.0,duration-unused_core_seconds),
        incumbent_expansion=original,redeployment_search=search,
        revision=revision_patch(programme,revision),results=original['results']+search['results'])
    return revision.programme,record,[row]


def _incumbent_allocation(observation,controller,programme,adaptive,world_indices,worlds,baseline,quote,deadline,
           estimated_full,*,baseline_compiled,limits=None):
    """Use the remaining search budget for a physically complete new task lot."""
    if world_indices!=[1]:
        raise ValueError('Task allocation uses the declared fixed primary decision world')
    begin=time.perf_counter()
    record=dict(kind='task_allocation',old_winner=programme.name,changed=False,quotes=0,selected=None,
        results=[],proposals=[],remaining_seconds=deadline-begin)
    if begin+estimated_full*1.15+.4>deadline:
        record.update(reason='Insufficient reserved time for a complete allocation comparison',seconds=0)
        return programme,record,None
    price=QuotePrices(observation,baseline_compiled)
    variants=[]
    seen=set()
    for timing in ('full_yield','calendar_end'):
        for revision in allocation_variants(programme,observation,controller,price=price,
                day_states=baseline_compiled.result['day_states'],limit=4,timing=timing):
            signature=tuple((day,actor,sequence) for day,actors in enumerate(revision.programme.days)
                            for actor,sequence in sorted(actors.items())),revision.programme.workers
            if signature not in seen:
                seen.add(signature)
                variants.append(revision)
    variants.sort(key=lambda r:(-r.detail['estimate'],r.programme.name))
    incumbent_group=_available_group(programme,observation,controller)
    proposals=([incumbent_group] if incumbent_group is not None else [])+variants
    record['proposals']=[dict(programme=r.programme.name,item=r.detail['item'],point=r.detail['point'],
        mode=r.detail['mode'],estimate=r.detail['estimate'],wages=r.detail['marginal_wages']) for r in proposals]
    if not proposals:
        record.update(reason='No complete crop replacement or insertion fits the horizon',seconds=time.perf_counter()-begin)
        return programme,record,None
    winner=None
    def evaluate(revision):
        nonlocal estimated_full,winner
        ready = getattr(quote, 'ready', None)
        prepared = callable(ready) and ready(revision.programme, adaptive, worlds[1], True)
        if not prepared and time.perf_counter()+estimated_full*1.15+.08>deadline:
            return False
        before=time.perf_counter()
        compiled=quote(revision.programme,adaptive,worlds[1],True)
        estimated_full=quote_timing.estimate(observation['step'],True)
        row=_comparison(compiled,baseline[1],observation,revision)
        row['programme']=revision.programme.name
        row['completed_before_deadline']=time.perf_counter()<=deadline
        row['admitted']=bool(row['admitted'] and row['completed_before_deadline'])
        if not row['completed_before_deadline']:
            row['rejection_reasons'].append('INCOMPLETE_WITHIN_BUDGET')
        record['results'].append(row)
        record['quotes']+=1
        if row['admitted'] and (winner is None or row['relative']>winner[1]['relative']):
            winner=(revision,row)
        return True
    # Preserve a complete quote for the incumbent family before screening new lots.
    if incumbent_group is not None:
        evaluate(incumbent_group)
    # A cheap native screen prices actual worker queues before assigning a full quote.
    screened=[]
    screen_cost=quote_timing.estimate(observation['step'],False)
    for revision in (variants[:limits[0]] if limits else variants):
        if time.perf_counter()+screen_cost+2*estimated_full*1.25+.10>deadline:
            break
        before=time.perf_counter()
        compiled=quote(revision.programme,adaptive,worlds[1],False)
        screen_cost=quote_timing.estimate(observation['step'],False)
        own,rival=cash_value(compiled.result)
        screened.append((own-rival,revision))
    if not screened:
        selected=variants[:1]
    else:
        selected=[revision for score,revision in sorted(screened,key=lambda row:(-row[0],row[1].programme.name))]
        if limits:
            selected=selected[:limits[1]]
    record['screen']=[dict(programme=r.programme.name,relative=score) for score,r in screened]
    for position, revision in enumerate(selected):
        prepare = getattr(quote, 'prefetch', None)
        if (callable(prepare) and position % 2 == 0 and position+1 < len(selected)
                and time.perf_counter()+2*estimated_full*1.25+.20 <= deadline):
            prepare([r.programme for r in selected[position:position+2]], adaptive, worlds[1], True)
        if not evaluate(revision):
            break
    record['seconds']=time.perf_counter()-begin
    record['changed']=winner is not None
    record['reason']='Complete replacement/insertion accepted' if winner else 'No completed allocation improves both own cash and relative value'
    if winner is None:
        return programme,record,None
    revision,row=winner
    record.update(selected=revision.programme.name,revision=revision_patch(programme,revision),**revision.detail)
    record['kind']='task_allocation'
    return revision.programme,record,[row]


def repair_allocation(observation,configuration,controller,world,seconds):
    """Revalue a delayed execution without resetting any actor's paid prefix."""
    begin=time.perf_counter()
    deadline=begin+seconds
    estimate=quote_timing.estimate(observation['step'],True)
    record=dict(kind='TASK_ALLOCATION_REPAIR',step=int(observation['step']),changed=False,results=[])
    if 2*estimate+.15>seconds:
        record.update(reason='No complete paired repair fits the remaining time',seconds=0)
        return controller,record
    def quote(c):
        start=time.perf_counter()
        result=compile_recipe(observation,configuration,c.programme,world,continuation=c,
            settlement=True,bind_herd=c.bind_herd,adaptive_crops=c.adaptive_crops)
        quote_timing.observe(observation['step'],True,time.perf_counter()-start)
        return result
    before=time.perf_counter()
    compiled=quote(controller)
    estimate=quote_timing.estimate(observation['step'],True)
    baseline=quote_summary(compiled.result,observation)
    variants=delay_variants(controller.programme,observation,controller,price=QuotePrices(observation,compiled))
    # Give one paid transfer and the lowest-loss feasible cancellation a comparison.
    transfers=[r for r in variants if r.detail['kind']=='paid_labor_transfer']
    cancellations=[r for r in variants if r.detail['kind']=='delay_cancellation']
    retimed=[r for r in variants if r.detail['kind']=='actual_maturation_retime']
    selected=[*retimed[:1],*cancellations[:1],*transfers[:1]]
    record['proposals']=[r.detail for r in variants]
    winner=None
    for revision in selected:
        if time.perf_counter()+estimate*1.1+.08>deadline:
            break
        candidate=apply_revision(controller,revision)
        before=time.perf_counter()
        result=quote(candidate)
        estimate=quote_timing.estimate(observation['step'],True)
        row=_comparison(result,baseline,observation,revision)
        row['programme']=revision.programme.name
        row['admitted']=row['admitted'] and time.perf_counter()<=deadline
        record['results'].append(row)
        if row['admitted'] and (winner is None or row['relative']>winner[1]['relative']):
            winner=(revision,row,candidate)
    if winner is not None:
        revision,row,candidate=winner
        record.update(changed=True,revision=revision_patch(controller.programme,revision),selected=candidate.programme.name)
    record['seconds']=time.perf_counter()-begin
    record['reason']='Complete paid continuation selected' if winner else 'Keep the current paid continuation'
    return (winner[2] if winner else controller),record
