## v0.1.0

Initial release.

### Feat

- ASAP connection release for pooled PostgreSQL connections: the connection is
  returned to the pool right after each query outside of `atomic()` blocks, and held
  only for the duration of a transaction.
- Native pool engine `django_pg_real_pool` (default) on top of Django's built-in
  psycopg pool (Django 5.1+, `OPTIONS={'pool': ...}`).
- Optional third-party pool engine `django_pg_real_pool.dj_db_conn_pool`
  (`django-db-connection-pool`, SQLAlchemy `QueuePool`) behind the
  `dj-db-conn-pool` extra.
- Fail-fast guard: the native engine raises `ImproperlyConfigured` if used without
  pooling enabled.
- Opt-in diagnostic (`DJANGO_PG_REAL_POOL_WARN_UNCLOSED_CURSORS=1`): logs a warning, with the
  open site, when a cursor is garbage-collected without being closed (never touches the connection).
- Tested matrix: Python 3.10–3.14 × Django 5.1 / 5.2 LTS / 6.0 × both pool backends.
