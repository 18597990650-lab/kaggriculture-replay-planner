"""Precompute complete sibling quotes without changing their evaluation order."""
import multiprocessing
import os
import sys
import threading

from .quote_batch import QuoteRequest, compile_batch


class QuotePrefetch:
    def __init__(self, observation, configuration, controller, record):
        default = '2' if sys.platform == 'linux' else '1'
        self.processes = int(os.environ.get('FARM_QUOTE_PROCESSES', default))
        if self.processes not in (1, 2):
            raise ValueError('FARM_QUOTE_PROCESSES must be 1 or 2')
        self.observation = observation
        self.configuration = configuration
        self.controller = controller
        self.record = record
        self.cache = {}
        self.stats = dict(batches=0, computed=0, consumed=0, child_quotes=0, seconds=0.0, processes=self.processes)

    def _key(self, programme, adaptive, world, settlement):
        return programme.name, adaptive, id(world), settlement

    def ready(self, programme, adaptive, world, settlement):
        if settlement is not True:
            return False
        value = self.cache.get(self._key(programme, adaptive, world, settlement))
        if value is None:
            return False
        before = value[0]
        if before.days != programme.days or before.workers != programme.workers:
            raise ValueError('Prefetched quote identity refers to different work plans')
        return True

    def take(self, programme, adaptive, world, settlement):
        if not self.ready(programme, adaptive, world, settlement):
            return None
        self.stats['consumed'] += 1
        return self.cache.pop(self._key(programme, adaptive, world, settlement))[1]

    def prepare(self, programmes, adaptive, world, settlement):
        if self.processes == 1 or settlement is not True:
            return False
        if (multiprocessing.current_process().daemon or threading.active_count() != 1
                or 'fork' not in multiprocessing.get_all_start_methods()):
            return False
        unique = {}
        for programme in programmes:
            if not self.ready(programme, adaptive, world, settlement):
                if programme.name in unique and unique[programme.name] != programme:
                    raise ValueError('Batch contains conflicting programme identities')
                unique[programme.name] = programme
        programmes = tuple(unique.values())
        if len(programmes) != 2:
            return False
        requests = tuple(QuoteRequest(p, world, adaptive, settlement) for p in programmes)
        batch = compile_batch(self.observation, self.configuration, self.controller, requests,
                              processes=2)
        for programme, compiled in zip(programmes, batch.compiled):
            self.cache[self._key(programme, adaptive, world, settlement)] = programme, compiled
        self.stats['batches'] += 1
        self.stats['computed'] += len(programmes)
        self.stats['child_quotes'] += batch.worker_quotes[1]
        self.stats['seconds'] += batch.seconds
        self.record(len(programmes), batch.seconds)
        return True
