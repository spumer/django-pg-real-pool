"""ASAP connection-release behaviour, independent of the underlying pool.

This module contains the only real logic of the package: a mixin that releases
the database connection back to the pool *as soon as possible*. It does not know
(and must not care) which pool sits underneath — Django's native ``psycopg``
pool or the third-party ``dj_db_conn_pool`` (SQLAlchemy ``QueuePool``). Both
return the connection to their pool when :meth:`DatabaseWrapper.close` is called,
which is all this mixin relies on.
"""

import logging
import os
import traceback
from typing import TYPE_CHECKING

from django.utils.asyncio import async_unsafe

_logger = logging.getLogger('django_pg_real_pool')


def _env_truthy(value):
    return (value or '').strip().lower() in ('1', 'true', 'yes', 'on')


# Opt-in diagnostic: when enabled, a cursor garbage-collected without being closed logs a
# warning (it NEVER touches the connection — a GC-time close would be unsafe). Read once at
# import; tests monkeypatch this module-level flag.
_WARN_UNCLOSED = _env_truthy(os.environ.get('DJANGO_PG_REAL_POOL_WARN_UNCLOSED_CURSORS'))

if TYPE_CHECKING:
    # For type checkers only: declare the host class so attribute access (self.connection,
    # self.in_atomic_block, self.close(), ...) resolves. At runtime the base is object,
    # so the real MRO is `DatabaseWrapper(ConnectionReleaseMixin, <pooled DatabaseWrapper>)`.
    from django.db.backends.postgresql.base import DatabaseWrapper as _MixinBase
else:
    _MixinBase = object


class ConnectionReleaseMixin(_MixinBase):
    """Release the connection back to the pool As Soon As Possible.

    Except when you are in an Atomic block — then the connection is released
    after the block ends.

    You can control the transaction manually (``set_autocommit(False)``), then the
    connection is released when the transaction ends — when ``commit``/``rollback``
    is called. That means ``set_autocommit(False)`` affects only until the
    ``commit``/``rollback`` call. After it you need to call ``set_autocommit(False)``
    again.

    You can also set autocommit to ``False`` as the default option for this driver.

    Motivation:

        The main problem of a plain pool is that it holds the connection after the
        first query and releases it only when the surrounding context (e.g. the
        request) finishes. That is, having made one query to the database, we keep
        the connection until the whole current context is done. This greatly limits
        the number of simultaneously running contexts: threads fight for connections
        and may fail to get one. Connections hang idle and are not used, although
        other threads may need them.

    Solution: release the connection ASAP — after each ``execute_sql`` when possible.
    """

    def _cursor(self, *args, **kwargs):
        must_close = self.connection is None
        if self.in_atomic_block:
            must_close = False

        cursor = super()._cursor(*args, **kwargs)
        if must_close:
            return AutoConnectionReleaseCursor(self, cursor)

        return cursor

    @async_unsafe
    def commit(self):
        super().commit()
        # django.db.transaction.Atomic uses set_autocommit(True) after commit and
        # calls on_commit hooks internally. Below we disable that behaviour by
        # setting closed_in_transaction = True, and run on_commit hooks manually
        # like set_autocommit(True) does.

        # autocommit value is assigned in `connect()` when the connection is acquired
        default_autocommit = self.settings_dict['AUTOCOMMIT']

        if not self.in_atomic_block and default_autocommit:
            # autocommit may currently be False (a manual set_autocommit(False) earlier);
            # restore it to the configured default so on_commit hooks run as Django expects
            self.set_autocommit(True)
            # for performance reasons:
            #   do not restore autocommit to False (it's non-default!)
            #   after re-acquire the connection will restore its defaults

        self.close()
        # do not allow instant re-acquire of the connection from the pool
        self.closed_in_transaction = True

    @async_unsafe
    def rollback(self):
        super().rollback()
        self.close()
        # do not allow instant re-acquire of the connection from the pool
        self.closed_in_transaction = True


class AutoConnectionReleaseCursor:
    """Cursor proxy that releases the connection to the pool when closed."""

    def __init__(self, connection, cursor):
        super().__setattr__('_AutoConnectionReleaseCursor__connection', connection)
        super().__setattr__('_AutoConnectionReleaseCursor__cursor', cursor)
        super().__setattr__('_AutoConnectionReleaseCursor__closed', False)
        # Capture the open site only when the diagnostic is on (it's not free).
        opened_at = ''.join(traceback.format_stack()[:-1]) if _WARN_UNCLOSED else None
        super().__setattr__('_AutoConnectionReleaseCursor__opened_at', opened_at)

    def __enter__(self):
        self.__cursor.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Always release the connection, even if closing the cursor raises.
        try:
            self.__cursor.__exit__(exc_type, exc_val, exc_tb)
        finally:
            self.close()

    def __getattr__(self, item):
        return getattr(self.__cursor, item)

    def __setattr__(self, key, value):
        assert key not in ('close',)
        return setattr(self.__cursor, key, value)

    def __iter__(self):
        return iter(self.__cursor)

    def close(self):
        super().__setattr__('_AutoConnectionReleaseCursor__closed', True)
        try:
            self.__cursor.close()
        except self.__connection.Database.InterfaceError:
            # already closed
            pass
        finally:
            self.__connection.close()

    def __del__(self):
        # Diagnostic only — NEVER touches the connection here (a GC-time close would be
        # unsafe: it runs on an arbitrary thread and could release a connection that the
        # wrapper has since re-acquired for another operation). Opt-in via
        # DJANGO_PG_REAL_POOL_WARN_UNCLOSED_CURSORS.
        if not _WARN_UNCLOSED:
            return
        try:
            if self.__closed:
                return
            _logger.warning(
                'Database cursor was garbage-collected without being closed; the pooled '
                'connection was not released early. Always use `with connection.cursor()` '
                'or call cursor.close().\nCursor opened at:\n%s',
                self.__opened_at or '<stack not captured>',
            )
        except Exception:  # never raise from __del__
            pass
