"""Spread a cohort frontier over ordinary calls and revalidate before adoption."""
from copy import deepcopy
import math
import time

import search_policy
from cohort_obligations import same_day_establishments
from cohort_proposals import proposals
from cohort_response import build
from crop_route_search import ordinary_response as existing_response
from p110_current_world import intraday_world
from top1_planner import quote_timing
from top1_planner.allocation_admission import _comparison
from top1_planner.available_crop_admission import quote_summary


_JOB = None


def quote(obs, cfg, owner, world, programme):
    began = time.perf_counter()
    result = search_policy.compile_recipe(obs, cfg, programme, world, settlement=True,
        continuation=owner.fork(programme=programme, settlement=True),
        bind_herd=owner.bind_herd, adaptive_crops=owner.adaptive_crops)
    quote_timing.observe(obs['step'], True, time.perf_counter() - began)
    return result


def compare(obs, revision, compiled, baseline):
    row = dict(_comparison(compiled, quote_summary(baseline.result, obs), obs, revision),
        programme=revision.programme.name, points=revision.detail['points'],
        minimum_cash=compiled.result['minimum_cash'])
    if not math.isfinite(row['minimum_cash']) or row['minimum_cash'] < 0:
        row['rejection_reasons'].append('UNFUNDED_RESPONSE')
    missing = same_day_establishments(obs, baseline.result, compiled.result)
    row['lost_same_day_establishments'] = missing
    if missing:
        row['rejection_reasons'].append('UNDECLARED_CURRENT_ESTABLISHMENT_LOSS')
    row['admitted'] = not row['rejection_reasons']
    return row


class Frontier:
    def __init__(self, obs, cfg, owner, world, baseline, revisions, first):
        self.obs, self.cfg, self.owner, self.world = obs, cfg, owner, world
        self.baseline = baseline
        self.revisions = revisions
        self.first = first
        self.next = 0
        self.results = []
        self.best = None

    def invalid(self, obs, cfg, owner):
        if int(obs['player']) != int(self.obs['player']):
            return 'OBSERVER_CHANGED'
        if int(obs['step']) <= int(self.obs['step']):
            return 'TIME_RESET'
        if int(obs['step']) // 24 != int(self.obs['step']) // 24:
            return 'DAY_CHANGED'
        if cfg != self.cfg:
            return 'CONFIGURATION_CHANGED'
        if owner.programme != self.owner.programme or owner.adaptive_crops != self.owner.adaptive_crops:
            return 'LIVE_PROGRAMME_CHANGED'
        return None

    def advance(self):
        revision = self.revisions[self.next]
        compiled = quote(self.obs, self.cfg, self.owner, self.world, revision.programme)
        row = compare(self.obs, revision, compiled, self.baseline)
        self.results.append(row)
        self.next += 1
        if row['admitted'] and (self.best is None or row['relative'] > self.best[1]['relative']):
            self.best = revision, row
        return row

    def validate(self, obs, cfg, owner, worlds, deadline):
        revision, original = self.best
        detail = dict(origin=self.obs['step'], step=obs['step'],
            programme=revision.programme.name, preparatory_relative_delta=original['relative_delta'],
            fresh_pair=False, changed=False)
        if not owner.can_continue(revision.programme):
            detail['reason'] = 'PAID_PREFIX_CHANGED'
            return owner, detail
        world, detail['p110_intraday'] = intraday_world(obs, worlds(obs, future_model='uniform_mean')[1])
        baseline = quote(obs, cfg, owner, world, owner.programme)
        compiled = quote(obs, cfg, owner, world, revision.programme)
        row = compare(obs, revision, compiled, baseline)
        row['completed_before_deadline'] = time.perf_counter() <= deadline
        if not row['completed_before_deadline']:
            row['rejection_reasons'].append('INCOMPLETE_WITHIN_BUDGET')
            row['admitted'] = False
        detail.update(fresh_pair=True, result=row, changed=row['admitted'],
            reason='FRESH_STATE_ADMITTED' if row['admitted'] else 'FRESH_STATE_REJECTED')
        if row['admitted']:
            return owner.fork(programme=revision.programme, settlement=True), detail
        return owner, detail


def ordinary_response(obs, cfg, owner, *, free_seconds, shared_remaining, worlds):
    global _JOB
    began = time.perf_counter()
    tick = int(obs['step'])
    if tick == 0:
        _JOB = None
    start = tick % 72 == 1 and 217 <= tick <= 481
    previous = None
    if not start:
        owner, previous = existing_response(obs, cfg, owner,
            free_seconds=free_seconds, shared_remaining=shared_remaining, worlds=worlds)
    if not start and _JOB is None:
        return owner, previous
    record = dict(kind='ORDINARY_COHORT_FRONTIER', step=tick, changed=False,
        origin=tick if start else _JOB.obs['step'], snapshot_quotes=0, fresh_quotes=0,
        free_seconds=free_seconds, shared_remaining=shared_remaining, selected=owner.programme.name)
    if previous is not None:
        record['existing_response'] = previous
    def finish(phase, reason):
        record.update(phase=phase, reason=reason, seconds=time.perf_counter() - began)
        return owner, record
    if start:
        _JOB = None
        estimate = quote_timing.estimate(tick, True)
        if estimate * 1.30 + .10 > free_seconds:
            return finish('SKIPPED', 'INCUMBENT_DOES_NOT_FIT_FREE_TIME')
        frozen = deepcopy(obs)
        controller = owner.fork()
        world, record['p110_intraday'] = intraday_world(frozen, worlds(frozen, future_model='uniform_mean')[1])
        baseline = quote(frozen, cfg, controller, world, controller.programme)
        record['snapshot_quotes'] = 1
        promised = same_day_establishments(frozen, baseline.result, dict(day_states=[]))
        first = tick // 24 + bool(promised)
        _, choices = proposals(controller, frozen, baseline)
        revisions = tuple(build(controller, frozen, (choice['points'],), allow_hire=True,
            day_states=baseline.result['day_states'], start_day=first) for choice in choices[:4])
        record.update(construction_day=first, proposals=[r.programme.name for r in revisions],
            protected_same_day_establishments=promised)
        if not revisions:
            return finish('FINISHED', 'NO_COHORT_CANDIDATES')
        _JOB = Frontier(frozen, dict(cfg), controller, world, baseline, revisions, first)
        return finish('PREPARED', 'SNAPSHOT_FOR_RANKING_ONLY')
    invalid = _JOB.invalid(obs, cfg, owner)
    if invalid:
        _JOB = None
        return finish('CANCELLED', invalid)
    job = _JOB
    remaining_free = max(0.0, free_seconds - (time.perf_counter() - began))
    if job.next < len(job.revisions):
        estimate = quote_timing.estimate(job.obs['step'], True)
        if estimate * 1.30 + .10 > remaining_free:
            return finish('WAITING', 'CANDIDATE_DOES_NOT_FIT_FREE_TIME')
        record['ranking_result'] = job.advance()
        record['snapshot_quotes'] = 1
        record['ranking_completed'] = job.next
        if job.next == len(job.revisions) and job.best is None:
            _JOB = None
            return finish('FINISHED', 'NO_PREPARATORY_CANDIDATE_PASSED')
        return finish('RANKED', 'AWAIT_REMAINING_RANKING_OR_FRESH_ADMISSION')
    if job.best is None:
        raise RuntimeError('A finished frontier has no candidate to validate')
    # Preparation uses ordinary-call time only. Fresh paired admission may use
    # at most one second of the already shared allowance, with a terminal guard.
    allowance = remaining_free + min(1.0, max(0.0, shared_remaining - 2.0))
    if quote_timing.estimate(tick, True) * 2.6 + .15 > allowance:
        return finish('WAITING', 'FRESH_PAIR_NOT_YET_FUNDED')
    deadline = time.perf_counter() + allowance
    owner, validation = job.validate(obs, cfg, owner, worlds, deadline)
    record.update(validation=validation, fresh_quotes=2 if validation['fresh_pair'] else 0,
        changed=validation['changed'], selected=owner.programme.name,
        construction_day=job.first, ranked_candidates=len(job.results))
    _JOB = None
    return finish('VALIDATED', validation['reason'])
