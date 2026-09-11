"""Third-party pool engine: ASAP release on top of ``django-db-connection-pool``.

This engine is **optional**. It uses the SQLAlchemy ``QueuePool``-based
``dj_db_conn_pool`` and works on Django versions older than 5.1 (which lack the
native pool). Install the extra::

    pip install 'django-pg-real-pool[dj-db-conn-pool]'

Usage::

    DATABASES = {
        'default': {
            'ENGINE': 'django_pg_real_pool.dj_db_conn_pool',
            'NAME': '...', 'USER': '...', ...,
            'POOL_OPTIONS': {'POOL_SIZE': 3, 'MAX_OVERFLOW': 7, 'RECYCLE': 86400},
        }
    }

Note: the ``dj_db_conn_pool`` import below resolves to the *external*
``django-db-connection-pool`` distribution (Python 3 absolute imports), not this
sub-package.
"""

from django.core.exceptions import ImproperlyConfigured

try:
    from dj_db_conn_pool.backends.postgresql.base import DatabaseWrapper as _PooledDatabaseWrapper
except ImportError as exc:  # pragma: no cover - exercised via the missing-extra path
    raise ImproperlyConfigured(
        "ENGINE 'django_pg_real_pool.dj_db_conn_pool' requires the optional "
        "'django-db-connection-pool' package. Install it with: "
        "pip install 'django-pg-real-pool[dj-db-conn-pool]'. "
        "For the native Django pool (Django >= 5.1) use ENGINE 'django_pg_real_pool' instead."
    ) from exc

from django_pg_real_pool._release import ConnectionReleaseMixin
from django_pg_real_pool._telemetry import TelemetryMixin


class DatabaseWrapper(ConnectionReleaseMixin, TelemetryMixin, _PooledDatabaseWrapper):
    """PostgreSQL wrapper with dj_db_conn_pool pooling and ASAP connection release."""

    telemetry_backend = 'dj_db_conn_pool'
