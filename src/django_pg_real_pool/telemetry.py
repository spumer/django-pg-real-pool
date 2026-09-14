"""Opt-in Prometheus telemetry for pooled Django backends.

Pool labels describe the configured endpoint, not a physical connection's peer.
Usage and counters update synchronously; pool statistics are sampled in each worker.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

_logger = logging.getLogger('django_pg_real_pool')
_DEFAULTS = {
    'connection_count': ('db_client_connection_count', 'Connections by state.', 'gauge'),
    'connection_size': (
        'db_client_connection_size',
        'Connections managed by the pool, including preparation.',
        'gauge',
    ),
    'connection_max': (
        'db_client_connection_max',
        'Maximum pool capacity; omitted if unlimited.',
        'gauge',
    ),
    'connection_pending_requests': (
        'db_client_connection_pending_requests',
        'Requests waiting in the pool queue.',
        'gauge',
    ),
    'connection_requests': (
        'db_client_connection_requests_total',
        'Connection checkout attempts through this backend.',
        'counter',
    ),
    'connection_errors': (
        'db_client_connection_errors_total',
        'Failed connection checkout attempts.',
        'counter',
    ),
    'connection_timeouts': (
        'db_client_connection_timeouts_total',
        'Connection checkout timeouts.',
        'counter',
    ),
}
_lock = threading.RLock()
_metrics: weakref.WeakKeyDictionary[Any, dict] = weakref.WeakKeyDictionary()
_observers: dict[int, PoolObserver] = {}
_pid = os.getpid()
_thread = None
_wake = threading.Event()


@dataclass(frozen=True)
class LabelContext:
    """Pool context passed to LABELS_PROVIDER; callbacks may run in a sampler thread."""

    alias: str
    backend: str
    metric: str
    server_address: str


def configuration(database):
    """Resolve global configuration and per-alias overrides without importing Prometheus."""
    config = dict(getattr(settings, 'DJANGO_PG_REAL_POOL_TELEMETRY', {}))
    config.update(database.get('TELEMETRY', {}))
    return config if config.get('ENABLED', False) else None


def _client():
    try:
        import prometheus_client
    except ImportError as exc:
        raise ImproperlyConfigured(
            "Pool telemetry requires pip install 'django-pg-real-pool[telemetry]'."
        ) from exc
    return prometheus_client


def validate(config):
    """Validate configuration before acquiring a database connection."""
    _client()
    names = config.get('METRIC_NAMES', {})
    if not isinstance(names, Mapping):
        raise ImproperlyConfigured('Telemetry METRIC_NAMES must be a mapping.')
    if set(names) - _DEFAULTS.keys():
        raise ImproperlyConfigured('Unknown telemetry METRIC_NAMES key.')
    resolved = [names.get(key, value[0]) for key, value in _DEFAULTS.items()]
    if len(set(resolved)) != len(resolved) or any(
        not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z_:][a-zA-Z0-9_:]*', name)
        for name in resolved
    ):
        raise ImproperlyConfigured('Telemetry metric names must be valid and unique.')
    static_labels = config.get('LABELS', {})
    if not isinstance(static_labels, Mapping):
        raise ImproperlyConfigured('Telemetry LABELS must be a mapping.')
    dynamic_labels = config.get('LABEL_NAMES', [])
    if not isinstance(dynamic_labels, Sequence) or isinstance(dynamic_labels, (str, bytes)):
        raise ImproperlyConfigured('Telemetry LABEL_NAMES must be a sequence of strings.')
    labels = list(static_labels) + list(dynamic_labels)
    reserved = {'pool_name', 'server_address', 'state', 'pid'}
    if len(labels) != len(set(labels)) or any(
        not isinstance(name, str)
        or not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*', name)
        or name.startswith('__')
        or name in reserved
        for name in labels
    ):
        raise ImproperlyConfigured('Telemetry labels must be valid, unique and not reserved.')
    interval = config.get('INTERVAL', 1.0)
    if not isinstance(interval, (float, int)) or not 0 < interval < float('inf'):
        raise ImproperlyConfigured('Telemetry INTERVAL must be a finite positive number.')
    provider = config.get('LABELS_PROVIDER')
    if isinstance(provider, str):
        provider = import_string(provider)
    if provider is not None and not callable(provider):
        raise ImproperlyConfigured('Telemetry LABELS_PROVIDER must be callable.')
    return provider


def format_server_address(database):
    """Pair configured hosts with their ports, preserving failover order."""
    hosts = [host.strip() for host in str(database.get('HOST', '') or '').split(',')]
    ports = [port.strip() or '5432' for port in str(database.get('PORT', '') or '').split(',')]
    if len(ports) == 1:
        ports *= len(hosts)
    if len(hosts) != len(ports):
        raise ImproperlyConfigured('Telemetry HOST and PORT lists must have matching lengths.')
    endpoints = []
    for host, port in zip(hosts, ports, strict=True):
        address = f'[{host}]' if ':' in host and not host.startswith(('[', '/')) else host
        endpoints.append(f'{address}:{port}')
    return ','.join(endpoints)


class PoolObserver:
    """One observer per pool instance, shared by the Django thread-local wrappers."""

    def __init__(self, pool, alias, backend, database, config):
        self.pool = weakref.ref(pool)
        self.alias = alias
        self.backend = backend
        self.provider = validate(config)
        self.interval = config.get('INTERVAL', 1.0)
        self.used = 0
        self.pid = os.getpid()
        self.event_children = {}
        self.opened = False
        self.retired = False
        self.registry = config.get('REGISTRY', _client().REGISTRY)
        self.base = {
            'pool_name': str(config.get('POOL_NAME', alias)),
            'server_address': format_server_address(database),
            **{key: str(value) for key, value in config.get('LABELS', {}).items()},
        }
        self.dynamic = tuple(config.get('LABEL_NAMES', ()))
        self.instruments = {}
        self.previous = {}
        self.guard = threading.RLock()
        client = _client()
        registry = self.registry
        cache = _metrics.setdefault(registry, {})
        for key, (default, help_text, kind) in _DEFAULTS.items():
            name = config.get('METRIC_NAMES', {}).get(key, default)
            labels = tuple(sorted(self.base)) + self.dynamic
            if key == 'connection_count':
                labels += ('state',)
            signature = (name, labels, kind)
            if signature not in cache:
                try:
                    factory = client.Gauge if kind == 'gauge' else client.Counter
                    kwargs = {'multiprocess_mode': 'livesum'} if kind == 'gauge' else {}
                    cache[signature] = factory(name, help_text, labels, registry=registry, **kwargs)
                except ValueError as exc:
                    raise ImproperlyConfigured(
                        f'Telemetry metric registration conflict: {name}'
                    ) from exc
            self.instruments[key] = cache[signature]

        self.bind_events()

    def bind_events(self):
        """Resolve event instruments outside the per-query path, retaining working labels on error."""
        try:
            counters = {
                key: self.instruments[key].labels(**self.labels(key))
                for key, (_, _, kind) in _DEFAULTS.items()
                if kind == 'counter'
            }
            self.gauge('connection_count', self.used, state='used')
            self.event_children = {
                **counters,
                'used': self.previous[('connection_count', (('state', 'used'),))],
            }
        except Exception:
            _logger.exception('Failed to resolve pool telemetry labels')

    def labels(self, key):
        """Evaluate pool-level dynamic values; callback failures leave the prior sample intact."""
        values = {**self.base, **dict.fromkeys(self.dynamic, '')}
        if self.provider:
            context = LabelContext(
                self.alias,
                self.backend,
                key,
                self.base['server_address'],
            )
            dynamic = self.provider(context)
            if set(dynamic) - set(self.dynamic):
                raise ValueError('LABELS_PROVIDER returned undeclared labels')
            values.update({key: str(value) for key, value in dynamic.items()})
        return values

    def gauge(self, key, value, **extra):
        """Publish a snapshot and zero any previous label combination."""
        labels = {**self.labels(key), **extra}
        child = self.instruments[key].labels(**labels)
        slot = (key, tuple(extra.items()))
        previous = self.previous.get(slot)
        if previous is not None and previous is not child:
            previous.set(0)
        child.set(value)
        self.previous[slot] = child

    def retire(self):
        """Zero gauges once, before another pool can reuse their time series."""
        if self.pid != os.getpid():
            return
        with self.guard:
            if not self.retired:
                for child in self.previous.values():
                    child.set(0)
                self.retired = True
                _wake.set()

    def refresh(self):
        """Read statistics without opening connections or resetting backend counters."""
        if self.pid != os.getpid():
            return False
        with self.guard:
            if self.retired:
                return False
            pool = self.pool()
            if pool is None or (self.opened and getattr(pool, 'closed', False)):
                self.retire()
                return False
            if getattr(pool, 'closed', False):
                return True  # Django creates native pools with open=False.
            self.opened = True
            self.bind_events()
            if self.backend == 'native':
                stats = pool.get_stats()
                idle = stats.get('pool_available', 0)
                size = stats.get('pool_size', 0)
                maximum = stats.get('pool_max', pool.max_size)
                self.gauge('connection_pending_requests', stats.get('requests_waiting', 0))
            else:
                idle = pool.checkedin()
                size = idle + pool.checkedout()
                # QueuePool has no public maximum-capacity accessor.
                maximum = getattr(pool, '_max_overflow', -1)
                maximum = pool.size() + maximum if maximum >= 0 and pool.size() else None
            self.gauge('connection_count', idle, state='idle')
            self.gauge('connection_size', size)
            if maximum is not None:
                self.gauge('connection_max', maximum)
            return True

    def event(self, change=0, error=None, request=False):
        """Record checkout/return events without allowing telemetry to fail a query."""
        # Inherited wrappers can retain observers after the module's fork reset.
        # Never touch their locks or Prometheus instruments in the child.
        if self.pid != os.getpid():
            return
        with self.guard:
            if self.retired:
                return
            self.used += change
            if change > 0:
                self.opened = True  # A pool may close before its first scheduled sample.
            if not self.event_children:
                return  # The sampler retries a failed initial label callback.
            try:
                self.event_children['used'].set(self.used)
                if request:
                    self.event_children['connection_requests'].inc()
                if error is not None:
                    self.event_children['connection_errors'].inc()
                    from psycopg_pool import PoolTimeout

                    timeout_types = (PoolTimeout,)
                    if self.backend != 'native':
                        from sqlalchemy.exc import TimeoutError as QueueTimeout

                        timeout_types += (QueueTimeout,)
                    if isinstance(error, timeout_types):
                        self.event_children['connection_timeouts'].inc()
            except Exception:
                _logger.exception('Failed to update pool telemetry')


def _sample():
    global _thread  # noqa: PLW0603 - sampler stops atomically with registry inspection
    deadlines = {}
    while True:
        with _lock:
            if not _observers:
                _thread = None
                return
            _wake.clear()
            observers = list(_observers.items())
        for key, observer in observers:
            if not observer.retired and time.monotonic() < deadlines.get(observer, 0):
                continue
            try:
                alive = observer.refresh()
            except Exception:
                _logger.exception('Failed to sample pool telemetry')
                alive = True
            if not alive:
                with _lock:
                    if _observers.get(key) is observer:
                        del _observers[key]
                deadlines.pop(observer, None)
            else:
                deadlines[observer] = time.monotonic() + observer.interval
        with _lock:
            if not _observers:
                _thread = None
                return
            current = set(_observers.values())
            deadlines = {obs: deadline for obs, deadline in deadlines.items() if obs in current}
            delay = max(0, min(deadlines.get(obs, 0) for obs in current) - time.monotonic())
        # Registration/retirement wakes us early; otherwise sleep until the next sample.
        _wake.wait(delay)


def observe(pool, alias, backend, database, config):
    """Register a pool lazily and start a sampler in the current worker process."""
    global _thread  # noqa: PLW0603 - process-local sampler lifecycle
    if _pid != os.getpid():
        _after_fork()
    key = id(pool)
    observer = _observers.get(key)
    if observer is not None and observer.pool() is pool and not observer.retired:
        return observer
    with _lock:
        observer = _observers.get(key)
        if observer is None or observer.pool() is not pool or observer.retired:
            registry = config.get('REGISTRY', _client().REGISTRY)
            for previous in list(_observers.values()):
                if previous.alias == alias and previous.registry is registry:
                    previous.retire()
            observer = PoolObserver(pool, alias, backend, database, MappingProxyType(config))
            _observers[key] = observer
            _wake.set()
        if _thread is None:
            _thread = threading.Thread(target=_sample, name='pg-pool-telemetry', daemon=True)
            _thread.start()
        return observer


def _after_fork():
    global _lock, _pid, _thread, _wake  # noqa: PLW0603 - locks must not survive fork
    _lock = threading.RLock()
    _wake = threading.Event()
    for observer in _observers.values():
        observer.guard = threading.RLock()
        observer.retired = True  # Don't mutate inherited Prometheus instruments.
    _observers.clear()
    _pid = os.getpid()
    _thread = None


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_after_fork)
