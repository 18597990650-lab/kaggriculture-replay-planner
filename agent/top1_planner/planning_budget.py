"""Account for total overage and allocate it across remaining decisions."""


def decision_window(*,step,total_remaining,free_seconds,quote_seconds,projects=False):
    if min(total_remaining,free_seconds,quote_seconds)<0:
        raise ValueError('Invalid decision budget')
    nodes=[*( (tick,1.0) for tick in range(72,577,72)),
           (600,.5),(624,.5),(648,.5),(696,.75)]
    future=[(tick,weight) for tick,weight in nodes if tick>=step]
    if not future or future[0][0]!=step:
        raise ValueError('No scheduled decision at this step')
    weights=[max(24,719-tick)*weight for tick,weight in future]
    reserve=max(2.0,min(4.0,quote_seconds*1.35))
    available=max(0.0,total_remaining-reserve)
    extra=available*weights[0]/sum(weights)
    wall=free_seconds+extra
    optional=extra*.28 if projects else 0.0
    if wall-optional<quote_seconds*1.25+.15:
        optional=0.0
    return dict(core_seconds=wall-optional,optional_seconds=optional,
        total_seconds=wall,overage_seconds=extra,guard_seconds=reserve,
        remaining_decisions=len(future),weight_share=weights[0]/sum(weights),
        quote_seconds=quote_seconds)


def remaining(*,core_limit,optional_limit,spent,optional_spent,framework_remaining=None):
    if min(core_limit,optional_limit,spent,optional_spent)<0 or optional_spent>spent:
        raise ValueError('Invalid planning expenditure ledger')
    total=max(0.0,core_limit+optional_limit-spent)
    if framework_remaining is not None:
        total=min(total,max(0.0,framework_remaining))
    core=max(0.0,core_limit-(spent-optional_spent))
    optional=max(0.0,optional_limit-optional_spent)
    return min(core,total),min(optional,total),total


def optional_charge(*,elapsed,base,optional_duration,optional_remaining):
    if min(elapsed,base,optional_duration,optional_remaining)<0:
        raise ValueError('Invalid timed call')
    # Free per-turn time is counted only once. Any optional overrun remains
    # charged to the total ledger and cannot create extra core allowance.
    return min(max(0.0,elapsed-base),optional_duration,optional_remaining)
