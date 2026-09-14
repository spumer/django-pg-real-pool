# Configuration

Both engines are configured entirely in `DATABASES`. Pick one per alias.

## Native pool (recommended)

```python
DATABASES = {
    "default": {
        "ENGINE": "django_pg_real_pool",
        "NAME": "mydb",
        "USER": "myuser",
        "PASSWORD": "secret",
        "HOST": "127.0.0.1",
        "PORT": "5432",
        "CONN_MAX_AGE": 0,                 # required: pooling forbids persistent connections
        "OPTIONS": {
            "pool": {"min_size": 2, "max_size": 10},
        },
    }
}
```

`OPTIONS["pool"]` accepts either `True` (use all defaults) or a dict that is passed straight
through to `psycopg_pool.ConnectionPool`.

### Pool options

These are `psycopg_pool.ConnectionPool` constructor arguments (defaults are owned by
`psycopg_pool`, not Django):

| Option | Default | Meaning |
|---|---|---|
| `min_size` | 4 | connections kept warm in the pool |
| `max_size` | `None` | `None`/`== min_size` → fixed size; `> min_size` → grow under load |
| `timeout` | 30.0 | seconds to wait for a free connection before `PoolTimeout` |
| `max_waiting` | 0 | cap on queued requests (0 = unlimited); excess → `TooManyRequests` |
| `max_lifetime` | 3600.0 | recycle age of a connection (seconds) |
| `max_idle` | 600.0 | close idle connections above `min_size` after this many seconds |
| `reconnect_timeout` | 300.0 | retry window before the `reconnect_failed` callback fires |
| `num_workers` | 3 | background threads for opening/maintaining connections |

!!! warning "`CONN_MAX_AGE` must be 0"
    Django's persistent connections and pooling are mutually exclusive. A non-zero `CONN_MAX_AGE`
    raises `ImproperlyConfigured("Pooling doesn't support persistent connections.")`. The pool
    manages connection lifetime itself via `max_lifetime` / `max_idle`.

!!! tip "Health checks"
    Set `"CONN_HEALTH_CHECKS": True` to have Django validate a borrowed connection before reuse
    (wired to `psycopg_pool`'s check mechanism).

## Third-party pool (`dj_db_conn_pool`)

```python
DATABASES = {
    "default": {
        "ENGINE": "django_pg_real_pool.dj_db_conn_pool",
        "NAME": "mydb",
        "USER": "myuser",
        "PASSWORD": "secret",
        "HOST": "127.0.0.1",
        "PORT": "5432",
        "POOL_OPTIONS": {
            "POOL_SIZE": 3,
            "MAX_OVERFLOW": 7,
            "RECYCLE": 24 * 60 * 60,
        },
    }
}
```

`POOL_OPTIONS` keys must be **UPPERCASE** and map onto SQLAlchemy `QueuePool`:

| Key | Effective default | Meaning |
|---|---|---|
| `POOL_SIZE` | 10 | persistent connections kept in the pool |
| `MAX_OVERFLOW` | 10 | extra connections allowed past `POOL_SIZE` (closed after use) |
| `RECYCLE` | 900 | recycle age of a connection (seconds) |
| `TIMEOUT` | 30 | seconds to wait for a connection |
| `PRE_PING` | `True` | test a connection's liveness before handing it out |
| `ECHO` | `False` | log pool checkout/checkin |

!!! note
    `django-db-connection-pool`'s README quotes SQLAlchemy's generic defaults (`pool_size=5`,
    `recycle=-1`); the package actually overrides them to the values above. The package itself pins
    no Django version — this library pins a `>=1.2.6` floor in the `dj-db-conn-pool` extra.

## Mixing pooled and non-pooled aliases

You can keep a non-pooled alias alongside a pooled one — e.g. a dedicated alias for a readiness
probe so a saturated pool never masks a real connectivity check:

```python
DATABASES = {
    "default": {
        "ENGINE": "django_pg_real_pool",
        "CONN_MAX_AGE": 0,
        "OPTIONS": {"pool": {"min_size": 2, "max_size": 10}},
        # ... connection params ...
    },
    "readiness": {
        "ENGINE": "django.db.backends.postgresql",
        # ... same connection params, no pool ...
    },
}
```

## Optional telemetry

See [Pool telemetry](telemetry.md) for opt-in Prometheus metrics, custom names, dynamic labels
and worker-process aggregation. Telemetry settings live outside driver `OPTIONS`.
