"""django-pg-real-pool: release pooled PostgreSQL connections As Soon As Possible.

A Django PostgreSQL database backend that returns the connection to the pool right
after each query (outside of atomic blocks), instead of holding it for the whole
request/context. Works on top of Django's native psycopg pool (default) or the
optional third-party ``dj_db_conn_pool``.

Engines:
    * ``django_pg_real_pool``                  -- native pool (Django >= 5.1), the default
    * ``django_pg_real_pool.dj_db_conn_pool``  -- third-party pool (optional extra)
"""

from django_pg_real_pool._release import AutoConnectionReleaseCursor, ConnectionReleaseMixin

__version__ = '1.1.0'

__all__ = ['AutoConnectionReleaseCursor', 'ConnectionReleaseMixin', '__version__']
