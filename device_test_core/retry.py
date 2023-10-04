"""Retry utils"""
import re
from functools import wraps
from device_test_core.errors import FinalAssertionError
from tenacity import (
    RetryError,
    Retrying,
    retry_if_exception_type,
    retry_if_not_exception_type,
    stop_after_delay,
    wait_fixed,
)
from requests.exceptions import RequestException


def configure_retry_on_members(obj: object, pattern: str, **kwargs):
    """Configure retry mechanism to all functions matching a pattern"""
    # apply retry mechanism
    pattern_re = re.compile(pattern)
    for name in dir(obj):
        if pattern_re.match(name, pos=0):

            def wrapper(func):
                @wraps(func)
                def retry_custom(*args, **kwargs):
                    return retrier(func, *args, **kwargs)

                return retry_custom

            setattr(obj, name, wrapper(getattr(obj, name)))


def retrier(func, *args, **kwargs):
    attempt = None
    try:
        wait = float(kwargs.pop("wait", 2))
        timeout = float(kwargs.pop("timeout", 30))

        for attempt in Retrying(
            retry=(
                retry_if_exception_type((AssertionError, RequestException, OSError))
                & retry_if_not_exception_type(FinalAssertionError)
            ),
            stop=(stop_after_delay(timeout)),
            wait=wait_fixed(wait),
            reraise=True,
        ):
            with attempt:
                return func(*args, **kwargs)
    except RetryError as ex:
        raise ex
    except Exception as ex:
        # Append additional context information
        message = (
            f"Retries ended. duration={attempt.retry_state.seconds_since_start:.3f}s, "
            f"attempts={attempt.retry_state.attempt_number}, "
            f"timeout={timeout:.3f}s, wait={wait:.3f}s"
        )
        raise ex from AssertionError(message)
