from __future__ import annotations

import asyncio

import pytest
from tenacity import RetryError, retry, retry_if_exception_type, stop_after_attempt

from backend.core.utils.request import (
    describe_exception,
    format_http_failure,
    summarize_error_message,
)


class _FakeRequest:
    def __init__(self, *, method: str, url: str) -> None:
        self.method = method
        self.url = url


class _FakeResponse:
    def __init__(self, *, status_code: int, text: str, method: str, url: str) -> None:
        self.status_code = status_code
        self.text = text
        self.request = _FakeRequest(method=method, url=url)


class _FalseyFakeResponse(_FakeResponse):
    def __bool__(self) -> bool:
        return False


class _FakeHTTPError(Exception):
    def __init__(self, message: str, *, response: _FakeResponse) -> None:
        super().__init__(message)
        self.response = response


def test_format_http_failure_redacts_body_and_keeps_context() -> None:
    response = _FakeResponse(
        status_code=401,
        text='{"error":"denied","token":"super-secret","api_key":"abc123"}',
        method="DELETE",
        url="http://localhost/api/v1/media/42?token=super-secret",
    )
    exc = _FakeHTTPError(
        "401 Client Error for url: http://localhost/api/v1/media/42?token=super-secret",
        response=response,
    )

    message = format_http_failure(action="Delete failed", exception=exc)

    assert "Delete failed" in message
    assert "status=401" in message
    assert "method=DELETE" in message
    assert "url=http://localhost/api/v1/media/42?token=<redacted>" in message
    assert '"token":"<redacted>"' in message
    assert '"api_key":"<redacted>"' in message
    assert "super-secret" not in message
    assert "abc123" not in message


def test_format_http_failure_keeps_explicit_falsey_response() -> None:
    response = _FalseyFakeResponse(
        status_code=413,
        text='{"error":"too large"}',
        method="POST",
        url="http://localhost/Collections/1/Items",
    )

    message = format_http_failure(
        action="Collection update failed",
        exception=RuntimeError("None"),
        response=response,
    )

    assert "status=413" in message
    assert "method=POST" in message
    assert "body=" in message


def test_summarize_error_message_redacts_and_truncates() -> None:
    raw = (
        "Failed request body: token=very-secret-value "
        + ("x" * 300)
        + " api_key=another-secret-value"
    )

    summary = summarize_error_message(raw, max_chars=80)

    assert summary is not None
    assert len(summary) <= 80
    assert "<redacted>" in summary
    assert "very-secret-value" not in summary
    assert "another-secret-value" not in summary


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        # a timeout with no message used to be stored as an empty reason
        (TimeoutError(), "TimeoutError"),
        (KeyError("tmdb_id"), "KeyError: 'tmdb_id'"),
        (ValueError("Failed to delete series 12"), "Failed to delete series 12"),
    ],
)
def test_describe_exception_is_never_empty(exc: BaseException, expected: str) -> None:
    assert describe_exception(exc) == expected


def test_describe_exception_unwraps_exhausted_retries() -> None:
    @retry(stop=stop_after_attempt(2), retry=retry_if_exception_type(TimeoutError))
    async def always_times_out() -> None:
        raise TimeoutError("read timed out (read timeout=60)")

    with pytest.raises(RetryError) as caught:
        asyncio.run(always_times_out())

    assert describe_exception(caught.value) == (
        "TimeoutError: read timed out (read timeout=60) (gave up after 2 attempts)"
    )
