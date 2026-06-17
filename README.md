# django-pg-real-pool

[![Documentation](https://img.shields.io/badge/docs-mkdocs--material-526CFE)](https://spumer.github.io/django-pg-real-pool/)
[![Coverage](https://codecov.io/gh/spumer/django-pg-real-pool/graph/badge.svg)](https://codecov.io/gh/spumer/django-pg-real-pool)

A Django PostgreSQL database backend that releases pooled connections **As Soon As Possible**.

## TL;DR

A plain connection pool hands a connection to a thread and keeps it checked out for the
whole request/context — even if a single query was made early and the rest of the work
never touches the database. Under load, threads fight over a small set of connections that
mostly sit idle while still "in use".

`django-pg-real-pool` returns the connection to the pool **right after each query** when you
are *not* inside a transaction, and holds it only for the duration of an `atomic()` block (or
a manually controlled transaction). The result: far fewer connections are needed to serve the
same concurrency.

```python
# settings.py
DATABASES = {
    "default": {
        "ENGINE": "django_pg_real_pool",          # native psycopg pool (Django 5.1+)
        "NAME": "mydb", "USER": "...", "PASSWORD": "...", "HOST": "...", "PORT": "5432",
        "CONN_MAX_AGE": 0,                          # pooling forbids persistent connections
        "OPTIONS": {"pool": {"min_size": 2, "max_size": 10}},
    }
}
```

That's it. Your code uses the ORM exactly as before; connections are just held for less time.

## Motivation

When you run a multi-threaded Django app (Gunicorn threads, dramatiq workers, Celery
prefork+threads, ASGI, …) every thread normally keeps its own DB connection for the lifetime
of the request. With a pool in front, the pool still won't reclaim a connection until the
caller releases it — which Django does only at the end of the request. So a request that does
one quick `SELECT` and then spends 200ms calling an external API keeps a database connection
pinned that entire time.

`django-pg-real-pool` changes the release point: the connection goes back to the pool after
each cursor finishes, except inside transactions where it must stay (correctness first). The
connection is transparently re-acquired the next time the ORM needs it.

## How it works

The package is one small mixin (`ConnectionReleaseMixin`) layered on top of a pooled
PostgreSQL `DatabaseWrapper`:

- After a cursor created outside an `atomic()` block is closed, the connection is returned to
  the pool (`AutoConnectionReleaseCursor`).
- `commit()` / `rollback()` return the connection to the pool when the transaction ends.
- Inside an `atomic()` block the connection is held until the block exits — so transactions,
  savepoints, server-side cursors and `on_commit` hooks all behave exactly as Django expects.

Because both Django's native pool and the third-party pool return the connection to the pool on
`close()`, the same mixin works on top of either.

## Pool backends

| Engine | Pool | Requires | Install |
|---|---|---|---|
| `django_pg_real_pool` (default) | Django native (`psycopg_pool`) | Django ≥ 5.1, psycopg 3 | `pip install django-pg-real-pool` |
| `django_pg_real_pool.dj_db_conn_pool` | [`django-db-connection-pool`](https://github.com/altairbow/django-db-connection-pool) (SQLAlchemy `QueuePool`) | any supported Django | `pip install 'django-pg-real-pool[dj-db-conn-pool]'` |

### Native pool (recommended, default)

Uses Django's built-in connection pool (added in **Django 5.1**), which is backed by
`psycopg_pool.ConnectionPool`. The `OPTIONS["pool"]` dict is passed straight through as
`ConnectionPool` keyword arguments:

```python
"OPTIONS": {
    "pool": {
        "min_size": 2,        # connections kept warm
        "max_size": 10,       # hard ceiling
        "timeout": 10,        # seconds to wait for a free connection
        "max_lifetime": 3600, # recycle age
        "max_idle": 600,      # close idle connections above min_size
    },
}
```

Requirements / rules:

- **psycopg 3** with the pool extra: `pip install 'psycopg[pool]'` (a dependency of this
  package; you may prefer `psycopg[binary,pool]`). psycopg2 does **not** support the native pool.
- **`CONN_MAX_AGE` must be `0`** — pooling and Django persistent connections are mutually
  exclusive (the pool manages connection lifetime via `max_lifetime`/`max_idle`).
- Use `"pool": True` to accept all `psycopg_pool` defaults.

### Third-party pool (optional)

If you cannot use psycopg 3 / Django 5.1, or you already standardised on
`django-db-connection-pool`, install the extra and point `ENGINE` at the sub-backend:

```python
"ENGINE": "django_pg_real_pool.dj_db_conn_pool",
"POOL_OPTIONS": {"POOL_SIZE": 3, "MAX_OVERFLOW": 7, "RECYCLE": 86400},
```

`POOL_OPTIONS` keys (uppercase): `POOL_SIZE`, `MAX_OVERFLOW`, `RECYCLE`, `TIMEOUT`,
`PRE_PING`, `ECHO`. Effective defaults when omitted: `pool_size=10`, `max_overflow=10`,
`recycle=900`, `timeout=30`, `pre_ping=True`.

## When (not) to use it

Good fit:

- Multi-threaded apps where DB work is bursty and interleaved with non-DB work.
- High concurrency against a database (or PgBouncer) with a limited connection budget.

Think twice:

- If most of your request time *is* spent in the database, the win is small.
- The trade-off is more `getconn`/`putconn` churn and re-acquisition between queries. For the
  native pool this is cheap; measure if you are latency-sensitive.

## Compatibility

- **Python**: 3.10 – 3.14
- **Django**: 5.1, 5.2 LTS, 6.0 (native pool). The third-party backend also works on older Django.
- **PostgreSQL**: any supported by your Django/psycopg version.

> Note: Django 5.1 is EOL upstream; it is supported here because it introduced the native pool.
> Prefer 5.2 LTS or newer for production.

## Install

```bash
pip install django-pg-real-pool
# or, for the third-party SQLAlchemy-based pool:
pip install 'django-pg-real-pool[dj-db-conn-pool]'
```

## Usage

Set the `ENGINE` in `settings.py` — no code changes are needed beyond that.

```python
# Native pool (default, Django 5.1+):
DATABASES = {
    "default": {
        "ENGINE": "django_pg_real_pool",
        "NAME": "mydb", "USER": "...", "PASSWORD": "...", "HOST": "...", "PORT": "5432",
        "CONN_MAX_AGE": 0,
        "OPTIONS": {"pool": {"min_size": 2, "max_size": 10}},
    }
}

# Third-party pool (optional extra):
DATABASES = {
    "default": {
        "ENGINE": "django_pg_real_pool.dj_db_conn_pool",
        "NAME": "mydb", "USER": "...", "PASSWORD": "...", "HOST": "...", "PORT": "5432",
        "POOL_OPTIONS": {"POOL_SIZE": 3, "MAX_OVERFLOW": 7, "RECYCLE": 86400},
    }
}
```

Runnable snippets live in [`examples/`](examples/). Full documentation — pool options,
compatibility matrix, internals and FAQ — is in [`docs/`](docs/docs/index.md)
(rendered at <https://spumer.github.io/django-pg-real-pool/>).

## Development

```bash
uv sync                                   # create venv and install dev deps
docker compose up -d                      # start PostgreSQL on host port 5433
PGPORT=5433 uv run pytest                 # run the suite against the native pool
DJANGO_PG_REAL_POOL_BACKEND=dj_db_conn_pool PGPORT=5433 uv run pytest   # third-party backend
uv run tox                                # full Django/Python matrix
```

## License

MIT
