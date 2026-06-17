import gc
import uuid

import pytest
from django import db
from django.contrib.auth import get_user_model
from django.db import transaction

pytestmark = [
    # Disable Django's per-test transaction wrapping: these tests drive commit/rollback
    # themselves and assert how the connection moves in and out of the pool.
    pytest.mark.django_db(transaction=True),
]


def is_connected(connection):
    """True while a real connection is checked out and pinned to this wrapper."""
    return connection.connection is not None


def get_pool_size(pool):
    """Number of idle connections currently sitting in the pool (ready to hand out).

    Duck-typed so it works for both backends: the native ``psycopg_pool`` exposes
    ``get_stats()``; the third-party SQLAlchemy ``QueuePool`` exposes the public
    ``checkedin()`` (number of idle connections).
    """
    if hasattr(pool, 'get_stats'):
        return pool.get_stats().get('pool_available', 0)
    return pool.checkedin()


@pytest.fixture(autouse=True)
def _no_serverside_cursor(settings, connection):
    settings.DATABASES[connection.alias]['DISABLE_SERVER_SIDE_CURSORS'] = True


def create_users(count: int = 10):
    assert count > 0
    UserModel = get_user_model()  # noqa: N806
    return UserModel.objects.bulk_create(
        [UserModel(username=f'{uuid.uuid4()}') for _ in range(count)]
    )


@pytest.fixture(autouse=True)
def _no_users():
    UserModel = get_user_model()  # noqa: N806
    UserModel.objects.all().delete()


def test_no_atomic__release_immediate(connection, pool):
    create_users(10)
    db.close_old_connections()

    UserModel = get_user_model()  # noqa: N806
    assert UserModel.objects.count() > 0

    assert get_pool_size(pool) == 1
    assert not is_connected(connection)

    for _ in range(2):
        list(UserModel.objects.all())
        assert get_pool_size(pool) == 1
        assert not is_connected(connection)


def test_atomic__release_after_commit(connection, pool):
    create_users(10)
    db.close_old_connections()

    UserModel = get_user_model()  # noqa: N806
    assert UserModel.objects.count() > 0

    assert not is_connected(connection)
    assert get_pool_size(pool) == 1

    with transaction.atomic():
        list(UserModel.objects.all())
        assert get_pool_size(pool) == 0
        assert is_connected(connection)

    assert not is_connected(connection)
    assert get_pool_size(pool) == 1


def test_atomic__release_after_rollback(connection, pool):
    db.close_old_connections()

    UserModel = get_user_model()  # noqa: N806
    assert UserModel.objects.count() == 0

    assert not is_connected(connection)
    assert get_pool_size(pool) == 1

    with pytest.raises(RuntimeError):  # noqa: PT012 pytest.raises() block should contain a single simple statement
        with transaction.atomic():
            create_users(10)
            assert get_pool_size(pool) == 0
            assert is_connected(connection)
            raise RuntimeError('Interrupt transaction')

    assert not is_connected(connection)
    assert get_pool_size(pool) == 1

    assert UserModel.objects.count() == 0, 'No rollback applied'


def test_atomic__savepoint__release_after_commit_outer_atomic(connection, pool):
    create_users(10)
    db.close_old_connections()

    UserModel = get_user_model()  # noqa: N806
    assert UserModel.objects.count() > 0

    assert not is_connected(connection)
    assert get_pool_size(pool) == 1

    with transaction.atomic():
        with transaction.atomic():
            list(UserModel.objects.all())
            assert get_pool_size(pool) == 0
            assert is_connected(connection)
        assert get_pool_size(pool) == 0
        assert is_connected(connection)

    assert not is_connected(connection)
    assert get_pool_size(pool) == 1


def test_no_atomic__iterator__release_immediate(connection, pool):
    create_users(10)
    db.close_old_connections()

    UserModel = get_user_model()  # noqa: N806
    assert UserModel.objects.count() > 0

    assert not is_connected(connection)
    assert get_pool_size(pool) == 1

    for _ in UserModel.objects.all().iterator(chunk_size=1):
        # without a server-side cursor the rows are fetched at once
        assert not is_connected(connection)
        assert get_pool_size(pool) == 1

    assert not is_connected(connection)
    assert get_pool_size(pool) == 1


def test_atomic__iterator__release_after_commit(connection, pool):
    create_users(10)
    db.close_old_connections()

    UserModel = get_user_model()  # noqa: N806
    assert UserModel.objects.count() > 0

    assert not is_connected(connection)
    assert get_pool_size(pool) == 1

    with transaction.atomic():
        for _ in UserModel.objects.all().iterator(chunk_size=1):
            # without a server-side cursor the rows are fetched at once,
            # but atomic keeps the connection held
            assert is_connected(connection)
            assert get_pool_size(pool) == 0

        # leaving the iterator we still hold the connection
        assert is_connected(connection)
        assert get_pool_size(pool) == 0

    assert not is_connected(connection)
    assert get_pool_size(pool) == 1


def test_no_atomic__iterator__server_side__release_after_iter(settings, connection, pool):
    settings.DATABASES[connection.alias]['DISABLE_SERVER_SIDE_CURSORS'] = False
    create_users(10)

    db.close_old_connections()

    UserModel = get_user_model()  # noqa: N806
    assert UserModel.objects.count() > 0

    assert not is_connected(connection)
    assert get_pool_size(pool) == 1

    for _ in UserModel.objects.all().iterator(chunk_size=1):
        # a server-side cursor holds the connection until all rows are read
        assert is_connected(connection)
        assert get_pool_size(pool) == 0
    # all rows read
    assert not is_connected(connection)
    assert get_pool_size(pool) == 1


def test__insert__release_after_commit(connection, pool):
    db.close_old_connections()

    assert not is_connected(connection)
    assert get_pool_size(pool) == 1

    create_users(1)

    assert not is_connected(connection)
    assert get_pool_size(pool) == 1


def test_raw_cursor__context_manager__release_after_close(connection, pool):
    """Using the cursor proxy directly: held while open, released on close."""
    db.close_old_connections()

    assert not is_connected(connection)
    assert get_pool_size(pool) == 1

    with connection.cursor() as cursor:
        cursor.execute('SELECT 1 UNION ALL SELECT 2')
        rows = list(cursor)  # exercises the proxy __iter__
        assert rows == [(1,), (2,)]

        # connection is held while the cursor is open
        assert is_connected(connection)
        assert get_pool_size(pool) == 0

    # leaving the cursor context releases the connection back to the pool
    assert not is_connected(connection)
    assert get_pool_size(pool) == 1


def test_unclosed_cursor__held_until_connection_close__gc_does_not_release(connection, pool):
    """ASAP release is tied to closing the cursor, NOT to garbage collection.

    Django's ORM always closes its cursors, so this never bites in practice. But third-party
    code that opens a cursor and never closes it gets no early release: the connection is held
    until Django's normal teardown (``close_old_connections()`` at end of request). Crucially,
    garbage-collecting the leaked cursor does NOT return the connection — the proxy has no
    ``__del__`` on purpose (a GC-time release would be unreliable across threads and could close
    a connection already re-acquired for another operation).
    """
    db.close_old_connections()
    assert not is_connected(connection)
    assert get_pool_size(pool) == 1

    cursor = connection.cursor()  # opened outside an atomic block, never closed
    cursor.execute('SELECT 1')
    assert is_connected(connection)
    assert get_pool_size(pool) == 0

    # dropping the cursor and forcing GC does NOT return the connection to the pool
    del cursor
    gc.collect()
    assert is_connected(connection)
    assert get_pool_size(pool) == 0

    # only the normal connection close (what Django does at request end) returns it
    connection.close()
    assert not is_connected(connection)
    assert get_pool_size(pool) == 1


def test__atomic__on_commit__called_after_commit(connection, pool):
    db.close_old_connections()

    assert not is_connected(connection)
    assert get_pool_size(pool) == 1

    _called = False

    def _on_commit_hook(*args):
        nonlocal _called
        _called = True
        assert is_connected(connection)
        assert get_pool_size(pool) == 0

        # ensure _on_commit_hook runs out of the transaction
        assert connection.autocommit
        assert not connection.in_atomic_block

    with transaction.atomic():
        assert is_connected(connection)
        assert get_pool_size(pool) == 0

        transaction.on_commit(_on_commit_hook)
        assert not _called

    assert _called

    assert not is_connected(connection)
    assert get_pool_size(pool) == 1
    assert connection.autocommit
    assert not connection.in_atomic_block


def test__atomic__on_commit__skipped_after_rollback(connection, pool):
    db.close_old_connections()

    assert not is_connected(connection)
    assert get_pool_size(pool) == 1

    _called = False

    def _on_commit_hook(*args):
        nonlocal _called
        _called = True
        assert is_connected(connection)
        assert get_pool_size(pool) == 0

        # ensure _on_commit_hook runs out of the transaction
        assert connection.autocommit
        assert not connection.in_atomic_block

    with pytest.raises(RuntimeError):  # noqa: PT012 pytest.raises() block should contain a single simple statement
        with transaction.atomic():
            assert is_connected(connection)
            assert get_pool_size(pool) == 0

            transaction.on_commit(_on_commit_hook)

            assert not _called

            raise RuntimeError('Interrupt transaction')

    assert not _called

    assert not is_connected(connection)
    assert get_pool_size(pool) == 1


def test__atomic__outermost_autocommit_enabled__release_connection_after_atomic(connection, pool):
    """Release connection ASAP when Django controls the transaction.

    When outermost autocommit=True is set we assume transactions are controlled
    automatically. So we can release the connection after the transaction. If the
    connection is needed afterwards it will be acquired again automatically.
    """
    db.close_old_connections()

    default_autocommit = connection.settings_dict['AUTOCOMMIT']
    transaction.set_autocommit(True)

    # set_autocommit ensures the connection is acquired
    assert is_connected(connection)
    assert get_pool_size(pool) == 0
    assert connection.autocommit

    UserModel = get_user_model()  # noqa: N806
    assert UserModel.objects.count() == 0
    with transaction.atomic():
        create_users(1)

    # release connection ASAP, ignore the one acquired in `set_autocommit`
    assert not is_connected(connection)
    assert get_pool_size(pool) == 1

    assert UserModel.objects.count() == 1
    # re-acquire sets autocommit back to the default
    assert connection.autocommit == default_autocommit


@pytest.mark.parametrize('commit', [True, False], ids=['commit', 'rollback'])
def test__atomic__outermost_autocommit_disabled__keep_connection_after_atomic(
    connection, pool, commit
):
    """Keep the connection when the programmer controls the transaction; release it after.

    From the Django documentation:
        Once you turn autocommit off, you get the default behavior of your database
        adapter, and Django won't help you.
    """
    db.close_old_connections()

    initial_autocommit = False
    default_autocommit = connection.settings_dict['AUTOCOMMIT']
    transaction.set_autocommit(initial_autocommit)

    # set_autocommit ensures the connection is acquired
    assert is_connected(connection)
    assert get_pool_size(pool) == 0
    assert connection.autocommit == initial_autocommit

    UserModel = get_user_model()  # noqa: N806
    assert UserModel.objects.count() == 0
    with transaction.atomic():
        create_users(1)
    assert UserModel.objects.count() == 1

    # transaction still active (no rollback or commit called)
    # check the connection is still pinned to the current thread
    # and not released to the pool
    assert is_connected(connection)
    assert get_pool_size(pool) == 0
    assert connection.autocommit == initial_autocommit

    # `count()` acquires the connection from the pool
    # and restores the defaults which we check afterwards
    if commit:
        connection.commit()
        assert (
            connection.autocommit == default_autocommit
        )  # autocommit now default for performance reasons
        assert UserModel.objects.count() == 1
    else:
        connection.rollback()
        assert connection.autocommit == initial_autocommit
        assert UserModel.objects.count() == 0

    # check default settings restored after a new query
    assert connection.autocommit == default_autocommit

    # transaction ended manually
    # now the connection is released and ready to be reused
    assert not is_connected(connection)
    assert get_pool_size(pool) == 1
