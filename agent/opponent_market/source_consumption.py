"""Continuously verified source wrapper for bounded consumption-window waiting."""
from collections import Counter
from copy import deepcopy
import gc
import time

from .ledger import RouteHypothesis
from .cached_source_policy import CachedSourcePolicy
from .bounded_consumption_wait import evaluate_consumption_wait


class SourceConsumption:
    def __init__(self, path, configuration):
        self.hypothesis = RouteHypothesis('public_v5_source', CachedSourcePolicy(path), configuration)
        self.active = True
        self.stats = Counter()
        self.rejection = None
        self.step = None
        self.rejected_observed_step = None
        self.rejected_simulated_steps = None

    def observe(self, observation):
        self.step = int(observation['step'])
        if self.active:
            self.hypothesis.observe(observation)
            if not self.hypothesis.consistent:
                self.rejection = dict(step=self.step, fit=self.hypothesis.last_fit)
                self.active = False
                self.rejected_observed_step = self.hypothesis.previous['step']
                self.rejected_simulated_steps = self.hypothesis.steps
                started = time.perf_counter()
                self.hypothesis = None
                self.stats['released_source_objects'] = gc.collect()
                self.stats['source_disposal_seconds'] = time.perf_counter()-started

    def apply(self, observation, configuration, action, controller, belief, *, deadline):
        if not self.active:
            report = self.report() if self.step % 24 == 0 or self.rejection['step'] == self.step else None
            return action, report
        started = time.perf_counter()
        chosen, raw = evaluate_consumption_wait(self.hypothesis, controller, observation,
            configuration, action, belief, deadline=deadline)
        decision = {k: v for k, v in raw.items() if k != 'baseline_prediction'}
        self.hypothesis.act_and_advance(chosen)
        self.stats['source_calls'] += 1
        self.stats['branch_quotes'] += decision['quotes']
        self.stats['changed_steps'] += int(decision['changed'])
        self.stats['budget_rejected_steps'] += int(decision['budget_exhausted'] and not decision['changed'])
        self.stats['short_model_own_delta'] += decision['own_delta']
        self.stats['short_model_relative_delta'] += decision['relative_delta']
        decision['seconds'] = time.perf_counter()-started
        self.stats['peak_decision_seconds'] = max(self.stats['peak_decision_seconds'], decision['seconds'])
        report = self.report() if decision['quotes'] or self.step % 24 == 0 else None
        if report is not None:
            report['decision'] = decision
        return deepcopy(chosen), report

    def report(self):
        return dict(status='awaiting_public_confirmation' if self.active else 'source_rejected',
            step=self.step, stats=dict(self.stats), rejection=self.rejection,
            observed_step=self.hypothesis.previous['step'] if self.active else self.rejected_observed_step,
            simulated_steps=self.hypothesis.steps if self.active else self.rejected_simulated_steps,
            scope='One identified source and three funded sale-error scenarios, not all legal futures')
