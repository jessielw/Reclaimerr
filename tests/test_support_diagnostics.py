from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.api.routes import diagnostics as route
from backend.core.auth import get_current_user
from backend.database import Base
from backend.database.models import (
    BackgroundJob,
    Episode,
    GeneralSettings,
    Movie,
    MovieVersion,
    NativePlaybackAggregate,
    PlaybackHistoryEvent,
    ReclaimCandidate,
    ReclaimRule,
    Season,
    Series,
    ServiceConfig,
    ServiceMediaLibrary,
    TaskRun,
    TaskSchedule,
    User,
)
from backend.enums import (
    BackgroundJobType,
    MediaType,
    ScheduleType,
    Service,
    Task,
    TaskStatus,
    UserRole,
)
from backend.models.diagnostics import (
    DiagnosticsOptions,
    MediaDiagnosticsOptions,
    Target,
)
from backend.services import diagnostics as service
from backend.services.diagnostics_export import Exporter


@pytest.fixture
def database(tmp_path: Path):
    path = tmp_path / "diagnostics.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE alembic_version(version_num TEXT)")
        conn.exec_driver_sql("INSERT INTO alembic_version VALUES ('test_revision')")
    with Session(engine) as db:
        main = ServiceConfig(
            service_type=Service.PLEX,
            base_url="https://private-host/?token=URLSECRET",
            api_key="APISECRET",
            name="PrivateServer",
            enabled=True,
            is_main=True,
        )
        linked = ServiceConfig(
            service_type=Service.PLEX,
            base_url="http://private-host-two",
            api_key="OTHERSECRET",
            name="OtherPrivateServer",
            enabled=True,
        )
        db.add_all([main, linked])
        db.flush()
        history = ServiceConfig(
            service_type=Service.TAUTULLI,
            base_url="http://history-host",
            api_key="HISTORYSECRET",
            enabled=True,
            extra_settings={
                "plex_service_config_id": main.id,
                "playback_history_sync": {
                    "available": False,
                    "last_error": "password=ERRORSECRET /home/privateuser/file",
                    "last_attempt_at": datetime.now(UTC).isoformat(),
                },
            },
        )
        db.add(history)
        db.add(
            GeneralSettings(
                path_mappings=[
                    {
                        "source_prefix": "/remote",
                        "local_prefix": "/home/privateuser/movies",
                        "service_config_id": main.id,
                    }
                ]
            )
        )
        movie = Movie(title="A Test Movie", tmdb_id=123)
        series = Series(title="A Test Series", tmdb_id=321)
        db.add_all([movie, series])
        db.flush()
        versions = [
            MovieVersion(
                movie_id=movie.id,
                service=Service.PLEX,
                service_item_id="item-one",
                service_media_id=str(i),
                library_id="PrivateLibrary",
                library_name="PrivateLibrary",
                path=f"/remote/movie-{i}.mkv",
            )
            for i in range(2)
        ]
        db.add_all(versions)
        season = Season(series_id=series.id, season_number=1)
        db.add(season)
        db.flush()
        episodes = [
            Episode(season_id=season.id, episode_number=i, name=f"Episode {i}")
            for i in range(1, 3)
        ]
        db.add_all(episodes)
        db.flush()
        rule = ReclaimRule(
            name="PrivateRule",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            definition={
                "version": 1,
                "root": {
                    "type": "group",
                    "op": "and",
                    "children": [
                        {
                            "field": "playback.usernames",
                            "operator": "contains_any",
                            "value": ["privateuser", {"token": "NESTEDSECRET"}],
                        }
                    ],
                },
            },
        )
        db.add(rule)
        db.flush()
        for version in versions:
            db.add(
                ReclaimCandidate(
                    media_type=MediaType.MOVIE,
                    movie_id=movie.id,
                    movie_version_id=version.id,
                    matched_rule_ids=[rule.id],
                    matched_criteria={"secret": "CRITERIASECRET"},
                    reason="password=REASONSECRET",
                    reason_data=[
                        {
                            "conditions": [
                                {
                                    "field": "playback.usernames",
                                    "operator": "contains_any",
                                    "expected": ["privateuser"],
                                    "actual": {"token": "CONDITIONSECRET"},
                                    "display": "DISPLAYSECRET",
                                },
                                {
                                    "field": "media.size",
                                    "operator": "greater_than",
                                    "expected": 100,
                                    "actual": 200,
                                },
                            ]
                        }
                    ],
                    last_delete_error="token=DELETESECRET",
                )
            )
        for episode in episodes:
            db.add(
                ReclaimCandidate(
                    media_type=MediaType.SERIES,
                    series_id=series.id,
                    season_id=season.id,
                    episode_id=episode.id,
                    matched_rule_ids=[],
                    matched_criteria={},
                    reason="private",
                    reason_data=[],
                )
            )
        db.add(
            NativePlaybackAggregate(
                source_service=Service.PLEX,
                source_service_config_id=main.id,
                target_scope="movie_version",
                target_id=versions[0].id,
                media_type=MediaType.MOVIE,
                play_count=3,
            )
        )
        for i in range(55):
            db.add(
                PlaybackHistoryEvent(
                    source_service=Service.TAUTULLI,
                    source_service_config_id=history.id,
                    source_event_key=str(i),
                    source_item_id="item-one",
                    provider_media_type="movie",
                    played_at=datetime.now(UTC),
                    duration_seconds=100,
                    observed_service_config_id=main.id,
                    movie_id=movie.id,
                    tmdb_id=movie.tmdb_id,
                    source_user_id=str(i % 2),
                    source_username="privateuser" + str(i % 2),
                )
            )
        db.commit()
        ids = {
            "movie": movie.id,
            "movie_version": versions[0].id,
            "series": series.id,
            "season": season.id,
            "episode": episodes[0].id,
        }
    yield path, ids, engine
    engine.dispose()


def report(database, scope=None, paths=False):
    path, ids, _ = database
    return service.collect_report(
        path,
        DiagnosticsOptions(include_file_paths=paths),
        Target(scope=scope, id=ids[scope]) if scope else None,
    )


@pytest.mark.parametrize(
    "scope", [None, "movie", "movie_version", "series", "season", "episode"]
)
def test_all_scopes_collect_without_section_errors(database, scope):
    result = report(database, scope)
    assert not [o for o in result.collection.observations if o.state != "available"]
    assert result.target.scope == scope if scope else result.target is None
    assert result.sections.services[0].history_refresh.state == "unavailable"
    assert result.sections.services[1].selection_state == "not_configured"


def test_secrets_and_private_text_never_leave_backend(database):
    for scope in (None, "movie_version"):
        for include in (False, True):
            result = report(database, scope, include)
            exported = result.model_dump_json()
            for secret in (
                "APISECRET",
                "OTHERSECRET",
                "HISTORYSECRET",
                "URLSECRET",
                "ERRORSECRET",
                "NESTEDSECRET",
                "CRITERIASECRET",
                "REASONSECRET",
                "CONDITIONSECRET",
                "DISPLAYSECRET",
                "DELETESECRET",
                "PrivateLibrary",
                "PrivateRule",
                "PrivateServer",
                "private-host",
                "privateuser",
            ):
                assert secret not in exported
            if scope:
                assert "A Test Movie" in exported
                assert (
                    result.sections.decisions.rules[0].definition["root"]["conditions"][
                        0
                    ]["field"]
                    == "playback.usernames"
                )
                numeric = result.sections.decisions.candidates[0].conditions[1]
                assert (numeric.expected, numeric.actual) == (100, 200)


def test_aliases_preserve_distinct_servers_accounts_and_mapping_relationships(database):
    result = report(database, "movie_version")
    servers = result.sections.services
    assert servers[0].alias != servers[1].alias
    events = result.sections.playback.events
    assert len({e.user for e in events}) == 2
    assert len({e.item for e in events}) == 1
    assert events[0].item == result.sections.storage[0].item
    assert result.sections.storage[0].mapped_paths
    assert "/remote" not in result.model_dump_json()
    included = report(database, "movie_version", True)
    assert "/remote/movie-0.mkv" in included.model_dump_json()
    assert "/home/user-" in included.model_dump_json()


def test_exact_scope_does_not_export_sibling_decisions(database):
    for scope in ("movie_version", "episode"):
        result = report(database, scope)
        assert len(result.sections.decisions.candidates) == 1
        assert result.sections.decisions.candidates[0].target == result.target
    assert len(report(database, "movie").sections.decisions.candidates) == 2


def test_native_watch_without_selected_history_is_unknown(database):
    result = report(database, "movie_version")
    assert result.sections.playback.aggregates[0].play_count == 3
    assert result.sections.playback.imported_state == "unknown"
    assert len(result.sections.playback.events) == 50
    assert result.sections.playback.stored_event_count == 55
    assert "not the file version" in result.sections.playback.note
    limit = next(
        x for x in result.collection.truncated if x.section == "playback_events"
    )
    assert (limit.returned, limit.total) == (50, 55)


def test_generation_is_read_only(database, monkeypatch):
    path, _, _ = database
    before = path.read_bytes()
    original = sqlite3.connect
    statements = []

    def connect(*args, **kwargs):
        db = original(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(service.sqlite3, "connect", connect)
    report(database, "movie_version")
    report(database)
    assert path.read_bytes() == before
    assert all(sql.startswith(("SELECT", "PRAGMA")) for sql in statements)
    assert not any("journal_mode=" in sql.lower() for sql in statements)
    assert not any(sql.startswith("BEGIN") for sql in statements)


def test_busy_and_missing_database_keep_runtime_report(database, tmp_path):
    path, _, _ = database
    with sqlite3.connect(path) as lock:
        lock.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        result = report(database)
        assert time.monotonic() - started < 5.5
        assert result.collection.partial
        assert result.app.version
        assert any(o.reason == "database_busy" for o in result.collection.observations)
        lock.rollback()
    missing = tmp_path / "not-created.db"
    result = service.collect_report(missing, DiagnosticsOptions())
    assert result.app.version and result.collection.partial
    assert not missing.exists()


def test_missing_target_is_explicit(database):
    result = service.collect_report(
        database[0], DiagnosticsOptions(), Target(scope="movie", id=99999)
    )
    assert result.collection.partial
    assert "identity: target no longer exists" in result.collection.omitted
    assert result.sections.playback is None


def test_deadline_marks_uncollected_sections(database, monkeypatch):
    monkeypatch.setattr(service, "BUDGET_SECONDS", 0)
    result = report(database)
    assert result.collection.partial
    assert all(o.reason == "collection_timeout" for o in result.collection.observations)


def test_size_cap_keeps_valid_json(database, monkeypatch):
    monkeypatch.setattr(service, "MAX_BYTES", 8000)
    result = report(database, "movie")
    exported = result.model_dump_json(indent=2)
    assert len(exported.encode()) <= 8000
    assert json.loads(exported)["collection"]["partial"]
    assert any("report size limit" in o for o in result.collection.omitted)


def test_path_opt_in_still_omits_url_credentials_and_usernames():
    exporter = Exporter(True)
    for path in ("smb://bob:SECRET@host/share", "/movies/file?token=SECRET"):
        assert "SECRET" not in exporter.path(path)
    assert "bob" not in exporter.path(r"C:\Users\bob\movies")
    assert "secret-host" not in exporter.path(r"\\secret-host\movies")


def test_large_child_lists_are_bounded_and_navigable(database):
    path, ids, engine = database
    with Session(engine) as db:
        db.add_all(
            Episode(season_id=ids["season"], episode_number=n) for n in range(3, 153)
        )
        db.commit()
    first = report(database, "season")
    assert len(first.sections.children) == 100
    assert first.collection.children_total == 152
    target = Target(scope="season", id=ids["season"])
    second = service.collect_report(
        path, MediaDiagnosticsOptions(target=target, children_offset=100), target
    )
    assert len(second.sections.children) == 52
    assert second.sections.children[0].episode_number == 101
    assert second.collection.children_offset == 100


def test_system_tasks_libraries_and_error_flags(database):
    with Session(database[2]) as db:
        db.add(
            ServiceMediaLibrary(
                library_id="PrivateLibrary",
                library_name="Private Library",
                media_type=MediaType.MOVIE,
                service_config_id=1,
                selected=True,
            )
        )
        db.add(
            TaskSchedule(
                task=Task.SYNC_MEDIA,
                schedule_type=ScheduleType.INTERVAL,
                schedule_value="3600",
                default_schedule_type=ScheduleType.INTERVAL,
                default_schedule_value="3600",
            )
        )
        for _ in range(25):
            run = TaskRun(task=Task.SYNC_MEDIA, status=TaskStatus.ERROR)
            run.error_message = "https://admin:TASKSECRET@private-host"
            db.add(run)
        db.add(
            BackgroundJob(
                job_type=BackgroundJobType.TASK_RUN,
                scheduled_at=datetime.now(UTC),
                payload={"password": "JOBSECRET"},
                claimed_by="private-machine",
            )
        )
        db.commit()
    result = report(database)
    assert len(result.sections.recent_runs) == 20
    assert all(
        r.error == "recorded_error_text_omitted" for r in result.sections.recent_runs
    )
    assert result.sections.schedules[0].schedule_value == "3600"
    assert result.sections.active_jobs[0].queue_age_seconds >= 0
    assert result.sections.libraries[0].selected
    media = report(database, "movie_version")
    assert (
        media.sections.storage[0].library_source_id
        == result.sections.libraries[0].source_id
    )
    assert all(
        s not in result.model_dump_json()
        for s in ("TASKSECRET", "JOBSECRET", "private-machine", "Private Library")
    )


def test_tracearr_bindings_are_scoped_to_the_observed_server(database):
    with Session(database[2]) as db:
        provider = ServiceConfig(
            service_type=Service.TRACEARR,
            base_url="http://private",
            api_key="secret",
            enabled=True,
            extra_settings={
                "server_bindings": [
                    {
                        "service_config_id": 1,
                        "server_type": "plex",
                        "tracearr_server_id": "remote-private-a",
                    },
                    {
                        "service_config_id": 2,
                        "server_type": "plex",
                        "tracearr_server_id": "remote-private-b",
                    },
                ],
                "playback_history_sync": {
                    "bindings": {
                        "remote-private-a": {
                            "available": True,
                            "last_attempt_at": datetime.now(UTC).isoformat(),
                        },
                        "remote-private-b": {
                            "available": False,
                            "last_error": "password=BINDINGSECRET",
                        },
                    }
                },
            },
        )
        db.add(provider)
        db.flush()
        db.add(
            PlaybackHistoryEvent(
                source_service=Service.TRACEARR,
                source_service_config_id=provider.id,
                source_event_key="remote-private-a:1",
                source_item_id="item-one",
                provider_media_type="movie",
                played_at=datetime.now(UTC),
                duration_seconds=80,
                observed_service_config_id=1,
                movie_id=database[1]["movie"],
            )
        )
        db.commit()
    result = report(database, "movie_version")
    first, second = result.sections.services[:2]
    assert first.selected_provider == second.selected_provider == "tracearr"
    assert first.remote_server != second.remote_server
    assert first.history_refresh.state == "available"
    assert second.history_refresh.state == "unavailable"
    assert result.sections.playback.events[0].selected_source is True
    assert any(e.selected_source is False for e in result.sections.playback.events)
    assert "remote-private" not in result.model_dump_json()
    assert "BINDINGSECRET" not in result.model_dump_json()


def test_oversized_saved_json_is_reported_without_leaking(database):
    with database[2].begin() as conn:
        conn.exec_driver_sql(
            "UPDATE service_configs SET extra_settings=? WHERE id=1",
            (json.dumps({"secret": "HIDDEN" * 20000}),),
        )
    result = report(database)
    assert result.collection.partial
    assert any(
        "extra_settings: saved JSON" in value for value in result.collection.omitted
    )
    assert "HIDDEN" not in result.model_dump_json()
    assert result.sections.services[0].selection_state == "unavailable"


def test_deadline_interrupts_expensive_sql(database):
    collector = service.Collector(database[0], DiagnosticsOptions(), None)
    collector.db = sqlite3.connect(database[0], isolation_level=None)
    collector.db.row_factory = sqlite3.Row
    collector.deadline = time.monotonic() + 0.05
    collector.db.set_progress_handler(
        lambda: int(time.monotonic() >= collector.deadline), 100
    )
    started = time.monotonic()
    try:
        collector.section(
            "playback",
            lambda: collector.query(
                "WITH RECURSIVE numbers(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM numbers WHERE n < 100000000) SELECT sum(n) FROM numbers"
            ),
        )
        assert time.monotonic() - started < 1
        assert (
            collector.report.collection.observations[0].reason == "collection_timeout"
        )
    finally:
        collector.db.close()


def test_diagnostics_auth_does_not_touch_session_and_checks_revocation(monkeypatch):
    import asyncio
    from datetime import timedelta
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from starlette.requests import Request
    from fastapi import HTTPException
    from backend.core import auth
    from backend.database.models import UserSession

    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)

        async def fail_touch(*args):
            raise AssertionError("diagnostics must not touch session activity")

        monkeypatch.setattr(auth, "_touch_user_session_last_seen", fail_touch)
        async with maker() as db:
            user = User(username="admin", password_hash="unused", role=UserRole.ADMIN)
            db.add(user)
            await db.flush()
            session = UserSession(
                user_id=user.id,
                session_id="diagnostics-test",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                last_seen_at=datetime.now(UTC) - timedelta(hours=1),
            )
            db.add(session)
            await db.commit()
            token = auth.create_access_token(
                data={"sub": str(user.id)},
                token_version=user.token_version,
                session_id=session.session_id,
            )
            request = Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/diagnostics/system",
                    "headers": [(b"cookie", f"{auth.COOKIE_NAME}={token}".encode())],
                    "query_string": b"",
                    "scheme": "http",
                    "server": ("localhost", 80),
                }
            )
            assert (await auth.get_current_user(request, db)).id == user.id
            session.revoked_at = datetime.now(UTC)
            await db.commit()
            with pytest.raises(HTTPException) as exc:
                await auth.get_current_user(request, db)
            assert exc.value.status_code == 401
        await engine.dispose()

    asyncio.run(run())


def test_api_permissions_validation_and_limits(database, monkeypatch):
    app = FastAPI()
    app.include_router(route.router)
    client = TestClient(app)
    assert client.post("/api/diagnostics/system", json={}).status_code == 401
    user = User(username="regular", password_hash="unused", role=UserRole.USER)
    app.dependency_overrides[get_current_user] = lambda: user
    assert client.post("/api/diagnostics/system", json={}).status_code == 403
    assert (
        client.post(
            "/api/diagnostics/media", json={"target": {"scope": "movie", "id": 1}}
        ).status_code
        == 403
    )
    user.role = UserRole.ADMIN
    monkeypatch.setattr(route, "settings", SimpleNamespace(db_path=database[0]))
    response = client.post("/api/diagnostics/system", json={})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert (
        client.post(
            "/api/diagnostics/media", json={"target": {"scope": "movie", "id": 0}}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/diagnostics/media", json={"target": {"scope": "user", "id": 1}}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/diagnostics/system", json={"include_secrets": True}
        ).status_code
        == 422
    )
    route.generation_slots.acquire()
    route.generation_slots.acquire()
    try:
        assert client.post("/api/diagnostics/system", json={}).status_code == 429
    finally:
        route.generation_slots.release()
        route.generation_slots.release()
