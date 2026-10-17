"""Direct port from full_cycle_app/translation/gemini/api/request_scheduler.py.
Algorithms and defaults retained. Only the GUI-independent cancellation-error import
was adapted for the server. Compare desktop behavior with offline parity tests.
"""
# -*- coding: utf-8 -*-
import asyncio
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from chaptera_translation_core.api.errors import OperationCancelledError
REQUEST_ROLE_PRIORITIES = {
    'repair_verify': 0,
    'missing_unit_repair': 1,
    'missing_repair': 1,
    'alignment_repair': 1,
    'completeness_repair': 1,
    'translation_recovery': 1,
    'targeted_repair': 2,
    'deterministic_repair': 2,
    'semantic_audit': 3,
    'book_consistency': 3,
    'consistency': 4,
    'glossary': 4,
    'translation': 5,
    'warmup': 6,
}
@dataclass(frozen=True)
class ApiRequestPermit:
    permit_id: int
    request_role: str
    waited_seconds: float
    queue_depth: int
    granted_at: float
    rpm_limit: int
    tpm_limit: int
    rps_limit: float
    interval_seconds: float
    token_cost: int
    previous_last_request_time: float
    previous_has_request: bool
@dataclass
class _SchedulerWaiter:
    ticket: int
    request_role: str
    priority: int
    created_at: float
    queue_depth: int
    token_cost: int = 0
class ApiRequestScheduler:
    def __init__(self, rpm_limit: int, *, tpm_limit: int = 0, rps_limit: float = 0.0, clock=None, sleep_func=None, aging_seconds: float | None = None):
        # Инициализирует thread-safe per-key scheduler с RPM/RPS/TPM gates, quality-first очередью и starvation aging.
        self.lock = threading.Lock()
        self._clock = clock or time.monotonic
        self._sleep = sleep_func or asyncio.sleep
        self.rpm_limit = max(0, int(rpm_limit or 0))
        self.tpm_limit = max(0, int(tpm_limit or 0))
        self.rps_limit = max(0.0, float(rps_limit or 0.0))
        self.interval = self._configured_interval()
        self.last_request_time = 0.0
        self._has_request = False
        self._cooldown_until = 0.0
        self._tpm_events = deque()
        self._waiters = {}
        self._next_ticket = 1
        self._next_permit_id = 1
        self._last_permit_id = None
        self._aging_seconds_override = aging_seconds
    def _configured_interval(self) -> float:
        # Возвращает самый строгий межзапросный интервал из RPM и optional RPS limits.
        rpm_interval = 60.0 / self.rpm_limit if self.rpm_limit > 0 else 0.0
        rps_interval = 1.0 / self.rps_limit if self.rps_limit > 0 else 0.0
        return max(rpm_interval, rps_interval)
    def _aging_seconds(self) -> float:
        # Возвращает интервал повышения приоритета, ограничивая голодание новых translation requests.
        if self._aging_seconds_override is not None:
            return max(0.01, float(self._aging_seconds_override))
        return max(1.0, self.interval if self.interval > 0 else 1.0)
    def _prune_tpm_locked(self, now: float) -> None:
        # Удаляет token reservations, вышедшие из rolling 60-second TPM window.
        cutoff = now - 60.0
        while self._tpm_events and self._tpm_events[0][0] <= cutoff:
            self._tpm_events.popleft()
    def _tpm_ready_at_locked(self, token_cost: int, now: float) -> float:
        # Вычисляет момент, когда новый conservative token reservation гарантированно поместится в TPM.
        self._prune_tpm_locked(now)
        if self.tpm_limit <= 0 or token_cost <= 0:
            return 0.0
        used = sum(cost for _, cost, _ in self._tpm_events)
        if used + token_cost <= self.tpm_limit:
            return 0.0
        required_release = used + token_cost - self.tpm_limit
        released = 0
        for timestamp, cost, _ in self._tpm_events:
            released += cost
            if released >= required_release:
                return timestamp + 60.0
        return now + 60.0
    def _ready_at_locked(self, token_cost: int = 0) -> float:
        # Вычисляет ближайший момент выдачи permit с учетом RPM/RPS spacing, TPM и provider cooldown.
        now = self._clock()
        interval_ready = self.last_request_time + self.interval if self._has_request and self.interval > 0 else 0.0
        return max(interval_ready, self._cooldown_until, self._tpm_ready_at_locked(token_cost, now))
    def _effective_priority_locked(self, waiter: _SchedulerWaiter, now: float) -> int:
        # Повышает долго ожидающий запрос до quality-priority без нарушения FIFO среди равных кандидатов.
        age_steps = int(max(0.0, now - waiter.created_at) / self._aging_seconds())
        return max(0, waiter.priority - age_steps)
    def _winner_locked(self, now: float) -> _SchedulerWaiter | None:
        # Выбирает текущего quality-first победителя с aging и FIFO tie-break.
        if not self._waiters:
            return None
        return min(
            self._waiters.values(),
            key=lambda waiter: (self._effective_priority_locked(waiter, now), waiter.created_at, waiter.ticket),
        )
    def _register_waiter_locked(self, request_role: str, now: float, token_cost: int) -> _SchedulerWaiter:
        # Добавляет request waiter с conservative TPM cost и фиксирует глубину очереди на момент постановки.
        ticket = self._next_ticket
        self._next_ticket += 1
        waiter = _SchedulerWaiter(
            ticket=ticket,
            request_role=request_role,
            priority=REQUEST_ROLE_PRIORITIES.get(request_role, REQUEST_ROLE_PRIORITIES['translation']),
            created_at=now,
            queue_depth=len(self._waiters) + 1,
            token_cost=token_cost,
        )
        self._waiters[ticket] = waiter
        return waiter
    async def acquire(self, request_role: str = 'translation', *, token_cost: int = 0, is_cancelled=None) -> ApiRequestPermit:
        # Ожидает quality-priority RPM/RPS/TPM slot и прекращает ожидание при отмене worker/session.
        token_cost = max(0, int(token_cost or 0))
        if self.tpm_limit > 0 and token_cost > self.tpm_limit:
            raise ValueError(f'Request token budget {token_cost} exceeds TPM limit {self.tpm_limit}.')
        now = self._clock()
        with self.lock:
            waiter = self._register_waiter_locked(request_role, now, token_cost)
        acquired = False
        try:
            while True:
                if callable(is_cancelled) and is_cancelled():
                    raise OperationCancelledError('API request отменен во время ожидания scheduler permit.')
                with self.lock:
                    now = self._clock()
                    winner = self._winner_locked(now)
                    ready_at = self._ready_at_locked(winner.token_cost if winner is not None else 0)
                    if winner is not None and winner.ticket == waiter.ticket and now >= ready_at:
                        self._waiters.pop(waiter.ticket, None)
                        previous_last_request_time = self.last_request_time
                        previous_has_request = self._has_request
                        self.last_request_time = now
                        self._has_request = True
                        permit_id = self._next_permit_id
                        self._next_permit_id += 1
                        self._last_permit_id = permit_id
                        if token_cost > 0 and self.tpm_limit > 0:
                            self._tpm_events.append((now, token_cost, permit_id))
                        acquired = True
                        return ApiRequestPermit(
                            permit_id=permit_id,
                            request_role=request_role,
                            waited_seconds=max(0.0, now - waiter.created_at),
                            queue_depth=waiter.queue_depth,
                            granted_at=now,
                            rpm_limit=self.rpm_limit,
                            tpm_limit=self.tpm_limit,
                            rps_limit=self.rps_limit,
                            interval_seconds=self.interval,
                            token_cost=token_cost,
                            previous_last_request_time=previous_last_request_time,
                            previous_has_request=previous_has_request,
                        )
                    delay = max(0.01, ready_at - now) if winner is not None and winner.ticket == waiter.ticket else 0.05
                await self._sleep(min(delay, 0.20))
        finally:
            if not acquired:
                with self.lock:
                    self._waiters.pop(waiter.ticket, None)
    def refund(self, permit: ApiRequestPermit | None) -> bool:
        # Возвращает pre-provider RPM/RPS/TPM permit без удаления уже совершенного provider start.
        if permit is None:
            return False
        with self.lock:
            token_refunded = False
            if self._tpm_events:
                kept = deque(event for event in self._tpm_events if event[2] != permit.permit_id)
                token_refunded = len(kept) != len(self._tpm_events)
                self._tpm_events = kept
            timing_refunded = permit.permit_id == self._last_permit_id
            if timing_refunded:
                self.last_request_time = permit.previous_last_request_time
                self._has_request = permit.previous_has_request
                self._last_permit_id = None
            return token_refunded or timing_refunded
    def apply_cooldown(self, delay_seconds: float) -> float:
        # Продлевает общий cooldown данного key/model bucket после provider 429 или transport backoff.
        delay = max(0.0, float(delay_seconds or 0.0))
        with self.lock:
            self._cooldown_until = max(self._cooldown_until, self._clock() + delay)
            return self._cooldown_until
    def reset(self):
        # Обнуляет spacing/cooldown/TPM reservations, не уничтожая уже ожидающие coroutine requests.
        with self.lock:
            self.last_request_time = 0.0
            self._has_request = False
            self._cooldown_until = 0.0
            self._tpm_events.clear()
            self._last_permit_id = None
    def get_rpm(self) -> int:
        # Возвращает текущий adaptive RPM limit.
        with self.lock:
            return self.rpm_limit
    def decrease_rpm(self, percentage=25):
        # Снижает adaptive RPM минимум на единицу и пересчитывает общий RPM/RPS spacing interval.
        with self.lock:
            reduction = int(self.rpm_limit * (float(percentage) / 100.0))
            self.rpm_limit = max(1, self.rpm_limit - max(1, reduction))
            self.interval = self._configured_interval()
    def set_rpm(self, new_rpm):
        # Устанавливает положительный RPM и пересчитывает общий RPM/RPS spacing interval.
        with self.lock:
            if new_rpm > 0:
                self.rpm_limit = int(new_rpm)
                self.interval = self._configured_interval()
    def set_tpm(self, new_tpm):
        # Устанавливает non-negative TPM ceiling для последующих conservative token reservations.
        with self.lock:
            self.tpm_limit = max(0, int(new_tpm or 0))
    def set_rps(self, new_rps):
        # Устанавливает optional RPS ceiling и пересчитывает общий межзапросный interval.
        with self.lock:
            self.rps_limit = max(0.0, float(new_rps or 0.0))
            self.interval = self._configured_interval()
    def update_last_request_time(self, delay=0):
        # Сдвигает следующий разрешенный start ровно на delay секунд от текущего момента.
        with self.lock:
            next_allowed_time = self._clock() + max(0.0, float(delay or 0.0))
            self.last_request_time = next_allowed_time - self.interval
            self._has_request = True
            self._cooldown_until = max(self._cooldown_until, next_allowed_time)
    def sync_last_request_time(self, timestamp):
        # Синхронизирует spacing с внешним timestamp для совместимости старого runtime.
        with self.lock:
            self.last_request_time = float(timestamp)
            self._has_request = True
    def can_proceed(self) -> bool:
        # Выполняет legacy non-blocking admission без token reservation и потребляет spacing slot при разрешении.
        with self.lock:
            now = self._clock()
            if now < self._ready_at_locked():
                return False
            self.last_request_time = now
            self._has_request = True
            return True
    def snapshot(self) -> dict:
        # Возвращает thread-safe runtime snapshot RPM/RPS/TPM state для diagnostics и regression-тестов.
        with self.lock:
            now = self._clock()
            self._prune_tpm_locked(now)
            tpm_used = sum(cost for _, cost, _ in self._tpm_events)
            return {
                'rpm_limit': self.rpm_limit,
                'rps_limit': self.rps_limit,
                'tpm_limit': self.tpm_limit,
                'tpm_reserved': tpm_used,
                'tpm_remaining': max(0, self.tpm_limit - tpm_used) if self.tpm_limit > 0 else None,
                'interval_seconds': self.interval,
                'queue_depth': len(self._waiters),
                'next_allowed_in': max(0.0, self._ready_at_locked() - now),
                'cooldown_remaining': max(0.0, self._cooldown_until - now),
                'last_request_time': self.last_request_time,
            }
@dataclass(frozen=True)
class ApiNetworkPermit:
    permit_id: int
    request_role: str
    waited_seconds: float
    granted_at: float
    inflight_at_grant: int
    max_inflight: int
    start_interval_seconds: float
    state: str
    is_probe: bool
class ApiNetworkController:
    def __init__(self, max_inflight: int, start_interval_seconds: float = 0.20, *, clock=None, sleep_func=None, circuit_threshold: int = 3, circuit_base_delay: float = 2.0, max_circuit_delay: float = 60.0, recovery_successes: int = 4, rolling_window: int = 20, health_min_samples: int = 6, degraded_error_ratio: float = 0.20, circuit_error_ratio: float = 0.50, jitter_func=None):
        # Инициализирует session-wide pacing/controller с rolling health, AIMD recovery и jittered circuit breaker.
        self.lock = threading.Lock()
        self._clock = clock or time.monotonic
        self._sleep = sleep_func or asyncio.sleep
        self.base_max_inflight = max(1, int(max_inflight or 1))
        self.max_inflight = self.base_max_inflight
        self.base_start_interval = max(0.0, float(start_interval_seconds or 0.0))
        self.start_interval = self.base_start_interval
        self.max_start_interval = max(self.base_start_interval, 2.0)
        self.circuit_threshold = max(1, int(circuit_threshold or 1))
        self.circuit_base_delay = max(0.01, float(circuit_base_delay or 0.01))
        self.max_circuit_delay = max(self.circuit_base_delay, float(max_circuit_delay or self.circuit_base_delay))
        self.recovery_successes = max(1, int(recovery_successes or 1))
        self.rolling_window = max(4, int(rolling_window or 20))
        self.health_min_samples = max(2, int(health_min_samples or 6))
        self.degraded_error_ratio = min(1.0, max(0.0, float(degraded_error_ratio or 0.0)))
        self.circuit_error_ratio = min(1.0, max(self.degraded_error_ratio, float(circuit_error_ratio or 0.0)))
        self._jitter = jitter_func or (lambda delay: delay * random.uniform(0.85, 1.15))
        self._last_start_time = 0.0
        self._has_started = False
        self._inflight = 0
        self._network_error_streak = 0
        self._provider_error_streak = 0
        self._success_streak = 0
        self._circuit_trip_count = 0
        self._circuit_scope = None
        self._recent_outcomes = deque(maxlen=self.rolling_window)
        self._circuit_open_until = 0.0
        self._probe_inflight = False
        self._waiters = {}
        self._active_permits = {}
        self._next_ticket = 1
        self._next_permit_id = 1
    def _error_ratio_locked(self) -> float:
        # Возвращает rolling долю availability ошибок: local transport и provider 5xx, но не content/validation/rate-limit.
        if not self._recent_outcomes:
            return 0.0
        return sum(self._recent_outcomes) / len(self._recent_outcomes)
    def _state_locked(self, now: float) -> str:
        # Возвращает текущее health-состояние без повторного захвата lock.
        if now < self._circuit_open_until:
            return 'open'
        if max(self._network_error_streak, self._provider_error_streak) >= self.circuit_threshold:
            return 'half_open'
        recent_ratio = self._error_ratio_locked()
        if (
            self.max_inflight < self.base_max_inflight
            or self.start_interval > self.base_start_interval
            or (len(self._recent_outcomes) >= self.health_min_samples and recent_ratio >= self.degraded_error_ratio)
        ):
            return 'degraded'
        return 'healthy'
    def _health_state_locked(self, now: float) -> str:
        # Не маскирует provider outage под потерю локального интернета: scope сохраняется на весь circuit/recovery цикл.
        state = self._state_locked(now)
        scope = self._circuit_scope or ('provider' if self._provider_error_streak > self._network_error_streak else 'network')
        if state == 'open':
            return 'PROVIDER_UNAVAILABLE' if scope == 'provider' else 'OFFLINE'
        if state == 'half_open':
            return 'PROVIDER_PROBING' if scope == 'provider' else 'PROBING'
        if state == 'degraded' and self._success_streak > 0:
            return 'PROVIDER_RECOVERING' if scope == 'provider' else 'RECOVERING'
        if state == 'degraded':
            return 'PROVIDER_DEGRADED' if scope == 'provider' else 'DEGRADED'
        return 'HEALTHY'
    def _winner_locked(self, now: float) -> _SchedulerWaiter | None:
        # Выбирает quality-first waiter с тем же aging/FIFO контрактом, что и per-key scheduler.
        if not self._waiters:
            return None
        aging_seconds = max(1.0, self.start_interval if self.start_interval > 0 else 1.0)
        def effective_priority(waiter):
            # Понижает numeric priority по мере ожидания, не ломая FIFO среди равных.
            age_steps = int(max(0.0, now - waiter.created_at) / aging_seconds)
            return max(0, waiter.priority - age_steps)
        return min(self._waiters.values(), key=lambda waiter: (effective_priority(waiter), waiter.created_at, waiter.ticket))
    def update_base_limits(self, max_inflight: int, start_interval_seconds: float) -> None:
        # Обновляет healthy ceiling и не сбрасывает уже накопленное degraded-состояние.
        new_base_max = max(1, int(max_inflight or 1))
        new_base_interval = max(0.0, float(start_interval_seconds or 0.0))
        with self.lock:
            was_healthy = self._state_locked(self._clock()) == 'healthy'
            self.base_max_inflight = new_base_max
            self.base_start_interval = new_base_interval
            self.max_start_interval = max(new_base_interval, 2.0)
            if was_healthy:
                self.max_inflight = new_base_max
                self.start_interval = new_base_interval
            else:
                self.max_inflight = min(self.max_inflight, new_base_max)
                self.start_interval = max(self.start_interval, new_base_interval)
    async def acquire(self, request_role: str = 'translation', *, is_cancelled=None) -> ApiNetworkPermit:
        # Выдает общий network permit с равномерным start spacing, inflight cap и half-open probe.
        now = self._clock()
        with self.lock:
            ticket = self._next_ticket
            self._next_ticket += 1
            waiter = _SchedulerWaiter(
                ticket=ticket,
                request_role=request_role,
                priority=REQUEST_ROLE_PRIORITIES.get(request_role, REQUEST_ROLE_PRIORITIES['translation']),
                created_at=now,
                queue_depth=len(self._waiters) + 1,
            )
            self._waiters[ticket] = waiter
        acquired = False
        try:
            while True:
                if callable(is_cancelled) and is_cancelled():
                    raise OperationCancelledError('API request отменен во время ожидания network permit.')
                with self.lock:
                    now = self._clock()
                    winner = self._winner_locked(now)
                    state = self._state_locked(now)
                    spacing_ready = self._last_start_time + self.start_interval if self._has_started and self.start_interval > 0 else 0.0
                    circuit_ready = self._circuit_open_until if state == 'open' else 0.0
                    effective_max = 1 if state == 'half_open' else self.max_inflight
                    probe_blocked = state == 'half_open' and self._probe_inflight
                    can_start = state != 'open' and not probe_blocked and self._inflight < effective_max and now >= spacing_ready
                    if winner is not None and winner.ticket == ticket and can_start:
                        self._waiters.pop(ticket, None)
                        permit_id = self._next_permit_id
                        self._next_permit_id += 1
                        is_probe = state == 'half_open'
                        self._inflight += 1
                        self._last_start_time = now
                        self._has_started = True
                        self._probe_inflight = self._probe_inflight or is_probe
                        self._active_permits[permit_id] = is_probe
                        acquired = True
                        return ApiNetworkPermit(
                            permit_id=permit_id,
                            request_role=request_role,
                            waited_seconds=max(0.0, now - waiter.created_at),
                            granted_at=now,
                            inflight_at_grant=self._inflight,
                            max_inflight=effective_max,
                            start_interval_seconds=self.start_interval,
                            state=state,
                            is_probe=is_probe,
                        )
                    if state == 'open':
                        delay = max(0.01, circuit_ready - now)
                    elif winner is not None and winner.ticket == ticket and now < spacing_ready:
                        delay = max(0.01, spacing_ready - now)
                    else:
                        delay = 0.05
                await self._sleep(min(delay, 0.20))
        finally:
            if not acquired:
                with self.lock:
                    self._waiters.pop(ticket, None)
    def finish(self, permit: ApiNetworkPermit | None, outcome: str = 'neutral', *, retry_after: float = 0.0) -> dict:
        # Освобождает inflight slot и адаптирует pacing/circuit state по availability outcome.
        with self.lock:
            now = self._clock()
            previous_state = self._state_locked(now)
            previous_health_state = self._health_state_locked(now)
            if permit is None or permit.permit_id not in self._active_permits:
                return self._snapshot_locked(now, previous_state, False, previous_health_state)
            is_probe = self._active_permits.pop(permit.permit_id)
            self._inflight = max(0, self._inflight - 1)
            if is_probe:
                self._probe_inflight = False
            if outcome in {'network_error', 'provider_error'}:
                self._recent_outcomes.append(1.0)
                if outcome == 'provider_error':
                    self._provider_error_streak += 1
                    self._network_error_streak = 0
                    self._circuit_scope = 'provider'
                    failure_streak = self._provider_error_streak
                else:
                    self._network_error_streak += 1
                    self._provider_error_streak = 0
                    self._circuit_scope = 'network'
                    failure_streak = self._network_error_streak
                self._success_streak = 0
                self.max_inflight = max(1, self.max_inflight // 2)
                next_interval = self.start_interval * 1.7 if self.start_interval > 0 else 0.25
                self.start_interval = min(self.max_start_interval, max(self.base_start_interval, next_interval))
                rolling_unavailable = (
                    len(self._recent_outcomes) >= self.health_min_samples
                    and self._error_ratio_locked() >= self.circuit_error_ratio
                )
                if failure_streak >= self.circuit_threshold or rolling_unavailable:
                    self._circuit_trip_count += 1
                    exponent = max(0, self._circuit_trip_count - 1)
                    raw_delay = min(self.max_circuit_delay, self.circuit_base_delay * (2 ** exponent))
                    if outcome == 'provider_error':
                        raw_delay = min(self.max_circuit_delay, max(raw_delay, max(0.0, float(retry_after or 0.0))))
                    circuit_delay = min(self.max_circuit_delay, max(0.05, float(self._jitter(raw_delay))))
                    self._circuit_open_until = max(self._circuit_open_until, now + circuit_delay)
            elif outcome == 'success':
                self._recent_outcomes.append(0.0)
                self._network_error_streak = 0
                self._provider_error_streak = 0
                if is_probe or now >= self._circuit_open_until:
                    self._circuit_open_until = 0.0
                self._success_streak += 1
                if self._success_streak >= self.recovery_successes:
                    self._success_streak = 0
                    if self.max_inflight < self.base_max_inflight:
                        self.max_inflight += 1
                    if self.start_interval > self.base_start_interval:
                        self.start_interval = max(self.base_start_interval, self.start_interval * 0.80)
                    if len(self._recent_outcomes) >= self.health_min_samples and self._error_ratio_locked() < self.degraded_error_ratio:
                        self._circuit_trip_count = 0
                    elif self._circuit_trip_count > 0:
                        self._circuit_trip_count -= 1
            current_state = self._state_locked(now)
            if current_state == 'healthy':
                self._circuit_scope = None
            current_health_state = self._health_state_locked(now)
            changed = current_state != previous_state or current_health_state != previous_health_state
            return self._snapshot_locked(now, previous_state, changed, previous_health_state)
    def _snapshot_locked(self, now: float, previous_state: str | None = None, state_changed: bool = False, previous_health_state: str | None = None) -> dict:
        # Формирует immutable diagnostics snapshot под уже удерживаемым lock.
        state = self._state_locked(now)
        health_state = self._health_state_locked(now)
        return {
            'state': state,
            'previous_state': previous_state,
            'health_state': health_state,
            'previous_health_state': previous_health_state,
            'state_changed': bool(state_changed),
            'base_max_inflight': self.base_max_inflight,
            'max_inflight': self.max_inflight,
            'inflight': self._inflight,
            'queue_depth': len(self._waiters),
            'start_interval_seconds': self.start_interval,
            'network_error_streak': self._network_error_streak,
            'provider_error_streak': self._provider_error_streak,
            'failure_scope': self._circuit_scope,
            'success_streak': self._success_streak,
            'circuit_trip_count': self._circuit_trip_count,
            'recent_samples': len(self._recent_outcomes),
            'recent_error_ratio': self._error_ratio_locked(),
            'circuit_remaining': max(0.0, self._circuit_open_until - now),
            'probe_inflight': self._probe_inflight,
        }
    def snapshot(self) -> dict:
        # Возвращает thread-safe diagnostics snapshot общего network controller.
        with self.lock:
            now = self._clock()
            return self._snapshot_locked(now)
class ApiNetworkControllerRegistry:
    def __init__(self):
        # Инициализирует owner-scoped registry общих network controller-ов по session identity.
        self.lock = threading.RLock()
        self._controllers = {}
    def get_controller(self, identity: str, max_inflight: int, start_interval_seconds: float) -> ApiNetworkController:
        # Возвращает единый controller и обновляет только его healthy ceiling/configured pacing.
        key = str(identity or 'default')
        with self.lock:
            controller = self._controllers.get(key)
            if controller is None:
                controller = ApiNetworkController(max_inflight, start_interval_seconds)
                self._controllers[key] = controller
                return controller
        controller.update_base_limits(max_inflight, start_interval_seconds)
        return controller
    def snapshot(self) -> dict:
        # Возвращает snapshots всех network controller-ов без mutable state.
        with self.lock:
            items = list(self._controllers.items())
        return {identity: controller.snapshot() for identity, controller in items}
    def clear(self):
        # Удаляет session-scoped network controller state после завершения владельца registry.
        with self.lock:
            self._controllers.clear()
class ApiRequestSchedulerRegistry:
    def __init__(self):
        # Инициализирует thread-safe registry scheduler-ов по provider/key/model identity.
        self.lock = threading.RLock()
        self._schedulers = {}
    def get_scheduler(self, provider_id: str, api_key: str, model_id: str, rpm_limit: int, tpm_limit: int = 0, rps_limit: float = 0.0) -> ApiRequestScheduler:
        # Возвращает единый scheduler bucket и применяет самые строгие configured RPM/RPS/TPM ceilings.
        identity = (str(provider_id or 'unknown'), str(api_key or ''), str(model_id or 'unknown'))
        configured_rpm = max(0, int(rpm_limit or 0))
        configured_tpm = max(0, int(tpm_limit or 0))
        configured_rps = max(0.0, float(rps_limit or 0.0))
        with self.lock:
            scheduler = self._schedulers.get(identity)
            if scheduler is None:
                scheduler = ApiRequestScheduler(configured_rpm, tpm_limit=configured_tpm, rps_limit=configured_rps)
                self._schedulers[identity] = scheduler
                return scheduler
        snapshot = scheduler.snapshot()
        if configured_rpm > 0 and (snapshot['rpm_limit'] <= 0 or configured_rpm < snapshot['rpm_limit']):
            scheduler.set_rpm(configured_rpm)
        if configured_tpm > 0 and (snapshot['tpm_limit'] <= 0 or configured_tpm < snapshot['tpm_limit']):
            scheduler.set_tpm(configured_tpm)
        if configured_rps > 0 and (snapshot['rps_limit'] <= 0 or configured_rps < snapshot['rps_limit']):
            scheduler.set_rps(configured_rps)
        return scheduler
    def snapshot(self) -> dict:
        # Возвращает snapshots всех bucket-ов без выдачи mutable scheduler registry наружу.
        with self.lock:
            items = list(self._schedulers.items())
        return {
            f'{provider_id}|…{api_key[-4:] if api_key else ""}|{model_id}': scheduler.snapshot()
            for (provider_id, api_key, model_id), scheduler in items
        }
    def clear(self):
        # Удаляет session-scoped scheduler buckets после завершения владельца registry.
        with self.lock:
            self._schedulers.clear()
