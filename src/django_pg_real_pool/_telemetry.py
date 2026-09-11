"""Backend hooks for optional telemetry, independent of ASAP release behaviour."""

import logging

from django.db.backends.base.base import NO_DB_ALIAS

_logger = logging.getLogger('django_pg_real_pool')


class TelemetryMixin:
    """Track successful checkout and actual low-level connection return once."""

    telemetry_backend = 'native'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from django_pg_real_pool.telemetry import configuration, validate

        self._telemetry_config = configuration(self.settings_dict)
        self._telemetry_observer = None
        if self._telemetry_config:
            validate(self._telemetry_config)

    def _pool_observer(self):
        if not self._telemetry_config or self.alias == NO_DB_ALIAS:
            return None
        from django_pg_real_pool.telemetry import observe

        if self.telemetry_backend == 'native':
            pool = self.pool
        else:
            from dj_db_conn_pool.core import pool_container
            from dj_db_conn_pool.core.exceptions import PoolDoesNotExist

            try:
                pool = pool_container.get(self.alias)
            except PoolDoesNotExist:
                pool = None
        if pool is None:
            return None
        return observe(
            pool, self.alias, self.telemetry_backend, self.settings_dict, self._telemetry_config
        )

    def get_new_connection(self, conn_params):
        """Count attempts and retain the pool observer until the connection is returned."""
        if not self._telemetry_config or self.alias == NO_DB_ALIAS:
            return super().get_new_connection(conn_params)
        # Register the native pool before checkout so timeouts and startup are observable.
        observer = self._pool_observer()
        try:
            connection = super().get_new_connection(conn_params)
        except Exception as exc:
            try:
                observer = observer or self._pool_observer()
                if observer:
                    observer.event(error=exc, request=True)
            except Exception:
                _logger.exception('Failed to record pool checkout error')
            raise
        try:
            observer = observer or self._pool_observer()
        except Exception:
            # Registration errors must not leak a successfully borrowed connection.
            if self.telemetry_backend == 'native':
                self.pool.putconn(connection)
            else:
                connection.close()
            raise
        self._telemetry_observer = observer
        if observer:
            observer.event(change=1, request=True)
        return connection

    def _close(self):
        """Update usage only when Django actually closes/returns the connection."""
        try:
            return super()._close()
        finally:
            # Django drops self.connection even if the pool return fails.
            observer = self._telemetry_observer
            self._telemetry_observer = None
            if observer:
                observer.event(change=-1)
