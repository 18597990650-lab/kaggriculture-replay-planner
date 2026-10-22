"""Screen complete work plans, then compare their actual settlement policies."""
import time

from .recipe import compile_recipe
from .variants import unpaid_herd_variants, unpaid_crop_variants
from .available_crop_admission import cash_value,quote_summary
from .allocation_admission import extend
from .redeployment import managed
from . import quote_timing


def choose_programme(observation,configuration,controller,bases,worlds,seconds,*,extra_variants=(),redeployment_seconds=0.0):
    started=time.perf_counter()
    current=controller.programme
    remaining_steps=719-int(observation['step'])
    estimated_full=quote_timing.estimate(observation['step'],True)
    estimated_screen=quote_timing.estimate(observation['step'],False)
    candidates={}
    coarse=[]
    equivalent_proposals=[]
    complete=[]
    baseline_primary=None
    complete_summaries={}
    complete_compiled={}
    reserved_full_challengers=1

    def key(programme,adaptive):
        return programme.name+('/crop-calendar' if adaptive else '/ordered')

    current_key=key(current,controller.adaptive_crops)

    def elapsed():
        return time.perf_counter()-started

    def retain(reason,generated_count=0):
        return current,dict(screen=coarse,options=[],generated_count=generated_count,redeployment_seconds=0.0,
            equivalent_proposals=equivalent_proposals,
            search_completed=False,reason=reason,seconds=elapsed(),
            unpaired_incumbent_primary=baseline_primary,
            selected_adaptive_crops=controller.adaptive_crops)

    def quote(programme,adaptive,world,settlement):
        continuation=None
        if controller.can_continue(programme) and adaptive==controller.adaptive_crops:
            continuation=controller.fork(programme=programme,settlement=settlement)
        before=time.perf_counter()
        result=compile_recipe(observation,configuration,programme,world,continuation=continuation,
            settlement=settlement,bind_herd=controller.bind_herd,adaptive_crops=adaptive)
        quote_timing.observe(observation['step'],settlement,time.perf_counter()-before)
        return result

    def screen(programme,adaptive):
        nonlocal estimated_screen
        label=key(programme,adaptive)
        if label in candidates:
            return True
        for canonical,(existing,mode) in candidates.items():
            if (mode==adaptive and existing.days==programme.days
                    and existing.workers==programme.workers):
                equivalent_proposals.append(dict(duplicate=label,canonical=canonical))
                return True
        if elapsed()+estimated_screen+reserved_full_challengers*estimated_full*1.15>seconds:
            return False
        before=time.perf_counter()
        result=quote(programme,adaptive,worlds[1],False)
        estimated_screen=quote_timing.estimate(observation['step'],False)
        own,rival=cash_value(result.result)
        candidates[label]=(programme,adaptive)
        coarse.append(dict(name=label,programme=programme.name,adaptive_crops=adaptive,
            score=own-rival,
            kind='screen_only_immediate_settlement'))
        return True

    if estimated_full*1.15+.08>seconds:
        return retain('Reserved budget cannot fund a measured incumbent quote')
    before=time.perf_counter()
    baseline_result=quote(current,controller.adaptive_crops,worlds[1],True)
    estimated_full=quote_timing.estimate(observation['step'],True)
    baseline_summary=quote_summary(baseline_result.result,observation)
    baseline_primary=(baseline_summary['own'],baseline_summary['rival'])
    complete_summaries[(current_key,1)]=baseline_summary
    complete_compiled[current_key]=baseline_result
    # Four coarse evaluations provide some proposal coverage before reserving
    # two complete alternatives. Shorter budgets retain a single challenger.
    if elapsed()+2*estimated_full*1.15+4*estimated_screen<=seconds:
        reserved_full_challengers=2
    parent,parent_mode=current,controller.adaptive_crops
    alternative_bases=[programme for programme in bases if programme!=current]
    # Coarse parent selection is needed only for distinct whole-programme
    # families. A regional investment competes with other current-plan changes.
    if alternative_bases:
        screen(current,controller.adaptive_crops)
        for programme in alternative_bases:
            if not screen(programme,controller.adaptive_crops):
                break
        if coarse:
            leader=max(coarse,key=lambda row:row['score'])
            parent,parent_mode=candidates[leader['name']]
    if not any(managed(t) for actors in parent.days for sequence in actors.values() for t in sequence):
        screen(parent,not parent_mode)
    variants=(*unpaid_herd_variants(parent,observation),*extra_variants,
              *unpaid_crop_variants(parent,observation,sizes=(2,4,8)))
    generated_count=len(variants)+1
    for programme in variants:
        if not screen(programme,parent_mode):
            break
    challengers=sorted((row for row in coarse if row['name']!=current_key),
                       key=lambda row:row['score'],reverse=True)
    candidates[current_key]=(current,controller.adaptive_crops)
    def complete_record(label,own,rival):
        programme,adaptive=candidates[label]
        return dict(name=label,programme=programme.name,adaptive_crops=adaptive,
            terminal_cash=[own],rival_market_receipts=[rival],score=own-rival,
            generated=any(marker in label for marker in ('-unpaid-','-retain-')))

    complete=[complete_record(current_key,*baseline_primary)]
    parent_rows=list(complete)
    quoted_parents=set()
    branch_records=[]
    expansions={}
    total_seconds=seconds+redeployment_seconds

    def evaluate_parent(challenger,deadline):
        nonlocal estimated_full
        if elapsed()+estimated_full*1.25+.10>deadline:
            return False
        label=challenger['name']
        programme,adaptive=candidates[label]
        result=quote(programme,adaptive,worlds[1],True)
        quoted_parents.add(label)
        estimated_full=quote_timing.estimate(observation['step'],True)
        if elapsed()>deadline:
            return False
        summary=quote_summary(result.result,observation)
        row=complete_record(label,summary['own'],summary['rival'])
        complete.append(row)
        parent_rows.append(row)
        complete_summaries[(label,1)]=summary
        complete_compiled[label]=result
        return True

    def expand_parent(parent,deadline):
        nonlocal estimated_full
        label=parent['name']
        programme,adaptive=candidates[label]
        core_end=min(deadline,max(elapsed(),seconds))
        expanded,detail,quoted=extend(observation,controller,programme,adaptive,[1],worlds,
            {1:complete_summaries[(label,1)]},quote,started+core_end,estimated_full,
            baseline_compiled=complete_compiled[label],optional_seconds=max(0.0,deadline-core_end))
        estimated_full=quote_timing.estimate(observation['step'],True)
        detail['primary_admitted']=detail['changed']
        if detail['changed']:
            expanded_key=key(expanded,adaptive)
            candidates[expanded_key]=(expanded,adaptive)
            complete.append(dict(name=expanded_key,programme=expanded.name,adaptive_crops=adaptive,
                terminal_cash=[r['own'] for r in quoted],rival_market_receipts=[r['rival'] for r in quoted],
                score=min(r['relative'] for r in quoted),generated=True))
            expansions[expanded_key]=detail
        else:
            expansions[label]=detail
        branch_records.append(dict(parent=programme.name,changed=detail['changed'],
            selected=expanded.name,quotes=detail['quotes'],seconds=detail['seconds'],
            score=(min(r['relative'] for r in quoted) if detail['changed'] else parent['score'])))
        return detail

    # Complete a useful anchor before increasing parent breadth. Its funded
    # allocation remains eligible when another bare parent scores higher.
    allocation_reserve=min(seconds*.35,3*estimated_full*1.25+.15)
    initial_deadline=max(elapsed(),seconds-allocation_reserve)
    for challenger in challengers[:2]:
        if not evaluate_parent(challenger,initial_deadline):
            break
    anchor=max(parent_rows,key=lambda row:(row['score'],row['name']==current_key))
    pending=[row for row in challengers if row['name'] not in quoted_parents]
    # Additional parent breadth must not shorten the complete protected chain.
    # The allocation scheduler returns early when its branches are exhausted.
    anchor_detail=expand_parent(anchor,total_seconds)

    # Extra parents have a separate opportunity after the complete anchor.
    # Protect enough time to also evaluate the leading new parent's allocation.
    successor_reserve=(min(max(0.0,total_seconds-elapsed())*.40,
                          4*estimated_full+.15) if pending else 0.0)
    parent_deadline=max(elapsed(),total_seconds-successor_reserve)
    for challenger in pending:
        if not evaluate_parent(challenger,parent_deadline):
            break
    alternative_parents=sorted((row for row in parent_rows if row['name']!=anchor['name']),
        key=lambda row:(row['score'],row['name']==current_key),reverse=True)
    for parent in alternative_parents:
        if elapsed()+2.5*estimated_full+.25>total_seconds:
            break
        expand_parent(parent,total_seconds)
    main_challengers_quoted=len(parent_rows)-1
    world_indices=[1]
    # The decision world is fixed before allocating computation. Additional
    # pressure scenarios remain offline diagnostics, not a time-dependent
    # change to the meaning of a candidate score.
    eligible=complete
    winner=max(eligible,key=lambda row:(row['score'],row['name']==current_key))
    expansion=dict(expansions.get(winner['name'],anchor_detail))
    expansion['changed']=winner['name'] in expansions and expansions[winner['name']]['changed']
    expansion['final_world_indices']=world_indices
    programme,adaptive=candidates[winner['name']]
    return programme,dict(screen=coarse,options=complete,generated_count=generated_count,expansion=expansion,
        redeployment_seconds=max(0.0,elapsed()-seconds),branch_comparisons=branch_records,
        equivalent_proposals=equivalent_proposals,
        search_completed=len(complete)>1,world_indices=world_indices,world_labels=[worlds[i].label for i in world_indices],
        scenario_policy='mean_physical_tape_expected_shop_demand_value',
        reserved_full_challengers=reserved_full_challengers,completed_challengers=len(complete)-1,
        main_challengers_quoted=main_challengers_quoted,
        main_challengers_available=len(challengers),parent_window_seconds=parent_deadline,
        allocation_reserved_seconds=allocation_reserve,quote_costs=quote_timing.snapshot(observation['step']),
        seconds=elapsed(),selected_adaptive_crops=adaptive,
        reason='Complete physical settlement tapes share the same declared supply world and expected future-shop repricing; pressure scenarios are offline diagnostics')
