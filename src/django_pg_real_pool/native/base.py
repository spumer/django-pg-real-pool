"""Native pool engine: ASAP release on top of Django's built-in psycopg pool.

Requires Django >= 5.1 and ``psycopg[pool]`` (psycopg 3). Enable pooling via
``DATABASES[...]['OPTIONS'] = {'pool': True}`` (or a dict of
``psycopg_pool.ConnectionPool`` options) and keep ``CONN_MAX_AGE = 0``.

Usage::

    DATABASES = {
        'default': {
            'ENGINE': 'django_pg_real_pool',          # this engine is the default
            'NAME': '...', 'USER': '...', ...,
            'CONN_MAX_AGE': 0,
            'OPTIONS': {'pool': {'min_size': 2, 'max_size': 10}},
        }
    }
"""

from django.core.exceptions import ImproperlyConfigured
from django.db.backends.base.base import NO_DB_ALIAS
from django.db.backends.postgresql.base import DatabaseWrapper as PostgresDatabaseWrapper

from django_pg_real_pool._release import ConnectionReleaseMixin
from django_pg_real_pool._telemetry import TelemetryMixin


class DatabaseWrapper(ConnectionReleaseMixin, TelemetryMixin, PostgresDatabaseWrapper):
    """PostgreSQL wrapper with native pooling and ASAP connection release."""

    def get_new_connection(self, conn_params):
        """Acquire a pooled connection, failing fast if pooling is not configured."""
        # Fail fast: the whole point of this engine is to lend a pooled connection
        # for the shortest possible time. Without a pool, ASAP-release degrades to
        # opening and closing a real connection on every query.
        if self.alias != NO_DB_ALIAS and not self.settings_dict['OPTIONS'].get('pool'):
            raise ImproperlyConfigured(
                f'django_pg_real_pool native engine requires connection pooling to be '
                f"enabled. Set DATABASES['{self.alias}']['OPTIONS'] = {{'pool': True}} "
                f'(or a dict of psycopg_pool.ConnectionPool options) and keep '
                f'CONN_MAX_AGE = 0. Install the pool driver with: pip install '
                f"'psycopg[pool]'. If you do not want pooling, use "
                f"'django.db.backends.postgresql' instead."
            )
        return super().get_new_connection(conn_params)
