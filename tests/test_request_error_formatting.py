from __future__ import annotations

from backend.core.utils.request import format_http_failure, summarize_error_message


class _FakeRequest:
    def __init__(self, *, method: str, url: str) -> None:
        self.method = method
        self.url = url


class _FakeResponse:
    def __init__(self, *, status_code: int, text: str, method: str, url: str) -> None:
        self.status_code = status_code
        self.text = text
        self.request = _FakeRequest(method=method, url=url)


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
    exc = _FakeHTTPError("401 Client Error", response=response)

    message = format_http_failure(action="Delete failed", exception=exc)

    assert "Delete failed" in message
    assert "status=401" in message
    assert "method=DELETE" in message
    assert "url=http://localhost/api/v1/media/42?token=<redacted>" in message
    assert '"token":"<redacted>"' in message
    assert '"api_key":"<redacted>"' in message
    assert "super-secret" not in message
    assert "abc123" not in message


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
