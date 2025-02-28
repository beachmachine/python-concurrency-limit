import contextlib
import time
import uuid

from ._connections import *
from .configuration import *
from .exceptions import *

__all__ = ["limit", "limit_clean", "limit_iter"]


@contextlib.contextmanager
def limit(
    redis_configuration: RedisConfiguration, limit_configuration: LimitConfiguration
):
    """
    The `limit` method is a context manager that allows for executing a scoped block of code under a concurrency limit.
    It prevents more than a certain number of executions of the scoped block at a time.

    Example usage:

        from concurrency_limit import *

        redis_configuration = RedisConfiguration(host='localhost', port=6379)
        limit_configuration = LimitConfiguration(key='my_key', limit=5, limit_timeout=10, limit_expire=30)

        with limit(redis_configuration, limit_configuration) as count:
            # Scoped block of code that will be executed under the concurrency limit.
            print(f"Executing the scoped block of code. Current count: {count}")

    The method acquires an execution slot by increasing a concurrency counter stored in Redis, and checks if the
    counter is below the configured limit. If the limit is exceeded, the method waits for the configured interval
    before trying again. If the configured timeout is reached, a `ConcurrencyLimitExceededException` is raised. Upon
    exiting the scoped block, the context manager releases the execution slot and updates the concurrency counter
    in Redis accordingly.

    Note: Any exceptions raised within the scoped block will propagate outside the scope of the `limit` method.

    :param redis_configuration: RedisConfiguration object containing the configuration details for connecting to Redis.
    :param limit_configuration: LimitConfiguration object containing the configuration details for the limit.
    """

    class _LockAcquireException(Exception):
        pass

    client = get_redis(redis_configuration)

    start = time.monotonic()

    lock_limit = limit_configuration.limit
    lock_key = limit_configuration.key
    lock_expire = limit_configuration.limit_expire
    lock_timeout = limit_configuration.limit_timeout
    lock_interval = limit_configuration.limit_interval
    lock_id = str(uuid.uuid4())

    try:
        # We loop as long as it was not possible to execute the context manager's scope.
        while True:
            try:
                # First we check if we should try to acquire an execution slot. If the limit is already exceeded,
                # there is no need to set the lock-key.
                count = client.hlen(lock_key)
                if count >= lock_limit:
                    raise _LockAcquireException()

                # We have the chance to acquire a slot, so we set current id on the lock-key. After doing so, we
                # re-check the number of acquired slots, and are re-trying if we now exceeded the limit.
                count = (
                    client.pipeline()
                    .hset(lock_key, lock_id, int(time.time()) + lock_expire)
                    .expire(lock_key, lock_expire)
                    .hlen(lock_key)
                    .execute()[-1]
                )

                if count > lock_limit:
                    client.hdel(lock_key, lock_id)
                    raise _LockAcquireException()

                # Now we are in the critical section and yield to the context manager's scope.
                yield count
                break

            except _LockAcquireException:
                elapsed = time.monotonic() - start

                # If we are waiting longer than the configured timeout, we raise a `ConcurrencyLimitExceededException`
                # exception. Executing the context manager's scope failed in this case.
                if elapsed > lock_timeout:
                    raise ConcurrencyLimitExceededException(
                        limit=lock_limit, timeout=lock_timeout
                    )

                # We failed to acquire an execution slot for the context manager's scope, but we want to try again.
                # However, we wait the configured interval before we do so.
                time.sleep(lock_interval)

    finally:
        client.hdel(lock_key, lock_id)


def limit_clean(
    redis_configuration: RedisConfiguration, limit_configuration: LimitConfiguration
):
    """
    Cleans stale limit locks in the hash for the given limit configuration.

    :param redis_configuration: RedisConfiguration object containing the configuration details for connecting to Redis.
    :param limit_configuration: LimitConfiguration object containing the configuration details for the limit.
    :return: Number of cleaned items
    """
    client = get_redis(redis_configuration)
    current = int(time.time())
    count = 0

    lock_key = limit_configuration.key

    for scan_lock_id, scan_lock_expire in client.hscan_iter(lock_key):
        try:
            clean_lock = current >= int(scan_lock_expire)
        except (ValueError, TypeError):
            clean_lock = True

        if clean_lock:
            count += client.hdel(lock_key, scan_lock_id)

    return count


def limit_iter(redis_configuration: RedisConfiguration, key_pattern: str):
    """
    Return an iterator over the items in Redis specified by `key_pattern`
    using the Redis connection specified by `redis_configuration`.

    :param redis_configuration: The configuration for connecting to Redis.
    :param key_pattern: The pattern for Redis to iterate over.
    :return: Scan Iterator
    """
    return get_redis(redis_configuration).scan_iter(key_pattern)
