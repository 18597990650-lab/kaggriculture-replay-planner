"""Share screening effort between complete projects and occupied-site alternatives."""
from collections import defaultdict


def portfolio_key(revision):
    detail = revision.detail
    return (detail['item'], tuple(sorted(tuple(p) for p in detail['points'])),
            detail.get('fertilized', False))


def successor_screens(projects, successors):
    """Keep project leaders while admitting separately funded local production."""
    groups = defaultdict(list)
    for revision in successors:
        groups[portfolio_key(revision)].append(revision)
    local = []
    for group in sorted(groups.values(), key=lambda rows: -max(r.detail['estimate'] for r in rows)):
        # Staffing alternatives describe the same investment. Give the queue
        # with least predicted displacement the first physical comparison;
        # all alternatives remain available because the estimate can be wrong.
        group.sort(key=lambda r: (sum(w.get('estimated_overload', 0) for w in r.detail.get('work', ())),
                                  -r.detail['estimate'], r.programme.name))
        local.append(group)
    ordered = list(projects[:2])
    remaining_projects = list(projects[2:])
    for depth in range(max((len(rows) for rows in local), default=0)):
        for rows in local:
            if depth < len(rows):
                ordered.append(rows[depth])
                if remaining_projects:
                    ordered.append(remaining_projects.pop(0))
    ordered.extend(remaining_projects)
    seen = set()
    return tuple(r for r in ordered if not (r.programme.name in seen or seen.add(r.programme.name)))


def distinct_first(revisions):
    """Spend initial complete quotes on distinct investments before their variants."""
    seen, leaders, alternatives = set(), [], []
    for revision in revisions:
        signature = portfolio_key(revision)
        if signature in seen:
            alternatives.append(revision)
        else:
            seen.add(signature)
            leaders.append(revision)
    return tuple((*leaders, *alternatives))


def smaller_cohorts(failed, pending):
    """Inspect the next smaller complete crop calendar after missing production.

    Only reorder existing strict subsets. Both the standalone calendar and its
    compatible service variant remain eligible for a physical comparison.
    """
    detail = failed.detail
    if detail['mode'] != 'continuous_short_crop_calendar':
        return list(pending), ()
    points = {tuple(p) for p in detail['points']}
    smaller = [r for r in pending
        if r.detail['mode'] == detail['mode'] and r.detail['item'] == detail['item']
        and {tuple(p) for p in r.detail['points']} < points]
    if not smaller:
        return list(pending), ()
    size = max(len(r.detail['points']) for r in smaller)
    leading = sorted((r for r in smaller if len(r.detail['points']) == size),
                     key=lambda r: (-r.detail['estimate'], r.programme.name))[:2]
    names = {r.programme.name for r in leading}
    return leading + [r for r in pending if r.programme.name not in names], tuple(r.programme.name for r in leading)
