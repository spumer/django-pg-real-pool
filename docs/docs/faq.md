# FAQ & limitations

## When should I use this?

Good fit:

- Multi-threaded apps where DB work is bursty and interleaved with non-DB work (external API
  calls, template rendering, background actors).
- High concurrency against a database — or a PgBouncer — with a limited connection budget.

Think twice:

- If most of your request time *is* spent in the database, the win is small.
- The trade-off is more `getconn`/`putconn` churn and re-acquisition between queries. For the
  native pool this is cheap; measure if you are latency-sensitive.

## Does my application code change?

No. You configure the `ENGINE` in `settings.py` and keep using the ORM as usual. Transactions,
`atomic()`, savepoints, server-side cursors and `on_commit` hooks behave exactly as in stock
Django.

## Native pool or `dj_db_conn_pool`?

Prefer the **native** engine (`django_pg_real_pool`): it is the default, needs no extra
dependency beyond `psycopg[pool]`, and is maintained by Django itself. Use
`django_pg_real_pool.dj_db_conn_pool` only if you are on Django < 5.1 / psycopg 2, or already
standardised on `django-db-connection-pool`.

## Why must `CONN_MAX_AGE` be 0?

Django persistent connections (`CONN_MAX_AGE > 0`) keep a connection pinned to a thread between
requests, which is fundamentally incompatible with handing connections back to a pool. Django
enforces this for the native pool with `ImproperlyConfigured("Pooling doesn't support persistent
connections.")`.

## What happens inside `atomic()`?

The connection is **held** for the whole `atomic()` block — it is not released between queries,
because a transaction must run on a single connection. It returns to the pool when the outermost
block commits or rolls back.

## I use server-side cursors. Is that handled?

Yes. A server-side (named) cursor keeps the connection while rows are streamed; the connection is
released only after the cursor is exhausted. If you set `DISABLE_SERVER_SIDE_CURSORS = True`, rows
are fetched eagerly and the connection is released immediately (outside transactions).

## What if I turn autocommit off myself?

`set_autocommit(False)` hands transaction control to you. The connection is then held until you call
`commit()` / `rollback()`, at which point it is released. After that, autocommit returns to the
backend default — set it again if you need another manual transaction.

## I get `ImproperlyConfigured: ... requires connection pooling to be enabled`

You used the native engine without enabling the pool. Add
`OPTIONS={"pool": True}` (or a dict of options) and keep `CONN_MAX_AGE = 0`. If you genuinely do
not want pooling, use `django.db.backends.postgresql` instead.

## I get `ImproperlyConfigured: ... requires the optional 'django-db-connection-pool' package`

You pointed `ENGINE` at `django_pg_real_pool.dj_db_conn_pool` without installing the extra:

```bash
pip install 'django-pg-real-pool[dj-db-conn-pool]'
```

## What if some library opens a cursor and never closes it?

ASAP release is triggered by **closing the cursor** — its `close()` / `__exit__`. Django's ORM and
internals always use cursors as context managers (`with connection.cursor() as cur:`), so the early
release is automatic for all normal ORM usage.

If third-party code obtains a cursor and never closes it, the early release simply **does not
happen for that scope**: the connection is held until Django's normal teardown
(`close_old_connections()`, typically at the end of the request) — exactly like a plain pool, so no
regression, just no ASAP benefit there.

**Garbage collection does not rescue it.** Dropping the leaked cursor and letting it be collected
does *not* return the connection — the cursor proxy intentionally has no `__del__`. A GC-time
fallback was deliberately rejected, because it is both unreliable and unsafe:

- Django connections are thread-bound. Cyclic GC can run on any thread, so a GC-time `close()`
  would raise `DatabaseError` ("objects created in a thread can only be used in that same thread")
  and be silently swallowed — the connection would *not* be returned anyway.
- Worse, the proxy references the *wrapper*, not a specific borrow. A stale proxy collected later
  (after its cursor was already closed) would close whatever connection the wrapper currently
  holds — potentially one re-acquired for an unrelated query or an **active transaction**, rolling
  it back mid-flight.

So: keep cursors closed (the ORM does). For raw cursors in your own code, always use
`with connection.cursor()`.

## Limitations

- **PostgreSQL only.** The package builds on Django's PostgreSQL backend.
- **Native engine requires psycopg 3.** psycopg 2 does not support the native pool.
- **No persistent connections** (`CONN_MAX_AGE` must be 0) when pooling.
- **Early release depends on cursors being closed.** Code that leaks an open cursor falls back to
  normal end-of-request release (see the question above); GC does not return the connection.
- Extra connection churn between queries — a deliberate trade for holding pool connections for the
  shortest possible time.
