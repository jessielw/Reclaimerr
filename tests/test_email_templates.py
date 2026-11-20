from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import apprise
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import Movie
from backend.enums import NotificationChannel, NotificationType
from backend.services import email_templates as et
from backend.services import notifications

EXAMPLES = Path(__file__).parent.parent / "backend" / "email_examples"
EVENT = "delete_request_execution_succeeded"


def _values(**overrides):
    base = {t: "" for t in et.TOKENS}
    base.update(overrides)
    return base


def test_values_are_html_escaped_in_the_body_but_not_the_subject():
    values = _values(media_title="<b>Tom & Jerry</b>")
    source = "<title>Gone: {{media_title}}</title><p>{{media_title}}</p>"

    subject, html = et.render_template(source, values, default_subject="x")

    assert subject == "Gone: <b>Tom & Jerry</b>"
    assert "&lt;b&gt;Tom &amp; Jerry&lt;/b&gt;" in html
    assert "<b>" not in html.split("<p>")[1]


def test_if_blocks_are_kept_only_when_the_token_has_a_value():
    source = "a{{#if genres}} [{{genres}}]{{/if}}{{#if overview}} [{{overview}}]{{/if}}"

    out = et.render_string(source, _values(genres="Drama"), html=True)

    assert out == "a [Drama]"


def test_unknown_tokens_render_as_nothing():
    assert et.render_string("x{{nope}}y", _values(), html=True) == "xy"


def test_missing_title_element_falls_back_to_the_default_subject():
    subject, _ = et.render_template("<p>hi</p>", _values(), default_subject="Default")
    assert subject == "Default"


def test_build_values_formats_the_context():
    values = et.build_values(
        event=EVENT,
        context={
            "request_id": 3,
            "media_title": "Dune",
            "media_year": 2021,
            "media_type": "movie",
            "poster_path": "/p.jpg",
            "media_size_bytes": 3 * 1024**3,
            "genres": ["Sci-Fi", "Drama"],
            "requested_at": datetime(2026, 1, 2, tzinfo=UTC),
            "executed_at": "2026-01-05T10:00:00+00:00",
        },
        recipient=SimpleNamespace(display_name="Alex", username="alex"),
        application_url="https://r.example.com/",
        total_reclaimed_bytes=2 * 1024**4,
    )

    assert values["recipient_name"] == "Alex"
    assert values["poster_url"] == "https://image.tmdb.org/t/p/w342/p.jpg"
    assert values["size"] == "3.0 GB"
    assert values["genres"] == "Sci-Fi, Drama"
    assert values["requested_date"] == "2 Jan 2026"
    assert values["deleted_date"] == "5 Jan 2026"
    assert values["total_reclaimed"] == "2.0 TB"
    assert values["app_url"] == "https://r.example.com"
    assert values["media_type"] == "Movie"


def test_missing_context_leaves_tokens_empty_instead_of_failing():
    values = et.build_values(event=EVENT, context=None)
    assert values["media_title"] == "" and values["size"] == ""
    assert values["poster_url"] == "" and values["genres"] == ""


def test_voices_are_picked_per_request_and_can_use_other_tokens(tmp_path):
    (tmp_path / "voices.json").write_text(
        json.dumps(
            [
                {"headline": "A {{media_title}}", "subline": "s0", "subject": "Zero"},
                {"headline": "B {{media_title}}", "subline": "s1", "subject": "One"},
            ]
        )
    )

    def pick(request_id):
        return et.build_values(
            event=EVENT,
            context={"request_id": request_id, "media_title": "Dune"},
            directory=tmp_path,
        )

    assert pick(4)["headline"] == "A Dune"
    assert pick(5)["headline"] == "B Dune"
    assert pick(5)["voice_subject"] == "One"


def test_broken_voices_file_falls_back_to_the_defaults(tmp_path):
    (tmp_path / "voices.json").write_text("not json")
    values = et.build_values(event=EVENT, context=None, directory=tmp_path)
    assert values["headline"]


def test_find_template_rejects_path_tricks(tmp_path):
    (tmp_path / "a.html").write_text("x")
    assert et.find_template("a", tmp_path) is not None
    assert et.find_template("../a", tmp_path) is None
    assert et.find_template("missing", tmp_path) is None


def _setting(channel, detail="template"):
    return SimpleNamespace(channel=channel, preferences={EVENT: {"detail": detail}})


def test_email_template_is_used_only_for_system_email(monkeypatch, tmp_path):
    (tmp_path / f"{EVENT}.html").write_text(
        "<title>S {{media_title}}</title>Hi {{recipient_name}}"
    )
    monkeypatch.setattr(et, "template_dir", lambda: tmp_path)
    kwargs = dict(
        notification_type=NotificationType.DELETE_REQUEST_EXECUTION_SUCCEEDED,
        context={"media_title": "Dune"},
        application_url=None,
        recipient=SimpleNamespace(display_name="Alex"),
    )

    email = asyncio.run(
        notifications._compose_email_template(
            setting=_setting(NotificationChannel.SYSTEM_EMAIL), **kwargs
        )
    )
    other = asyncio.run(
        notifications._compose_email_template(
            setting=_setting(NotificationChannel.APPRISE), **kwargs
        )
    )

    assert email == (
        "S Dune",
        "<title>S Dune</title>Hi Alex",
        apprise.NotifyFormat.HTML,
    )
    assert other is None


def test_without_a_template_file_the_default_email_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(et, "template_dir", lambda: tmp_path)

    result = asyncio.run(
        notifications._compose_email_template(
            setting=_setting(NotificationChannel.SYSTEM_EMAIL),
            notification_type=NotificationType.DELETE_REQUEST_EXECUTION_SUCCEEDED,
            context={},
            application_url=None,
            recipient=None,
        )
    )

    assert result is None


def test_posters_come_from_tmdb_not_from_a_private_address(monkeypatch):
    assert et._poster_url("/p.jpg") == "https://image.tmdb.org/t/p/w342/p.jpg"
    assert et._poster_url("https://image.tmdb.org/t/p/w500/p.jpg").endswith("/p.jpg")
    assert et._poster_url("http://192.168.1.5:8096/Items/1/Images/Primary") == ""
    assert et._poster_url(None) == ""

    monkeypatch.setattr(
        et.settings, "email_poster_base_url", "https://img.example.com/"
    )
    assert et._poster_url("/p.jpg") == "https://img.example.com/p.jpg"


def test_default_wording_is_the_example_wording(tmp_path):
    example = et.parse_voices((EXAMPLES / "voices.json").read_text(encoding="utf-8"))

    def headline(request_id):
        return et.build_values(
            event=EVENT,
            context={"request_id": request_id, "media_title": "Dune"},
            recipient=SimpleNamespace(display_name="Alex"),
            directory=tmp_path,  # no voices file saved here
        )["headline"]

    expected = [
        et.render_string(
            v["headline"],
            _values(media_title="Dune", recipient_name="Alex"),
            html=False,
        )
        for v in example
    ]
    assert [headline(i) for i in range(len(example))] == expected


def test_a_destination_must_choose_the_template_option(monkeypatch, tmp_path):
    (tmp_path / f"{EVENT}.html").write_text("<title>S</title>x")
    monkeypatch.setattr(et, "template_dir", lambda: tmp_path)

    def compose(detail):
        return asyncio.run(
            notifications._compose_email_template(
                setting=_setting(NotificationChannel.SYSTEM_EMAIL, detail),
                notification_type=NotificationType.DELETE_REQUEST_EXECUTION_SUCCEEDED,
                context={},
                application_url=None,
                recipient=None,
            )
        )

    assert compose("template") is not None
    assert compose("standard") is None
    assert compose("compact") is None


def test_template_is_only_a_valid_choice_for_templatable_events():
    from backend.models.settings import normalize_notification_preferences

    prefs = normalize_notification_preferences(
        {
            EVENT: {"detail": "template"},
            "task_failure": {"detail": "template"},
        }
    )

    assert prefs[EVENT]["detail"] == "template"
    assert prefs["task_failure"]["detail"] == "standard"
