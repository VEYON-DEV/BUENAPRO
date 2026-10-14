"""Shared limits for threads owned by one globally locked coordinator."""
from __future__ import annotations

from contextlib import contextmanager
import threading
import time

_NOT_CONFIGURED = object()


class LimitReached(RuntimeError):
    pass


class SharedPacer:
    def __init__(self, seconds, *, clock=time.monotonic, sleep=time.sleep):
        self.seconds, self.clock, self.sleep = seconds, clock, sleep
        self.last = None
        self.lock = threading.Lock()

    def __call__(self, request=None):
        with self.lock:
            if self.last is not None:
                delay = self.seconds - (self.clock() - self.last)
                if delay > 0:
                    self.sleep(delay)
            self.last = self.clock()


class RunLimits:
    def __init__(self, *, max_accepted, max_candidates, max_jev_calls, max_llm1_calls,
                 model_workers=2, accepted_ids=(), seen=0, jev_calls=0, llm1_calls=0, after_stage=None,
                 max_embedding_calls=_NOT_CONFIGURED, embedding_calls=0):
        self.max_accepted, self.max_candidates = max_accepted, max_candidates
        self.maximum = {"jev": max_jev_calls, "llm1": max_llm1_calls}
        self.calls = {"jev": jev_calls, "llm1": llm1_calls}
        self.reserved = {"jev": 0, "llm1": 0}
        if max_embedding_calls is not _NOT_CONFIGURED:
            self.maximum["embedding"] = max_embedding_calls
            self.calls["embedding"] = embedding_calls
            self.reserved["embedding"] = 0
        self.accepted_ids, self.seen = set(accepted_ids), seen
        self.lock = threading.RLock()
        self.model_slots = threading.BoundedSemaphore(model_workers)
        self.semantic_locks = {}
        self.after_stage = after_stage

    def stage(self, staging, candidate_id, item):
        with self.lock:
            # Even at the cap duplicates need no provider or new payload.
            if self.max_candidates is not None and self.seen >= self.max_candidates:
                return False
            added = staging.stage(candidate_id, item)
            self.seen += int(added)
            if added and self.after_stage:
                self.after_stage(staging.stats()["seen"])
            return added

    def stage_many(self, staging, entries):
        """Batch unlimited replay while retaining the bounded serial contract."""
        with self.lock:
            if self.max_candidates is not None:
                return [self.stage(staging, candidate_id, item) for candidate_id, item in entries]
            results = staging.stage_many(entries)
            if (not isinstance(results, list) or len(results) != len(entries)
                    or any(type(value) is not bool for value in results)):
                raise ValueError("Unexpected staging batch result vector")
            added = sum(results)
            self.seen += added
            if added and self.after_stage:
                self.after_stage(staging.stats()["seen"])
            return results

    def sweep_stopped(self):
        with self.lock:
            return self.max_candidates is not None and self.seen >= self.max_candidates

    def can_process(self, convocation):
        with self.lock:
            return (self.max_accepted is None or convocation in self.accepted_ids
                    or len(self.accepted_ids) < self.max_accepted)

    def admit(self, convocation, callback):
        """Reserve the slot and persist promotion while holding the same lock."""
        with self.lock:
            if not self.can_process(convocation):
                return None
            result = callback()
            self.accepted_ids.add(convocation)
            return result

    def before_request(self, provider):
        with self.lock:
            if (self.maximum[provider] is not None
                    and self.calls[provider] + self.reserved[provider] >= self.maximum[provider]):
                raise LimitReached("Configured provider-call ceiling reached")
            self.calls[provider] += 1

    def reserve_request(self, provider):
        """Reserve potential capacity BEFORE creating a durable payment claim."""
        with self.lock:
            if (self.maximum[provider] is not None
                    and self.calls[provider] + self.reserved[provider] >= self.maximum[provider]):
                raise LimitReached("Configured provider-call ceiling reached")
            self.reserved[provider] += 1
        return _Permit(self, provider)

    @contextmanager
    def model(self, key):
        with self.lock:
            lock = self.semantic_locks.setdefault(key, threading.Lock())
        # Identical payloads within this coordinator wait for a reusable receipt.
        with lock, self.model_slots:
            yield


class _Permit:
    def __init__(self, limits, provider):
        self.limits, self.provider, self.used, self.closed = limits, provider, False, False

    def issue(self):
        with self.limits.lock:
            if self.used or self.closed:
                raise LimitReached("Provider permit already consumed")
            self.limits.reserved[self.provider] -= 1
            self.limits.calls[self.provider] += 1
            self.used = True

    def close(self):
        with self.limits.lock:
            if not self.closed and not self.used:
                self.limits.reserved[self.provider] -= 1
            self.closed = True
