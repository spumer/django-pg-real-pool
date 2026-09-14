import builtins
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from django.core.exceptions import ImproperlyConfigured
from prometheus_client import CollectorRegistry
from psycopg_pool import PoolTimeout

from django_pg_real_pool._telemetry import TelemetryMixin
from django_pg_real_pool.telemetry import PoolObserver, configuration, validate


class NativePool:
    closed = False
    max_size = 10

    def __init__(self):
        self.stats = {'pool_size': 4, 'pool_available': 2, 'requests_waiting': 1}

    def get_stats(self):
        return self.stats.copy()


@pytest.fixture
def registry():
    return CollectorRegistry()


@pytest.fixture
def observer(registry):
    pool = NativePool()
    observer = PoolObserver(
        pool,
        'default',
        'native',
        {'HOST': 'pg-1, pg-2', 'PORT': '6432'},
        {'ENABLED': True, 'REGISTRY': registry},
    )
    return observer, pool


def sample(registry, name, **extra):
    return registry.get_sample_value(
        name,
        {
            'pool_name': 'default',
            'server_address': 'pg-1:6432,pg-2:6432',
            **extra,
        },
    )


def test_usage_does_not_count_connections_being_prepared(observer, registry):
    obs, _ = observer
    obs.event(change=1, request=True)
    assert sample(registry, 'db_client_connection_count', state='used') == 1
    obs.refresh()  # Pool statistics are sampled separately from checkout events.
    assert sample(registry, 'db_client_connection_count', state='idle') == 2
    assert sample(registry, 'db_client_connection_size') == 4
    assert sample(registry, 'db_client_connection_max') == 10
    assert sample(registry, 'db_client_connection_pending_requests') == 1
    obs.event(change=-1)
    assert sample(registry, 'db_client_connection_count', state='used') == 0


def test_events_do_not_read_stats_or_evaluate_labels(observer, monkeypatch):
    obs, pool = observer
    obs.refresh()
    stats = Mock(side_effect=AssertionError('stats on hot path'))
    labels = Mock(side_effect=AssertionError('callback on hot path'))
    monkeypatch.setattr(pool, 'get_stats', stats)
    monkeypatch.setattr(obs, 'labels', labels)
    obs.event(change=1, request=True)
    obs.event(change=-1)
    assert obs.used == 0
    stats.assert_not_called()
    labels.assert_not_called()


def test_errors_are_not_all_timeouts(observer, registry):
    obs, _ = observer
    obs.event(error=ValueError('failed'), request=True)
    obs.event(error=PoolTimeout('exhausted'), request=True)
    assert sample(registry, 'db_client_connection_requests_total') == 2
    assert sample(registry, 'db_client_connection_errors_total') == 2
    assert sample(registry, 'db_client_connection_timeouts_total') == 1


def test_closed_pool_zeros_gauges(observer, registry):
    obs, pool = observer
    obs.refresh()
    pool.closed = True
    assert obs.refresh() is False
    assert sample(registry, 'db_client_connection_size') == 0
    assert sample(registry, 'db_client_connection_max') == 0


def test_dynamic_labels_and_renaming(registry):
    pool = NativePool()
    current = {'cluster': 'blue'}
    obs = PoolObserver(
        pool,
        'default',
        'native',
        {'HOST': 'pg-1,pg-2', 'PORT': '6432'},
        {
            'REGISTRY': registry,
            'METRIC_NAMES': {'connection_count': 'custom_connections'},
            'LABELS': {'service': 'billing'},
            'LABEL_NAMES': ['cluster'],
            'LABELS_PROVIDER': lambda context: current,
        },
    )
    obs.event(change=1)
    assert (
        sample(registry, 'custom_connections', state='used', service='billing', cluster='blue') == 1
    )
    current['cluster'] = 'green'
    obs.refresh()
    assert (
        sample(registry, 'custom_connections', state='used', service='billing', cluster='blue') == 0
    )
    assert (
        sample(registry, 'custom_connections', state='used', service='billing', cluster='green')
        == 1
    )
    obs.event(change=-1)
    assert (
        sample(registry, 'custom_connections', state='used', service='billing', cluster='green')
        == 0
    )


def test_callback_failure_does_not_break_checkout(observer, caplog):
    obs, _ = observer
    obs.provider = lambda context: {'undeclared': 'bad'}
    obs.bind_events()
    obs.event(change=1)
    assert obs.used == 1
    assert 'Failed to resolve pool telemetry labels' in caplog.text
    obs.provider = None
    obs.event(change=-1)
    assert obs.used == 0


def test_threaded_usage(observer, registry):
    obs, _ = observer
    barrier = threading.Barrier(4)

    def use():
        obs.event(change=1, request=True)
        barrier.wait(timeout=5)
        obs.event(change=-1)

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: use(), range(4)))
    assert sample(registry, 'db_client_connection_count', state='used') == 0
    assert sample(registry, 'db_client_connection_requests_total') == 4


@pytest.mark.parametrize(
    'config',
    [
        {'METRIC_NAMES': {'unknown': 'foo'}},
        {'METRIC_NAMES': {'connection_count': 'invalid-name'}},
        {'METRIC_NAMES': []},
        {'METRIC_NAMES': {'connection_count': []}},
        {'LABELS': {'state': 'reserved'}},
        {'LABELS': ['service']},
        {'LABELS': {1: 'value'}},
        {'LABEL_NAMES': ['cluster', 'cluster']},
        {'LABEL_NAMES': 'cluster'},
        {'LABEL_NAMES': [1]},
        {'LABEL_NAMES': [['cluster']]},
        {'INTERVAL': 0},
        {'INTERVAL': float('nan')},
        {'LABELS_PROVIDER': 123},
    ],
)
def test_invalid_configuration(config):
    with pytest.raises(ImproperlyConfigured):
        validate(config)


def test_opt_in_and_alias_override(settings):
    assert configuration({}) is None
    settings.DJANGO_PG_REAL_POOL_TELEMETRY = {'ENABLED': True, 'LABELS': {'service': 'test'}}
    assert configuration({})['ENABLED']
    assert configuration({'TELEMETRY': {'ENABLED': False}}) is None


def test_no_prometheus_needed_when_disabled(settings, monkeypatch):
    from django_pg_real_pool.native.base import DatabaseWrapper

    original = builtins.__import__

    def without_prometheus(name, *args, **kwargs):
        if name.startswith('prometheus_client'):
            raise ImportError('intentionally missing')
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', without_prometheus)
    database = {**settings.DATABASES['default'], 'TELEMETRY': {'ENABLED': False}}
    DatabaseWrapper(database)
    database['TELEMETRY']['ENABLED'] = True
    with pytest.raises(ImproperlyConfigured, match=r'\[telemetry\]'):
        DatabaseWrapper(database)


def test_registration_shared_across_aliases(registry):
    pools = [NativePool(), NativePool()]
    observers = [
        PoolObserver(pool, alias, 'native', {}, {'REGISTRY': registry})
        for pool, alias in zip(pools, ['default', 'replica'], strict=True)
    ]
    for obs in observers:
        obs.refresh()
    samples = [
        s
        for metric in registry.collect()
        for s in metric.samples
        if s.name == 'db_client_connection_max'
    ]
    assert {s.labels['pool_name'] for s in samples} == {'default', 'replica'}


def test_queuepool_size_and_overflow(registry):
    queue_pool = pytest.importorskip('sqlalchemy.pool').QueuePool

    class Connection:
        def rollback(self):
            pass

        def close(self):
            pass

    pool = queue_pool(Connection, pool_size=1, max_overflow=1)
    obs = PoolObserver(
        pool,
        'default',
        'dj_db_conn_pool',
        {'HOST': 'pg-1,pg-2', 'PORT': '6432'},
        {'REGISTRY': registry},
    )
    first, second = pool.connect(), pool.connect()
    obs.event(change=2)
    obs.refresh()
    assert sample(registry, 'db_client_connection_size') == 2
    assert sample(registry, 'db_client_connection_max') == 2
    first.close()
    second.close()
    obs.event(change=-2)
    obs.refresh()
    assert sample(registry, 'db_client_connection_size') == 1
    assert sample(registry, 'db_client_connection_pending_requests') is None
    pool.dispose()


def test_failed_close_releases_accounting_and_recovers(observer, registry):
    obs, _ = observer

    class Base:
        fail = True

        def _close(self):
            if self.fail:
                raise RuntimeError('putconn blew up')

    class Wrapper(TelemetryMixin, Base):
        def __init__(self):
            self._telemetry_observer = obs

    wrapper = Wrapper()
    obs.event(change=1)
    with pytest.raises(RuntimeError, match='putconn blew up'):
        wrapper._close()
    assert wrapper._telemetry_observer is None
    assert obs.used == 0
    assert sample(registry, 'db_client_connection_count', state='used') == 0
    wrapper.fail = False
    wrapper._telemetry_observer = obs
    obs.event(change=1)
    wrapper._close()
    assert obs.used == 0
    assert sample(registry, 'db_client_connection_count', state='used') == 0


def test_registration_failure_does_not_replace_checkout_error():
    original = OSError('database unavailable')

    class Base:
        def get_new_connection(self, params):
            raise original

    class Wrapper(TelemetryMixin, Base):
        def __init__(self):
            self._telemetry_config = {'ENABLED': True}
            self.alias = 'default'
            self._pool_observer = Mock(side_effect=[None, ImproperlyConfigured('duplicate metric')])

    with pytest.raises(OSError, match='database unavailable') as caught:
        Wrapper().get_new_connection({})
    assert caught.value is original


@pytest.mark.django_db(transaction=True)
def test_real_backend_usage_and_duplicate_close(connection, settings, registry, monkeypatch):
    connection.close()
    from django_pg_real_pool import telemetry

    monkeypatch.setattr(telemetry, '_observers', {})
    old = connection._telemetry_config
    connection._telemetry_config = {'ENABLED': True, 'REGISTRY': registry}
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            labels = {
                'pool_name': connection.alias,
                'server_address': f'{settings.DATABASES["default"]["HOST"]}:{settings.DATABASES["default"]["PORT"]}',
                'state': 'used',
            }
            assert registry.get_sample_value('db_client_connection_count', labels) == 1
        assert registry.get_sample_value('db_client_connection_count', labels) == 0
        connection.close()
        assert registry.get_sample_value('db_client_connection_count', labels) == 0

        from django.db import transaction

        for rollback in (False, True):
            with transaction.atomic():
                assert registry.get_sample_value('db_client_connection_count', labels) == 1
                transaction.set_rollback(rollback)
            assert registry.get_sample_value('db_client_connection_count', labels) == 0

        with connection.cursor():
            observer = connection._telemetry_observer
            pool = observer.pool()
            if connection.telemetry_backend == 'native':
                from django.db import OperationalError

                expected_error = OperationalError
                monkeypatch.setattr(pool, 'timeout', 0.01)
            else:
                from sqlalchemy.exc import TimeoutError as QueueTimeout

                expected_error = QueueTimeout
                monkeypatch.setattr(pool, '_timeout', 0.01)
            contender = type(connection)(connection.settings_dict, alias=connection.alias)
            contender._telemetry_config = connection._telemetry_config
            with pytest.raises(expected_error):
                contender.ensure_connection()
            assert registry.get_sample_value('db_client_connection_count', labels) == 1
            counter_labels = {key: value for key, value in labels.items() if key != 'state'}
            assert (
                registry.get_sample_value('db_client_connection_timeouts_total', counter_labels)
                == 1
            )
        assert registry.get_sample_value('db_client_connection_count', labels) == 0
    finally:
        connection.close()
        connection._telemetry_config = old


def test_unopened_pool_stays_registered(observer):
    obs, pool = observer
    pool.closed = True
    assert obs.refresh() is True
    pool.closed = False
    assert obs.refresh() is True
    pool.closed = True
    assert obs.refresh() is False


def test_pool_closed_before_first_sample_is_retired(observer):
    obs, pool = observer
    obs.event(change=1)  # A checkout opens the pool even before the first sample.
    obs.event(change=-1)
    pool.closed = True
    assert obs.refresh() is False
    assert obs.retired


def test_sampler_updates_without_queries(registry):
    import time

    from django_pg_real_pool.telemetry import observe

    pool = NativePool()
    obs = observe(pool, 'background', 'native', {}, {'REGISTRY': registry, 'INTERVAL': 0.01})
    labels = {'pool_name': 'background', 'server_address': ':5432'}
    deadline = time.monotonic() + 3

    def wait_for(size):
        while registry.get_sample_value('db_client_connection_size', labels) != size:
            assert time.monotonic() < deadline
            time.sleep(0.01)

    wait_for(4)
    pool.stats['pool_size'] = 7
    wait_for(7)  # Every sample re-reads the pool instead of republishing a cached snapshot.
    pool.closed = True
    obs.refresh()


def test_sampler_honors_short_intervals_and_exits_when_empty():
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent("""
        from unittest.mock import Mock
        from django_pg_real_pool import telemetry
        now = [0.0]
        telemetry.time.monotonic = lambda: now[0]
        observer = Mock(retired=False, interval=0.01)
        observer.refresh.side_effect = [True, False]
        telemetry._observers = {1: observer}
        delays = []
        def wait(delay):
            delays.append(delay)
            now[0] += delay
        telemetry._wake = Mock()
        telemetry._wake.wait.side_effect = wait
        telemetry._thread = object()
        telemetry._sample()
        assert delays == [0.01], delays
        assert not telemetry._observers
        assert telemetry._thread is None
    """)
    subprocess.run([sys.executable, '-c', script], check=True, timeout=5)


def test_sampler_wakes_for_registration_and_restarts_after_retirement():
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent("""
        import threading
        from prometheus_client import CollectorRegistry
        from django_pg_real_pool import telemetry
        class Pool:
            closed = False
            max_size = 1
            def __init__(self):
                self.sampled = threading.Event()
            def get_stats(self):
                self.sampled.set()
                return {'pool_size': 1, 'pool_available': 1}
        registry = CollectorRegistry()
        pools = [Pool(), Pool(), Pool()]
        config = {'REGISTRY': registry, 'INTERVAL': 3600}
        first = telemetry.observe(pools[0], 'first', 'native', {}, config)
        assert pools[0].sampled.wait(2)
        thread = telemetry._thread
        second = telemetry.observe(pools[1], 'second', 'native', {}, config)
        assert pools[1].sampled.wait(2)  # Wakes even if the other deadline is an hour away.
        first.retire()
        second.retire()
        thread.join(2)
        assert not thread.is_alive()
        assert telemetry._thread is None
        third = telemetry.observe(pools[2], 'third', 'native', {}, config)
        assert pools[2].sampled.wait(2)
        restarted = telemetry._thread
        assert restarted is not thread
        third.retire()
        restarted.join(2)
        assert not restarted.is_alive()
    """)
    subprocess.run([sys.executable, '-c', script], check=True, timeout=10)


def test_existing_observer_lookup_does_not_lock(registry, monkeypatch):
    from django_pg_real_pool import telemetry

    pool = NativePool()
    # An isolated observer map keeps the lookup out of the sampler's way.
    obs = PoolObserver(pool, 'default', 'native', {}, {'REGISTRY': registry})
    monkeypatch.setattr(telemetry, '_observers', {id(pool): obs})
    guard = Mock()
    guard.__enter__ = Mock(side_effect=AssertionError('global lock on hot path'))
    guard.__exit__ = Mock()
    monkeypatch.setattr(telemetry, '_lock', guard)
    assert telemetry.observe(pool, 'default', 'native', {}, {}) is obs


def test_multiprocess_aggregation_and_worker_cleanup(tmp_path):
    import json
    import subprocess
    import sys
    import textwrap

    worker = textwrap.dedent("""
        import os
        from prometheus_client import CollectorRegistry
        from django_pg_real_pool.telemetry import PoolObserver
        class Pool:
            closed = False
            max_size = 3
            def get_stats(self):
                return {'pool_size': 2, 'pool_available': 1}
        pool = Pool()
        observer = PoolObserver(pool, 'default', 'native', {'HOST': 'pg'}, {})
        observer.event(change=1, request=True)
        print(os.getpid())
    """)
    env = {**os.environ, 'PROMETHEUS_MULTIPROC_DIR': str(tmp_path)}
    pids = [
        int(subprocess.check_output([sys.executable, '-c', worker], env=env, text=True))
        for _ in range(2)
    ]
    reader = textwrap.dedent("""
        import json, sys
        from prometheus_client import CollectorRegistry, multiprocess
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        labels = {'pool_name': 'default', 'server_address': 'pg:5432', 'state': 'used'}
        before = registry.get_sample_value('db_client_connection_count', labels)
        multiprocess.mark_process_dead(int(sys.argv[1]))
        after = registry.get_sample_value('db_client_connection_count', labels)
        labels.pop('state')
        requests = registry.get_sample_value('db_client_connection_requests_total', labels)
        print(json.dumps([before, after, requests]))
    """)
    result = subprocess.check_output(
        [sys.executable, '-c', reader, str(pids[0])], env=env, text=True
    )
    assert json.loads(result) == [2, 1, 2]


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='requires fork')
def test_fork_with_observer_guards_held_by_another_thread():
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent("""
        import os, signal, threading, warnings
        from prometheus_client import CollectorRegistry
        from django_pg_real_pool import telemetry
        from django_pg_real_pool._telemetry import TelemetryMixin
        class Pool:
            pass
        class Base:
            def _close(self):
                pass
        class Wrapper(TelemetryMixin, Base):
            def __init__(self, observer):
                self._telemetry_observer = observer
        pools = [Pool(), Pool()]
        observers = [telemetry.PoolObserver(p, str(i), 'native', {},
                     {'REGISTRY': CollectorRegistry()}) for i, p in enumerate(pools)]
        wrappers = [Wrapper(obs) for obs in observers]
        # Include a wrapper-held observer no longer present in the module registry.
        telemetry._observers[id(pools[0])] = observers[0]
        ready, release = threading.Event(), threading.Event()
        def hold():
            with observers[0].guard, observers[1].guard:
                ready.set()
                release.wait(5)
        thread = threading.Thread(target=hold)
        thread.start()
        assert ready.wait(2)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)
            pid = os.fork()
        if pid == 0:
            signal.alarm(2)
            for wrapper in wrappers:
                wrapper._close()
                assert wrapper._telemetry_observer is None
            os._exit(0)
        try:
            _, status = os.waitpid(pid, 0)
            assert os.waitstatus_to_exitcode(status) == 0, status
        finally:
            release.set()
            thread.join(2)
    """)
    subprocess.run([sys.executable, '-c', script], check=True, timeout=10)


def test_recreated_pool_cannot_be_zeroed_by_retired_observer(registry):
    from django_pg_real_pool.telemetry import observe

    first, second = NativePool(), NativePool()
    original = observe(first, 'recreated', 'native', {}, {'REGISTRY': registry})
    original.refresh()
    replacement = observe(second, 'recreated', 'native', {}, {'REGISTRY': registry})
    replacement.event(change=1)
    assert original.refresh() is False
    labels = {
        'pool_name': 'recreated',
        'server_address': ':5432',
        'state': 'used',
    }
    assert registry.get_sample_value('db_client_connection_count', labels) == 1
    replacement.retire()


@pytest.mark.django_db(transaction=True)
def test_telemetry_enabled_before_first_pool_creation(connection, registry):
    import uuid

    alias = 'telemetry_' + uuid.uuid4().hex
    database = {**connection.settings_dict, 'TELEMETRY': {'ENABLED': True, 'REGISTRY': registry}}
    wrapper = type(connection)(database, alias=alias)
    try:
        with wrapper.cursor() as cursor:
            cursor.execute('SELECT 1')
            assert wrapper._telemetry_observer.used == 1
        labels = {
            'pool_name': alias,
            'server_address': f'{database["HOST"]}:{database["PORT"]}',
            'state': 'used',
        }
        assert registry.get_sample_value('db_client_connection_count', labels) == 0
    finally:
        wrapper.close()
        if wrapper.telemetry_backend == 'native':
            wrapper.close_pool()
        else:
            from dj_db_conn_pool.core import pool_container

            pool = pool_container.pop(alias, None)
            if pool:
                pool.dispose()


@pytest.mark.parametrize(
    ('database', 'expected'),
    [
        ({'HOST': 'pg1.host,pg2.host', 'PORT': '5432,3322'}, 'pg1.host:5432,pg2.host:3322'),
        ({'HOST': ' pg1.host , pg2.host ', 'PORT': '6432'}, 'pg1.host:6432,pg2.host:6432'),
        ({'HOST': 'pg1,pg2', 'PORT': ',3322'}, 'pg1:5432,pg2:3322'),
        ({'HOST': 'pg1,pg2'}, 'pg1:5432,pg2:5432'),
        ({'HOST': '::1,2001:db8::1', 'PORT': '5432,3322'}, '[::1]:5432,[2001:db8::1]:3322'),
        ({'HOST': '/run/postgresql', 'PORT': 5432}, '/run/postgresql:5432'),
        ({}, ':5432'),
    ],
)
def test_combined_server_label(database, expected, registry):
    pool = NativePool()
    observer = PoolObserver(pool, 'default', 'native', database, {'REGISTRY': registry})
    observer.refresh()
    labels = {'pool_name': 'default', 'server_address': expected}
    assert registry.get_sample_value('db_client_connection_max', labels) == 10


def test_mismatched_host_and_port_lists():
    from django_pg_real_pool.telemetry import format_server_address

    with pytest.raises(ImproperlyConfigured, match='matching lengths'):
        format_server_address({'HOST': 'pg1,pg2', 'PORT': '5432,5433,5434'})
