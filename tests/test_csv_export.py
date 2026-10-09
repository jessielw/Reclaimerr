from __future__ import annotations

import csv
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import media as media_routes
from backend.core.auth import get_current_user
from backend.core.utils.csv_export import csv_cell, csv_response
from backend.database import get_db
from backend.database.models import User
from backend.enums import UserRole


@pytest.mark.parametrize("value", ["=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx"])
def test_formula_looking_text_is_escaped(value: str) -> None:
    assert csv_cell(value) == f"'{value}"


@pytest.mark.parametrize("value", ["Dune", "", 42, 1.5, None, "a=b"])
def test_other_values_are_left_alone(value: object) -> None:
    assert csv_cell(value) == value


def test_every_field_is_quoted() -> None:
    response = csv_response("x.csv", ["Title", "Size"], [["Dune, Part Two", 2.5]])
    text = response.body.decode("utf-8-sig")
    assert text.splitlines() == ['"Title","Size"', '"Dune, Part Two","2.5"']
    assert list(csv.reader(io.StringIO(text)))[1] == ["Dune, Part Two", "2.5"]
    assert 'filename="x.csv"' in response.headers["content-disposition"]


@pytest.mark.parametrize(
    "url", ["/api/media/candidates/export", "/api/media/reclaim-history/export"]
)
def test_exports_need_the_same_page_access_as_their_lists(url: str) -> None:
    app = FastAPI()
    app.include_router(media_routes.router)
    app.dependency_overrides[get_current_user] = lambda: User(
        username="viewer",
        password_hash="x",
        role=UserRole.USER,
        permissions=[],
        allowed_pages=[],
    )
    app.dependency_overrides[get_db] = lambda: None

    assert TestClient(app).get(url).status_code == 403
