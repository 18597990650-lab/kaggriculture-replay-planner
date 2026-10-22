"""Pack existing crop visits before paying for the last opening hand."""
from collections import Counter
from dataclasses import replace
import time

from .task_allocation import _Calendar, CROP_WORK


def pack_last(observation, controller, programme):
    day = int(observation['step']) // 24
    count = programme.workers[day]
    farm = observation['farms'][int(observation['player'])]
    if count <= len(farm['hands']) or count < 1:
        return None
    donor = programme.days[day].get(count, ())
    if not donor or any(task.operation not in CROP_WORK for task in donor):
        return None
    rows = dict(programme.days[day])
    del rows[count]
    workers = list(programme.workers)
    workers[day] = count - 1
    calendar = _Calendar(programme, observation, controller)
    visits = []
    for task in donor:
        if visits and visits[-1][-1].point == task.point:
            visits[-1].append(task)
        else:
            visits.append([task])
    transfers = [calendar.insert(rows, workers, day, tuple(block), False) for block in visits]
    before = Counter(task for seq in programme.days[day].values() for task in seq)
    after = Counter(task for seq in rows.values() for task in seq)
    if before != after or workers[day] != count - 1:
        raise ValueError('Packing must retain every existing task without adding labor')
    days = list(programme.days)
    days[day] = rows
    return replace(programme, name=programme.name + f'-packed-{day}-{count-1}',
                   days=tuple(days), workers=tuple(workers)), transfers


def same_production(base, candidate):
    return (len(base['day_states']) == len(candidate['day_states'])
            and all(a['step'] == b['step'] and a['farm']['tiles'] == b['farm']['tiles']
                    and a['farm']['unlocked_quadrants'] == b['farm']['unlocked_quadrants']
                    for a, b in zip(base['day_states'], candidate['day_states'])))


def optimize_opening_work(observation, configuration, controller, seconds):
    from .recipe import compile_recipe
    from .worlds import public_worlds
    tick = int(observation['step'])
    if tick % 24 or not 0 <= tick < 120 or seconds < .15:
        return controller, None
    first = pack_last(observation, controller, controller.programme)
    if first is None:
        return controller, None
    started = time.perf_counter()
    deadline = started + seconds
    world = public_worlds(observation, future_model='uniform_mean')[0]
    record = dict(kind='OPENING_WORK_PACKING', step=tick, changed=False, quotes=[],
                  horizon=144, all_original_tasks_retained=True,
                  evidence='Finite opening continuation under declared visible supply, with all daily crop and animal states checked; not terminal or online gain.')

    def quote(programme):
        return compile_recipe(observation, configuration, programme, world,
            continuation=controller.fork(programme=programme), end_step=144).result

    base = quote(controller.programme)
    cost = time.perf_counter() - started
    best = base['cash']
    winner = controller.programme
    candidate = first
    for _ in range(2):
        if candidate is None or time.perf_counter() + cost * 1.25 + .015 > deadline:
            break
        programme, transfers = candidate
        before = time.perf_counter()
        result = quote(programme)
        cost = max(cost, time.perf_counter() - before)
        same = same_production(base, result)
        passed = (time.perf_counter() <= deadline and not result['issues'] and same
                  and not (Counter(result['lost']) - Counter(base['lost']))
                  and not (Counter(base['harvest']) - Counter(result['harvest']))
                  and result['cash'] > best)
        record['quotes'].append(dict(hands=programme.workers[tick//24], transfers=transfers,
            cash_delta=result['cash']-base['cash'], same_daily_production=same,
            minimum_cash=result['minimum_cash'], issues=len(result['issues']), passed=passed))
        if not passed:
            break
        winner, best = programme, result['cash']
        candidate = pack_last(observation, controller, programme)
    record.update(changed=winner != controller.programme, cash_delta=best-base['cash'],
                  seconds=time.perf_counter()-started, selected=winner.name)
    return (controller.fork(programme=winner) if winner != controller.programme else controller), record
