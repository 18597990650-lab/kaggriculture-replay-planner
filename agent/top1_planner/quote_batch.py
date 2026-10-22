"""Evaluate a fixed quote list with one caller and at most one forked worker."""
from dataclasses import dataclass
import gc
import multiprocessing
import threading
import time
import traceback

from top1_planner.recipe import compile_recipe


@dataclass(frozen=True)
class QuoteRequest:
    programme: object
    world: object
    adaptive_crops: bool
    settlement: bool


@dataclass(frozen=True)
class QuoteBatch:
    compiled: tuple
    per_quote_seconds: tuple
    worker_quotes: tuple
    seconds: float
    processes: int


def _compile(observation, configuration, controller, request):
    continuation = None
    if (controller.can_continue(request.programme)
            and request.adaptive_crops == controller.adaptive_crops):
        continuation = controller.fork(programme=request.programme,
                                       settlement=request.settlement)
    started = time.perf_counter()
    result = compile_recipe(observation, configuration, request.programme, request.world,
        continuation=continuation, settlement=request.settlement,
        bind_herd=controller.bind_herd, adaptive_crops=request.adaptive_crops)
    return result, time.perf_counter() - started


def _consume(observation, configuration, controller, requests, next_index):
    output = []
    while True:
        with next_index.get_lock():
            index = next_index.value
            next_index.value = index + 1
        if index >= len(requests):
            return output
        result, seconds = _compile(observation, configuration, controller, requests[index])
        output.append((index, result, seconds))


def _child(receive, send, observation, configuration, controller, requests, next_index):
    receive.close()
    try:
        rows = _consume(observation, configuration, controller, requests, next_index)
        send.send((rows, None))
    except BaseException:
        send.send((None, traceback.format_exc()))
    finally:
        send.close()


def _compile_batch(observation, configuration, controller, requests, *, processes):
    """Return every complete result in input order, without ranking or budget cutoffs.

    The caller owns the worker lifetime; no live state or background work survives
    this call. Parallel execution requires a single-threaded, non-daemon Linux
    process. This function never changes the candidate set, worlds or horizon.
    """
    if processes not in (1, 2):
        raise ValueError('Quote batches require one or two processes')
    requests = tuple(requests)
    started = time.perf_counter()
    if processes == 1 or len(requests) < 2:
        rows = [_compile(observation, configuration, controller, r) for r in requests]
        return QuoteBatch(tuple(row[0] for row in rows), tuple(row[1] for row in rows),
                          (len(rows),), time.perf_counter() - started, 1)
    if 'fork' not in multiprocessing.get_all_start_methods():
        raise RuntimeError('Parallel quote batches require fork support')
    if multiprocessing.current_process().daemon or threading.active_count() != 1:
        raise RuntimeError('Parallel quotes require a non-daemon, single-threaded caller')
    context = multiprocessing.get_context('fork')
    next_index = context.Value('Q', 0)
    receive, send = context.Pipe(duplex=False)
    worker = context.Process(target=_child,
        args=(receive, send, observation, configuration, controller, requests, next_index))
    worker.start()
    send.close()
    try:
        own = _consume(observation, configuration, controller, requests, next_index)
        try:
            other, error = receive.recv()
        except EOFError as exc:
            raise RuntimeError('Quote worker exited without a complete response') from exc
        if error is not None:
            raise RuntimeError('Quote worker failed:\n' + error)
        worker.join(1)
        if worker.exitcode != 0:
            raise RuntimeError('Quote worker did not exit cleanly: ' + str(worker.exitcode))
        rows = sorted((*own, *other), key=lambda row: row[0])
        if [row[0] for row in rows] != list(range(len(requests))):
            raise RuntimeError('Quote worker returned an incomplete or duplicated batch')
        return QuoteBatch(tuple(row[1] for row in rows), tuple(row[2] for row in rows),
                          (len(own), len(other)), time.perf_counter() - started, 2)
    finally:
        receive.close()
        if worker.is_alive():
            worker.terminate()
        worker.join()
        worker.close()


def compile_batch(observation, configuration, controller, requests, *, processes,
                  collect_garbage=False, collection_policy='full'):
    """Optionally consolidate cyclic-GC work and charge collection to this batch.

    The opt-in mode is intended for larger, complete candidate batches. Small
    batches retain automatic collection unless the caller explicitly requests it.
    The caller's enabled/disabled GC state is restored on success and failure.
    """
    if collection_policy not in ('full', 'generational'):
        raise ValueError('Unknown batch garbage collection policy')
    if not collect_garbage:
        return _compile_batch(observation, configuration, controller, requests, processes=processes)
    started = time.perf_counter()
    enabled = gc.isenabled()
    gc.disable()
    try:
        result = _compile_batch(observation, configuration, controller, requests, processes=processes)
    finally:
        generation = 2
        if collection_policy == 'generational':
            counts = gc.get_count()
            thresholds = gc.get_threshold()
            generation = (0 if counts[1] < thresholds[1]
                          else 1 if counts[2] < thresholds[2] else 2)
        gc.collect(generation)
        if enabled:
            gc.enable()
    return QuoteBatch(result.compiled, result.per_quote_seconds, result.worker_quotes,
                      time.perf_counter() - started, result.processes)
