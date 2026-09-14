# Pool telemetry

Telemetry is opt-in and supports both pool engines. Install the optional client:

```bash
pip install 'django-pg-real-pool[telemetry]'
# With the SQLAlchemy-based backend:
pip install 'django-pg-real-pool[dj-db-conn-pool,telemetry]'
```

Then enable collection in Django settings:

```python
DJANGO_PG_REAL_POOL_TELEMETRY = {"ENABLED": True}
```

Installing the extra alone does not enable telemetry. When disabled, the backend does
not import `prometheus_client`, register metrics, run callbacks or start a sampler.
When enabled without the extra, creating a database wrapper raises `ImproperlyConfigured`.

## Metrics

| Configuration key | Default Prometheus name | Type | Meaning |
|---|---|---|---|
| `connection_count` | `db_client_connection_count` | Gauge | Connections checked out through this backend (`state="used"`) or available in the pool (`state="idle"`) |
| `connection_size` | `db_client_connection_size` | Gauge | Current size reported by the pool |
| `connection_max` | `db_client_connection_max` | Gauge | Maximum capacity, including SQLAlchemy overflow; absent for an unlimited pool |
| `connection_pending_requests` | `db_client_connection_pending_requests` | Gauge | Requests waiting in the pool queue; native backend only |
| `connection_requests` | `db_client_connection_requests_total` | Counter | Completed checkout attempts, including failures |
| `connection_errors` | `db_client_connection_errors_total` | Counter | Failed checkout attempts |
| `connection_timeouts` | `db_client_connection_timeouts_total` | Counter | Checkout attempts that failed specifically with a pool timeout |

Count, maximum, pending requests and timeouts use names based on
[OpenTelemetry database pool conventions](https://opentelemetry.io/docs/specs/semconv/db/database-metrics/),
with Prometheus formatting. Size, requests and errors are library extensions. These defaults
are part of this library's API; upstream changes to experimental conventions do not
silently rename them.

`used` counts borrowed connections, including connections held by transactions or cursors.
It does not measure SQL queries actively executing on the server. Accessing the underlying
pool directly bypasses checkout counters and `used` tracking.

For psycopg, size includes connections being prepared in background workers. Consequently,
`used + idle` can be smaller than size. SQLAlchemy size is checked-in plus checked-out
connections, not its configured persistent capacity. No SQL queries are issued to collect metrics.

All metrics describe local application pools, not PgBouncer's internal pool or PostgreSQL's
server-wide connection count. Before the first connection attempt there are no pool samples.
Used connections and checkout counters update synchronously on checkout/return using
cached metric handles, without reading pool statistics or invoking the labels provider.
Idle connections, size, capacity and queue depth are sampled approximately every `INTERVAL`
seconds (default `1.0`). Any finite positive interval is supported; scheduling and collection
cost still limit actual precision. Samples are not an atomic snapshot across different metrics.
The daemon sampler sleeps until the next pool's deadline, wakes on registration/retirement,
and exits once no observed pools remain. New registrations restart it lazily.

## Labels and configuration

Default labels:

- `pool_name`: Django database alias, overridable with `POOL_NAME`.
- `server_address`: configured endpoints as `host:port`, for example
  `pg1.host:5432,pg2.host:3322`. Hosts and ports are paired in their configured order.
  A single port applies to all hosts; empty ports default to `5432`. Whitespace is
  trimmed, and IPv6 addresses are bracketed (`[::1]:5432`).
- `state`: `used` or `idle`, only on `connection_count`.

`server_address` identifies the configured endpoint, not the physical server selected by
failover. For a PgBouncer endpoint it is the PgBouncer address. An empty `HOST` produces `:5432` (or the configured port); connection parameters supplied through service files or environment variables
are not resolved. Passwords and connection strings are never added as labels.

```python
DJANGO_PG_REAL_POOL_TELEMETRY = {
    "ENABLED": True,
    "INTERVAL": 1.0,
    "METRIC_NAMES": {
        "connection_count": "myapp_pool_connections",
        "connection_max": "myapp_pool_capacity",
    },
    "LABELS": {"service": "billing"},
    "LABEL_NAMES": ["cluster"],
    "LABELS_PROVIDER": "myapp.telemetry.pool_labels",
}

# Overrides global options for this alias. Nested dictionaries are replaced.
DATABASES["default"]["TELEMETRY"] = {"POOL_NAME": "billing-pgbouncer"}
DATABASES["replica"]["TELEMETRY"] = {"ENABLED": False}
```

```python
# myapp/telemetry.py
from django_pg_real_pool.telemetry import LabelContext


def pool_labels(context: LabelContext):
    # Read your application's thread-safe, in-memory configuration here.
    return {"cluster": "primary" if context.alias == "default" else "replica"}
```

The provider can be a callable or a dotted import path. `LabelContext` contains `alias`,
`backend` (`native` or `dj_db_conn_pool`), `metric` (the configuration key), `server_address`. It deliberately contains neither credentials nor database connections.

Declare dynamic label **names** in `LABEL_NAMES`; their **values** can change at runtime.
Missing dynamic values become empty strings. Undeclared names are rejected. Providers run
during initial registration and in the sampler thread: they must be fast, thread-safe, and independent
of request-local state. Do not execute database queries from a provider. Callback exceptions
are logged without failing database operations; subsequent updates retry the callback.

Dynamic value changes take effect on the next successful sampler refresh. The previous
gauge series is set to zero and the current pool snapshot is published under the new values.
Counters retain history under old values and count future events under the refreshed values.
If refreshing event labels fails, existing handles remain usable; an initial callback failure
suppresses event samples until a successful refresh (the internal used count is retained). Use bounded values (for example cluster or deployment
role), not user IDs or request IDs: historical label combinations remain registered.

Configuration is read when wrappers are constructed. Names, registry, static labels and
enabling/disabling telemetry are startup settings; use the provider for runtime value changes.
Pool names must uniquely identify pools in a registry. Aliases sharing a metric name must
use the same label-name schema. Reserved labels cannot be supplied through `LABELS` or
`LABEL_NAMES`. Invalid configuration and metric registration conflicts raise
`ImproperlyConfigured` rather than silently dropping metrics.

## Exposing metrics

By default metrics use `prometheus_client.REGISTRY`. If your application already exposes
that registry, pool metrics appear on its existing endpoint. The library does not start an
HTTP server or add a Django URL automatically.

A minimal single-process Django endpoint:

```python
from django.http import HttpResponse
from django.urls import path
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest


def metrics(request):
    return HttpResponse(generate_latest(), content_type=CONTENT_TYPE_LATEST)


urlpatterns = [path("metrics", metrics)]
```

For a custom single-process registry, pass its object as `REGISTRY` in the telemetry
configuration and use the same registry in `generate_latest(registry)`.

## Gunicorn and other multiprocess workers

Metrics use ordinary Prometheus gauges and counters, including `multiprocess_mode="livesum"`
for gauges. Each worker samples its own pool; the multiprocess collector sums worker values.
For example, four workers each configured with capacity 10 report total capacity 40.

Follow the [Prometheus Python multiprocess setup](https://prometheus.github.io/client_python/multiprocess/):

1. Set `PROMETHEUS_MULTIPROC_DIR` to an existing writable directory **before starting Python**.
   Use a fresh/empty directory for each application run; never clear it while workers are running.
2. Create the metrics endpoint's registry separately from the registry used for instrumentation:

   ```python
   from django.http import HttpResponse
   from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest, multiprocess

   def metrics(request):
       registry = CollectorRegistry()
       multiprocess.MultiProcessCollector(registry)
       return HttpResponse(generate_latest(registry), content_type=CONTENT_TYPE_LATEST)
   ```

3. Notify the client when a worker exits. For Gunicorn:

   ```python
   # gunicorn.conf.py
   from prometheus_client import multiprocess

   def child_exit(server, worker):
       multiprocess.mark_process_dead(worker.pid)
   ```

Other process managers, including Celery, need equivalent worker-exit cleanup. Without it,
terminated workers' gauge values remain in the aggregate. Counters retain completed workers'
history. Do not create/open database pools before forking; each worker must create its own pool.
Custom registries do not provide isolation in Prometheus multiprocess mode.

Inherited telemetry observers are retired after fork; closing a wrapper inherited from the
parent does not acquire its old telemetry locks or update its metrics. This does not make
inherited database connections safe to use.
