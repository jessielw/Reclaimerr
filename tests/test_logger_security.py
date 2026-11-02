from __future__ import annotations

import io
import logging
from pathlib import Path

import pytest

from backend.core.diagnostic_trace import current_operation_id
from backend.core.logger import Logger, _RawAwareFormatter


@pytest.fixture
def captured_logger(tmp_path: Path):
    shared = logging.getLogger("reclaimerr")
    previous_level = shared.level
    log = Logger(tmp_path / "unused.log", to_console=False, to_file=False)
    shared.setLevel(previous_level)
    log.logger = logging.Logger("isolated-security-test", logging.DEBUG)
    log._initialized = True
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(_RawAwareFormatter("%(levelname)s - %(message)s"))
    log.logger.addHandler(handler)
    yield log, output
    handler.close()


@pytest.mark.parametrize("method", ["debug", "info", "warning", "error", "critical"])
def test_untrusted_messages_cannot_create_log_entries(captured_logger, method):
    log, output = captured_logger

    getattr(log, method)("name\r\n2099-01-01 - INFO - forged\rhidden\nlast")

    lines = output.getvalue().splitlines()
    assert len(lines) == 1
    assert lines[0].endswith(r"name\r\n2099-01-01 - INFO - forged\rhidden\nlast")


@pytest.mark.parametrize("method", ["error", "exception"])
def test_exception_message_is_escaped_and_traceback_is_preserved(
    captured_logger, method
):
    log, output = captured_logger
    try:
        raise ValueError("delivery failed")
    except ValueError:
        if method == "exception":
            log.exception("destination\nINFO - forged")
        else:
            log.error("destination\nINFO - forged", exc_info=True)

    lines = output.getvalue().splitlines()
    assert lines[0].endswith(r"destination\nINFO - forged")
    assert lines[1] == "Traceback (most recent call last):"
    assert lines[-1] == "ValueError: delivery failed"


def test_support_tags_cannot_create_log_entries(captured_logger):
    log, output = captured_logger
    token = current_operation_id.set("operation\nINFO - forged")
    try:
        log.info("hello")
    finally:
        current_operation_id.reset(token)

    assert len(output.getvalue().splitlines()) == 1
    assert r"operation=operation\nINFO - forged" in output.getvalue()
