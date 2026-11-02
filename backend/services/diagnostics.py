"""Bounded stored-state diagnostics. No provider, scheduler or write entry points."""

from __future__ import annotations

import json
import platform
import re
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from backend.core.__version__ import __version__
from backend.core.rule_actions import get_arr_service_config_ids
from backend.core.rule_engine import FIELD_LABELS, OPERATOR_LABELS
from backend.core.utils.filesystem import mapped_path_variants
from backend.database.models import ServiceConfig
from backend.enums import (
    BackgroundJobStatus,
    BackgroundJobType,
    MediaType,
    ProtectionRequestStatus,
    ScheduleType,
    Service,
    Task,
    TaskStatus,
)
from backend.models import diagnostics as m
from backend.services.diagnostics_export import Exporter, timestamp
from backend.services.media_identity import MediaIdentityOwnership
from backend.services.playback_selection import HistorySource, resolve_history_sources

MAX_BYTES = 512 * 1024
BUDGET_SECONDS = 5.0
MAX_ROWS = 100
MAX_JSON_CELL = 65536
# Per-process admission control; reject rather than queue expensive reports.
generation_slots = threading.BoundedSemaphore(2)
TABLES = {
    "movie": "movies",
    "movie_version": "movie_versions",
    "series": "series",
    "season": "seasons",
    "episode": "episodes",
}


def now() -> datetime:
    return datetime.now(UTC)


def enum_value(enum: Any, value: object) -> str:
    try:
        return enum[str(value)].value
    except KeyError:
        try:
            return enum(value).value
        except (ValueError, TypeError):
            return "unknown"


def object_json(value: object) -> Any:
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except (ValueError, RecursionError):
        return None


def integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def target_of(row: dict[str, Any]) -> m.Target:
    for scope in ("episode", "movie_version", "season", "movie", "series"):
        if row.get(scope + "_id"):
            return m.Target(scope=scope, id=row[scope + "_id"])  # type: ignore[arg-type]
    raise ValueError("missing target")


class Collector:
    def __init__(
        self, path: Path, options: m.DiagnosticsOptions, target: m.Target | None
    ):
        self.path = path
        self.history_days = options.history_days
        self.event_limit = options.event_limit
        self.children_offset = (
            options.children_offset
            if isinstance(options, m.MediaDiagnosticsOptions)
            else 0
        )
        self.export = Exporter(options.include_file_paths)
        self.deadline = time.monotonic() + BUDGET_SECONDS
        self.db: sqlite3.Connection | None = None
        self.configs: list[ServiceConfig] = []
        self.sources: dict[int, HistorySource] = {}
        self.mappings: list[dict[str, Any]] = []
        self.context: dict[str, Any] = {}
        self.report = m.DiagnosticsReport(
            report_kind="media" if target else "system",
            report_id=str(uuid4()),
            generated_at=now(),
            target=target,
            app=m.AppInfo(
                version=str(__version__),
                python=platform.python_version(),
                platform=platform.system(),
                deployment="desktop_bundle"
                if getattr(sys, "frozen", False)
                else "container"
                if Path("/.dockerenv").is_file()
                else "source_or_package",
            ),
            collection=m.Collection(
                started_at=now(), budget_seconds=BUDGET_SECONDS, max_bytes=MAX_BYTES
            ),
            sharing=m.Sharing(file_paths_included=options.include_file_paths),
        )
        if not options.include_file_paths:
            self.report.sharing.masked_categories.append("file_paths")

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        if time.monotonic() >= self.deadline:
            raise TimeoutError
        if self.db is None:
            raise sqlite3.OperationalError("database unavailable")
        # Autocommit SELECTs release their read locks as soon as the cursor closes.
        cursor = self.db.execute(sql, params)
        try:
            rows = [dict(row) for row in cursor.fetchall()]
            for row in rows:
                for key, oversized in row.items():
                    if key.endswith("_oversized") and oversized:
                        reason = (
                            key.removesuffix("_oversized")
                            + ": saved JSON exceeds collection limit"
                        )
                        if reason not in self.report.collection.omitted:
                            self.report.collection.omitted.append(reason)
            return rows
        finally:
            cursor.close()

    def rows(
        self,
        section: str,
        table: str,
        columns: str,
        where: str = "1",
        params: tuple = (),
        limit: int = MAX_ROWS,
        order: str = "id",
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        # SQL identifiers/expressions are internal constants, never request values.
        rows = self.query(
            f"SELECT {columns} FROM {table} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, limit + 1, offset),
        )
        if len(rows) > limit or offset:
            try:
                total = self.query(
                    f"SELECT count(*) AS n FROM {table} WHERE {where}", params
                )[0]["n"]
            except (sqlite3.Error, TimeoutError):
                total = None
            self.report.collection.truncated.append(
                m.Limit(section=section, returned=limit, total=total)
            )
        return rows[:limit]

    def json_column(self, name: str) -> str:
        return (
            f"CASE WHEN length({name}) <= {MAX_JSON_CELL} THEN {name} ELSE NULL END AS {name}, "
            f"length({name}) > {MAX_JSON_CELL} AS {name}_oversized"
        )

    def section(self, name: str, collect: Callable[[], Any]) -> None:
        observed = now()
        try:
            if time.monotonic() >= self.deadline:
                raise TimeoutError
            result = collect()
            setattr(self.report.sections, name, result)
            self.report.collection.observations.append(
                m.Observation(section=name, observed_at=observed, state="available")
            )
        except Exception as exc:
            reason = (
                "collection_timeout"
                if isinstance(exc, TimeoutError) or time.monotonic() >= self.deadline
                else "database_busy"
                if isinstance(exc, sqlite3.OperationalError)
                and getattr(exc, "sqlite_errorcode", None)
                in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                else "section_unavailable"
            )
            self.report.collection.partial = True
            self.report.collection.observations.append(
                m.Observation(
                    section=name,
                    observed_at=observed,
                    state="unavailable",
                    reason=reason,
                )
            )

    def refresh(self, raw: object, *, configured: bool = True) -> m.Refresh:
        if not configured:
            return m.Refresh(state="not_configured")
        if not isinstance(raw, dict) or not raw:
            return m.Refresh()
        attempted = timestamp(raw.get("last_attempt_at"))
        available = (
            raw.get("available") if isinstance(raw.get("available"), bool) else None
        )
        state: m.EvidenceState = (
            "available"
            if available is True
            else "unavailable"
            if available is False
            else "unknown"
        )
        if available and (attempted is None or now() - attempted > timedelta(hours=24)):
            state = "stale"
        return m.Refresh(
            state=state,
            available=available,
            last_attempt_at=attempted,
            last_success_at=timestamp(raw.get("last_success_at")),
            last_event_at=timestamp(raw.get("last_event_at")),
            imported_events=integer(raw.get("imported_events")),
            format_version=integer(raw.get("format_version")),
            aggregate_format_version=integer(raw.get("aggregate_format_version")),
            error="provider_reported_error" if raw.get("last_error") else None,
        )

    def services(self) -> list[m.ServiceInfo]:
        rows = self.rows(
            "services",
            "service_configs",
            "id, service_type, enabled, is_main, last_synced_at, "
            + self.json_column("extra_settings"),
        )
        selection_complete = not any(
            r.get("extra_settings_oversized") for r in rows
        ) and not any(t.section == "services" for t in self.report.collection.truncated)
        for row in rows:
            extra = object_json(row["extra_settings"])
            if extra is not None and not isinstance(extra, dict):
                selection_complete = False
                self.report.collection.omitted.append(
                    "services: invalid saved settings structure"
                )
            service = Service(enum_value(Service, row["service_type"]))
            config = ServiceConfig(
                service_type=service,
                base_url="",
                api_key="",
                name=str(self.export.alias("server", row["id"])),
                enabled=bool(row["enabled"]),
                is_main=bool(row["is_main"]),
                extra_settings=extra if isinstance(extra, dict) else {},
            )
            config.id = row["id"]
            self.configs.append(config)
        self.sources = (
            resolve_history_sources(self.configs) if selection_complete else {}
        )
        by_id = {c.id: c for c in self.configs}
        result = []
        for row, config in zip(rows, self.configs):
            source = self.sources.get(config.id)
            history = None
            if source and source.provider_config_id in by_id:
                history = (by_id[source.provider_config_id].extra_settings or {}).get(
                    "playback_history_sync"
                )
                if source.remote_server_id and isinstance(history, dict):
                    history = (history.get("bindings") or {}).get(
                        source.remote_server_id
                    )
            result.append(
                m.ServiceInfo(
                    id=config.id,
                    alias=config.name,
                    service=config.service_type.value,
                    enabled=config.enabled,
                    is_main=config.is_main,
                    last_synced_at=timestamp(row["last_synced_at"]),
                    selected_provider=source.provider_key if source else None,
                    selection_preference=(
                        source.preference
                        if source.preference
                        in ("automatic", "tautulli", "tracearr", "playback_reporting")
                        else "unknown"
                    )
                    if source
                    else None,
                    provider_config_id=source.provider_config_id if source else None,
                    remote_server=self.export.alias(
                        "remote-server", source.remote_server_id
                    )
                    if source
                    else None,
                    selection_state="available"
                    if source and source.provider
                    else "not_configured"
                    if selection_complete
                    else "unavailable",
                    native_refresh=self.refresh(
                        (config.extra_settings or {}).get("native_playback_sync")
                    ),
                    history_refresh=self.refresh(
                        history, configured=bool(source and source.provider)
                    )
                    if selection_complete
                    else m.Refresh(state="unavailable"),
                )
            )
        return result

    def libraries(self) -> list[m.LibraryInfo]:
        return [
            m.LibraryInfo(
                id=r["id"],
                alias=str(
                    self.export.alias(
                        "library", (r["service_config_id"], r["library_id"])
                    )
                ),
                source_id=str(self.export.alias("library-reference", r["library_id"])),
                server_id=r["service_config_id"],
                selected=bool(r["selected"]),
                media_type=enum_value(MediaType, r["media_type"]),
            )
            for r in self.rows(
                "libraries",
                "service_media_libraries",
                "id,library_id,service_config_id,selected,media_type",
            )
        ]

    def configuration(self) -> m.Configuration:
        columns = [
            "active_playback_protection_enabled",
            "playback_unavailable_protection_enabled",
            "media_server_fallback_enabled",
            "auto_delete_movie_delay_days",
            "auto_delete_series_delay_days",
            "playback_movie_min_seconds",
            "playback_episode_min_seconds",
        ]
        rows = self.query(
            "SELECT "
            + ",".join(columns)
            + ",move_destination_movies,move_destination_series,"
            + self.json_column("path_mappings")
            + " FROM general_settings LIMIT 1"
        )
        if not rows:
            raise ValueError("settings not initialized")
        row = rows[0]
        raw = object_json(row["path_mappings"])
        if isinstance(raw, list):
            self.mappings = [v for v in raw[:100] if isinstance(v, dict)]
            if len(raw) > 100:
                self.report.collection.truncated.append(
                    m.Limit(section="path_mappings", returned=100, total=len(raw))
                )
        elif row["path_mappings"] is None:
            self.report.collection.omitted.append(
                "path_mappings: missing or oversized saved value"
            )
        return m.Configuration(
            **{k: row[k] for k in columns},
            move_destination_movies=self.export.path(row["move_destination_movies"]),
            move_destination_series=self.export.path(row["move_destination_series"]),
            path_mappings=[
                m.PathMapping(
                    source=self.export.path(v.get("source_prefix")),
                    destination=self.export.path(v.get("local_prefix")),
                    service=enum_value(Service, v["service_type"])
                    if v.get("service_type")
                    else None,
                    server_id=integer(v.get("service_config_id")),
                )
                for v in self.mappings
            ],
        )

    def database(self) -> m.DatabaseInfo:
        pragmas = {
            name: next(iter(self.query("PRAGMA " + name)[0].values()))
            for name in (
                "journal_mode",
                "query_only",
                "busy_timeout",
                "foreign_keys",
                "synchronous",
            )
        }
        revisions = [
            r["version_num"]
            for r in self.query("SELECT version_num FROM alembic_version LIMIT 10")
            if re.fullmatch(r"[a-zA-Z0-9_]{1,64}", r["version_num"])
        ]

        def size(path: Path) -> int | None:
            try:
                return path.stat().st_size
            except OSError:
                return None

        return m.DatabaseInfo(
            sqlite_version=sqlite3.sqlite_version,
            migration_revisions=revisions,
            **pragmas,
            database_bytes=size(self.path),
            wal_bytes=size(Path(str(self.path) + "-wal")),
        )

    def schedules(self) -> list[m.ScheduleInfo]:
        return [
            m.ScheduleInfo(
                id=r["id"],
                task=enum_value(Task, r["task"]),
                enabled=bool(r["enabled"]),
                schedule_type=enum_value(ScheduleType, r["schedule_type"]),
                schedule_value=r["schedule_value"]
                if re.fullmatch(r"[\d*/?,\- A-Za-z]{1,100}", r["schedule_value"])
                and not re.search(r"[A-Za-z]{4}", r["schedule_value"])
                else "omitted",
            )
            for r in self.rows(
                "schedules",
                "task_schedules",
                "id,task,enabled,schedule_type,schedule_value",
            )
        ]

    def runs(self) -> list[m.RunInfo]:
        return [
            m.RunInfo(
                id=r["id"],
                task=enum_value(Task, r["task"]),
                status=enum_value(TaskStatus, r["status"]),
                started_at=timestamp(r["started_at"]),
                completed_at=timestamp(r["completed_at"]),
                items_processed=r["items_processed"],
                error="recorded_error_text_omitted" if r["has_error"] else None,
            )
            for r in self.rows(
                "recent_runs",
                "task_runs",
                "id,task,status,started_at,completed_at,items_processed,error_message IS NOT NULL AS has_error",
                limit=20,
                order="id DESC",
            )
        ]

    def jobs(self) -> list[m.JobInfo]:
        result = []
        for r in self.rows(
            "active_jobs",
            "background_jobs",
            "id,job_type,status,attempts,created_at,scheduled_at,started_at,completed_at,error_message IS NOT NULL AS has_error",
            "status IN ('PENDING','RUNNING')",
            order="created_at,id",
        ):
            created = timestamp(r["created_at"])
            result.append(
                m.JobInfo(
                    id=r["id"],
                    job_type=enum_value(BackgroundJobType, r["job_type"]),
                    status=enum_value(BackgroundJobStatus, r["status"]),
                    attempts=r["attempts"],
                    created_at=created,
                    scheduled_at=timestamp(r["scheduled_at"]),
                    started_at=timestamp(r["started_at"]),
                    completed_at=timestamp(r["completed_at"]),
                    queue_age_seconds=max(0, (now() - created).total_seconds())
                    if created and r["status"] == "PENDING"
                    else None,
                    error="recorded_error_text_omitted" if r["has_error"] else None,
                )
            )
        return result

    def identity_row(self, scope: m.Scope, row: dict[str, Any]) -> m.Identity:
        fields = {
            key: row[key]
            for key in (
                "id",
                "tmdb_id",
                "tvdb_id",
                "movie_id",
                "series_id",
                "season_id",
                "season_number",
                "episode_number",
                "size",
                "video_width",
                "video_height",
                "view_count",
            )
            if key in row
        }
        imdb = row.get("imdb_id")
        return m.Identity(
            **fields,
            scope=scope,
            title=self.export.title(
                row.get("name") if scope == "episode" else row.get("title")
            ),
            parent_title=self.export.title(row.get("title"))
            if scope == "episode"
            else None,
            imdb_id=imdb
            if isinstance(imdb, str) and re.fullmatch(r"tt\d+", imdb)
            else None,
            path=self.export.path(row.get("path")),
            removed_at=timestamp(row.get("removed_at")),
            last_viewed_at=timestamp(row.get("last_viewed_at")),
            updated_at=timestamp(
                row.get("updated_at", row.get("last_metadata_refresh_at"))
            ),
        )

    def identity_columns(self, scope: str) -> str:
        return {
            "movie": "id,title,tmdb_id,imdb_id,size,view_count,last_viewed_at,removed_at,last_metadata_refresh_at",
            "series": "id,title,tmdb_id,tvdb_id,imdb_id,size,view_count,last_viewed_at,removed_at,last_metadata_refresh_at",
            "movie_version": "id,movie_id,path,size,video_width,video_height,updated_at",
            "season": "id,series_id,season_number,path,size,view_count,last_viewed_at,updated_at",
            "episode": "id,season_id,episode_number,name,path,size,view_count,last_viewed_at,updated_at",
        }[scope]

    def identity(self) -> m.Identity:
        target = self.report.target
        assert target is not None
        rows = self.query(
            f"SELECT {self.identity_columns(target.scope)} FROM {TABLES[target.scope]} WHERE id=?",
            (target.id,),
        )
        if not rows:
            self.report.collection.omitted.append("identity: target no longer exists")
            return m.Identity(id=target.id, scope=target.scope)
        row = rows[0]
        self.context = dict(row)
        if target.scope == "episode":
            season = self.query(
                "SELECT series_id,season_number FROM seasons WHERE id=?",
                (row["season_id"],),
            )[0]
            self.context.update(season)
        if target.scope in ("season", "episode", "movie_version"):
            parent = "movie" if target.scope == "movie_version" else "series"
            parent_id = self.context[parent + "_id"]
            parent_row = self.query(
                f"SELECT title,tmdb_id,imdb_id,removed_at FROM {TABLES[parent]} WHERE id=?",
                (parent_id,),
            )[0]
            self.context.update(parent_row)
            row.update({k: v for k, v in parent_row.items() if k not in row})
            if "series_id" in self.context:
                row["series_id"] = self.context["series_id"]
            if "season_number" in self.context:
                row["season_number"] = self.context["season_number"]
        else:
            self.context[target.scope + "_id"] = target.id
        return self.identity_row(target.scope, row)

    def children(self) -> list[m.Identity]:
        target = self.report.target
        assert target is not None
        if target.scope not in ("movie", "series", "season"):
            return []
        scope: m.Scope = {
            "movie": "movie_version",
            "series": "season",
            "season": "episode",
        }[target.scope]  # type: ignore[assignment]
        self.report.collection.children_offset = self.children_offset
        self.report.collection.children_total = self.query(
            f"SELECT count(*) AS n FROM {TABLES[scope]} WHERE {target.scope}_id=?",
            (target.id,),
        )[0]["n"]
        return [
            self.identity_row(scope, r)
            for r in self.rows(
                "children",
                TABLES[scope],
                self.identity_columns(scope),
                target.scope + "_id=?",
                (target.id,),
                order="season_number,id"
                if scope == "season"
                else "episode_number,id"
                if scope == "episode"
                else "id",
                offset=self.children_offset,
            )
        ]

    def ownership(self) -> MediaIdentityOwnership:
        main = next((c for c in self.configs if c.is_main), None)
        return MediaIdentityOwnership(
            main.id if main else None,
            main.service_type if main else None,
            {c.id: c.service_type for c in self.configs},
        )

    def storage(self) -> list[m.StorageIdentity]:
        target = self.report.target
        assert target is not None
        if not self.context:
            raise ValueError("identity unavailable")
        result = []
        ownership = self.ownership()
        if target.scope in ("movie", "movie_version"):
            table, kind, where = (
                "movie_versions",
                "movie_version",
                "movie_id=?" if target.scope == "movie" else "id=?",
            )
        elif target.scope == "episode":
            table, kind, where = "episode_versions", "episode_version", "episode_id=?"
        else:
            table, kind, where = "series_service_refs", "series_ref", "series_id=?"
        item_col = "service_id" if kind == "series_ref" else "service_item_id"
        ref_id = (
            self.context.get("series_id", target.id)
            if kind == "series_ref"
            else target.id
        )
        for r in self.rows(
            "storage",
            table,
            f"id,service,{item_col} AS item,library_id,path,updated_at"
            + (",size" if kind != "series_ref" else ""),
            where,
            (ref_id,),
        ):
            service = Service(enum_value(Service, r["service"]))
            owner_ids = ownership.configs_owning_media_rows(service)
            owner = next(iter(owner_ids)) if len(owner_ids) == 1 else None
            variants = mapped_path_variants(
                r["path"],
                self.mappings,
                service_type=service.value,
                service_config_id=owner,
            )
            result.append(
                m.StorageIdentity(
                    id=r["id"],
                    kind=kind,
                    service=service.value,
                    server_id=owner,
                    item=self.export.alias("item", (owner, r["item"])),
                    library=self.export.alias("library", (owner, r["library_id"])),
                    library_source_id=self.export.alias(
                        "library-reference", r["library_id"]
                    ),
                    path=self.export.path(r["path"]),
                    mapped_paths=[str(self.export.path(v)) for v in sorted(variants)],
                    size=r.get("size"),
                    updated_at=timestamp(r["updated_at"]),
                )
            )
        field = (
            "movie_id"
            if target.scope in ("movie", "movie_version")
            else target.scope + "_id"
        )
        identifier = (
            self.context["movie_id"] if target.scope == "movie_version" else target.id
        )
        for r in self.rows(
            "linked_storage",
            "supplemental_media_matches",
            "id,source_service,source_service_config_id,source_item_id,updated_at",
            field + "=?",
            (identifier,),
        ):
            result.append(
                m.StorageIdentity(
                    id=r["id"],
                    kind="linked_match",
                    service=enum_value(Service, r["source_service"]),
                    server_id=r["source_service_config_id"],
                    item=self.export.alias(
                        "item", (r["source_service_config_id"], r["source_item_id"])
                    ),
                    updated_at=timestamp(r["updated_at"]),
                )
            )
        parent = "movie" if "movie_id" in self.context else "series"
        for r in self.rows(
            "arr_storage",
            parent + "_arr_refs",
            f"id,service_config_id,arr_{parent}_id AS item,arr_{parent}_path AS path,updated_at",
            parent + "_id=?",
            (self.context[parent + "_id"],),
        ):
            variants = mapped_path_variants(
                r["path"], self.mappings, service_config_id=r["service_config_id"]
            )
            result.append(
                m.StorageIdentity(
                    id=r["id"],
                    kind="arr_ref",
                    server_id=r["service_config_id"],
                    item=self.export.alias("item", (r["service_config_id"], r["item"])),
                    path=self.export.path(r["path"]),
                    mapped_paths=[str(self.export.path(v)) for v in sorted(variants)],
                    updated_at=timestamp(r["updated_at"]),
                )
            )
        return result

    def playback(self) -> m.Playback:
        target = self.report.target
        if target is None:
            counts = self.query(
                "SELECT count(*) AS total, coalesce(sum(movie_id IS NULL AND episode_id IS NULL),0) AS unmatched FROM playback_history_events"
            )[0]
            return m.Playback(
                stored_event_count=counts["total"],
                unmatched_event_count=counts["unmatched"],
            )
        if not self.context:
            raise ValueError("identity unavailable")
        result = m.Playback()
        if target.scope == "movie":
            where, params = (
                "target_scope='movie_version' AND target_id IN (SELECT id FROM movie_versions WHERE movie_id=?)",
                (target.id,),
            )
        else:
            where, params = "target_scope=? AND target_id=?", (target.scope, target.id)
        aggregate_tables: tuple[tuple[str, Literal["native", "imported"]], ...] = (
            ("native_playback_aggregates", "native"),
            ("playback_history_aggregates", "imported"),
        )
        for table, kind in aggregate_tables:
            columns = (
                "target_scope,target_id,play_count,unique_user_count,last_activity_at,"
                + (
                    "source_service_config_id,refreshed_at"
                    if kind == "native"
                    else "total_duration_seconds,updated_at"
                )
            )
            for r in self.rows("playback_" + kind, table, columns, where, params):
                refreshed = timestamp(r.get("refreshed_at", r.get("updated_at")))
                state: m.EvidenceState = (
                    "stale"
                    if refreshed is None or now() - refreshed > timedelta(hours=24)
                    else "zero"
                    if r["play_count"] == 0
                    else "available"
                )
                result.aggregates.append(
                    m.Aggregate(
                        kind=kind,
                        target=m.Target(scope=r["target_scope"], id=r["target_id"]),
                        server_id=r.get("source_service_config_id"),
                        state=state,
                        play_count=r["play_count"],
                        unique_user_count=r["unique_user_count"],
                        last_activity_at=timestamp(r["last_activity_at"]),
                        refreshed_at=refreshed,
                        total_duration_seconds=r.get("total_duration_seconds"),
                        note="Stored native snapshot."
                        if kind == "native"
                        else "Legacy combined aggregate; not authoritative for current provider selection.",
                    )
                )
        native = [a for a in result.aggregates if a.kind == "native"]
        result.native_state = (
            "stale"
            if any(a.state == "stale" for a in native)
            else "available"
            if native
            else "unknown"
        )
        # Never turn a missing aggregate or unmatched event into a zero.
        result.imported_state = (
            "unknown"
            if any(s.provider for s in self.sources.values())
            else "not_configured"
            if self.report.sections.services is not None
            else "unavailable"
        )
        if any(
            s.selection_state == "unavailable"
            for s in self.report.sections.services or []
        ):
            result.imported_state = "unavailable"
        scope = "movie" if target.scope == "movie_version" else target.scope
        identifier = (
            self.context["movie_id"] if target.scope == "movie_version" else target.id
        )
        linked = scope + "_id=?"
        canonical = "tmdb_id=? AND provider_media_type=?"
        args: tuple = (
            identifier,
            self.context.get("tmdb_id"),
            "movie" if scope == "movie" else "episode",
        )
        if scope in ("season", "episode"):
            canonical += " AND season_number=?"
            args += (self.context["season_number"],)
        if scope == "episode":
            canonical += " AND episode_number=?"
            args += (self.context["episode_number"],)
        event_where = (
            f"(({linked}) OR ({canonical})) AND played_at >= ? AND played_at <= ?"
        )
        args += (
            (self.report.generated_at - timedelta(days=self.history_days))
            .replace(tzinfo=None)
            .isoformat(" "),
            self.report.generated_at.replace(tzinfo=None).isoformat(" "),
        )
        columns = (
            "id,source_service,source_service_config_id,source_event_key,source_item_id,observed_service_config_id,source_user_id,source_username,played_at,duration_seconds,completed,"
            + scope
            + "_id AS stored_id"
        )
        for r in self.rows(
            "playback_events",
            "playback_history_events",
            columns,
            event_where,
            args,
            limit=self.event_limit,
            order="played_at DESC,id DESC",
        ):
            source = self.sources.get(r["observed_service_config_id"])
            selected = None
            if source:
                selected = (
                    source.provider_config_id == r["source_service_config_id"]
                    and source.provider is not None
                    and source.provider.value
                    == enum_value(Service, r["source_service"])
                    and (
                        source.remote_server_id is None
                        or r["source_event_key"].startswith(
                            source.remote_server_id + ":"
                        )
                    )
                )
            user = r["source_user_id"] or r["source_username"]
            result.events.append(
                m.EventInfo(
                    id=r["id"],
                    provider=enum_value(Service, r["source_service"]),
                    provider_config_id=r["source_service_config_id"],
                    observed_server_id=r["observed_service_config_id"],
                    item=str(
                        self.export.alias(
                            "item",
                            (r["observed_service_config_id"], r["source_item_id"]),
                        )
                    ),
                    user=self.export.alias(
                        "account", (r["source_service_config_id"], user)
                    )
                    if user
                    else None,
                    username=self.export.alias("user", r["source_username"]),
                    played_at=timestamp(r["played_at"]),
                    duration_seconds=r["duration_seconds"],
                    completed=r["completed"],
                    selected_source=selected,
                    association="stored_target_link"
                    if r["stored_id"] == identifier
                    else "canonical_id_only",
                )
            )
        result.stored_event_count = self.query(
            "SELECT count(*) AS n FROM playback_history_events WHERE " + linked,
            (identifier,),
        )[0]["n"]
        if target.scope == "movie_version":
            result.note += (
                " Event links identify the movie, not the file version played."
            )
        return result

    def decision_filter(self) -> tuple[str, tuple]:
        target = self.report.target
        assert target is not None
        if not self.context:
            raise ValueError("identity unavailable")
        if target.scope in ("movie", "series"):
            return target.scope + "_id=?", (target.id,)
        if target.scope == "movie_version":
            return "movie_id=? AND (movie_version_id=? OR movie_version_id IS NULL)", (
                self.context["movie_id"],
                target.id,
            )
        if target.scope == "season":
            return "series_id=? AND (season_id=? OR season_id IS NULL)", (
                self.context["series_id"],
                target.id,
            )
        return (
            "series_id=? AND ((episode_id=?) OR (episode_id IS NULL AND (season_id=? OR season_id IS NULL)))",
            (self.context["series_id"], target.id, self.context["season_id"]),
        )

    def decisions(self) -> m.Decisions:
        where, params = self.decision_filter()
        ids = "id,movie_id,movie_version_id,series_id,season_id,episode_id"
        result = m.Decisions()
        rule_ids: set[int] = set()
        columns = (
            ids
            + ",reviewed,approved_for_deletion,created_at,updated_at,delete_attempts,last_delete_attempt_at,last_delete_error IS NOT NULL AS has_error,auto_delete_cancelled_at,auto_delete_postponed_until,auto_delete_timer_started_at,delete_request_id,"
            + self.json_column("reason_data")
            + ","
            + self.json_column("matched_rule_ids")
        )
        for r in self.rows("candidates", "reclaim_candidates", columns, where, params):
            matched = object_json(r["matched_rule_ids"])
            matched = (
                [v for v in matched[:100] if isinstance(v, int)]
                if isinstance(matched, list)
                else []
            )
            rule_ids.update(matched)
            conditions = []
            reasons = object_json(r["reason_data"])
            for reason in reasons[:100] if isinstance(reasons, list) else []:
                if not isinstance(reason, dict):
                    continue
                raw_conditions = reason.get("conditions", [])
                for c in (
                    raw_conditions[:100] if isinstance(raw_conditions, list) else []
                ):
                    if (
                        not isinstance(c, dict)
                        or not isinstance(c.get("field"), str)
                        or c["field"] not in FIELD_LABELS
                    ):
                        continue
                    field = c["field"]
                    conditions.append(
                        m.Condition(
                            rule_id=integer(reason.get("rule_id")),
                            field=field,
                            field_label=FIELD_LABELS[field],
                            operator_label=OPERATOR_LABELS.get(
                                c["operator"], "Unknown operator"
                            )
                            if isinstance(c.get("operator"), str)
                            else "Unknown operator",
                            operator=c.get("operator")
                            if isinstance(c.get("operator"), str)
                            and c["operator"] in OPERATOR_LABELS
                            else "unknown",
                            expected=self.export.literal(c.get("expected"), field),
                            actual=self.export.literal(c.get("actual"), field),
                        )
                    )
            result.candidates.append(
                m.CandidateInfo(
                    id=r["id"],
                    target=target_of(r),
                    matched_rule_ids=matched,
                    recorded_at=timestamp(r["created_at"]),
                    last_updated_at=timestamp(r["updated_at"]),
                    reviewed=bool(r["reviewed"]),
                    approved_for_deletion=bool(r["approved_for_deletion"]),
                    delete_attempts=r["delete_attempts"],
                    last_delete_attempt_at=timestamp(r["last_delete_attempt_at"]),
                    delete_error="recorded_error_text_omitted"
                    if r["has_error"]
                    else None,
                    conditions=conditions[:100],
                    conditions_state="recorded" if reasons else "not_collected",
                    auto_delete_cancelled_at=timestamp(r["auto_delete_cancelled_at"]),
                    auto_delete_postponed_until=timestamp(
                        r["auto_delete_postponed_until"]
                    ),
                    auto_delete_timer_started_at=timestamp(
                        r["auto_delete_timer_started_at"]
                    ),
                    delete_request_id=r["delete_request_id"],
                )
            )
        for r in self.rows(
            "protections",
            "protected_media",
            ids + ",permanent,expires_at,source_rule_id",
            where,
            params,
        ):
            result.protections.append(
                m.ProtectionInfo(
                    id=r["id"],
                    target=target_of(r),
                    permanent=bool(r["permanent"]),
                    expires_at=timestamp(r["expires_at"]),
                    source_rule_id=r["source_rule_id"],
                )
            )
            if r["source_rule_id"]:
                rule_ids.add(r["source_rule_id"])
        for r in self.rows(
            "deletion_requests",
            "delete_requests",
            ids
            + ",status,reviewed_at,executed_at,execution_error IS NOT NULL AS has_error",
            where,
            params,
        ):
            result.deletion_requests.append(
                m.ReviewInfo(
                    id=r["id"],
                    target=target_of(r),
                    status=enum_value(ProtectionRequestStatus, r["status"]),
                    reviewed_at=timestamp(r["reviewed_at"]),
                    executed_at=timestamp(r["executed_at"]),
                    error="recorded_error_text_omitted" if r["has_error"] else None,
                )
            )
        for r in self.rows(
            "protection_requests",
            "protection_requests",
            ids + ",status,reviewed_at",
            where,
            params,
        ):
            result.protection_requests.append(
                m.ReviewInfo(
                    id=r["id"],
                    target=target_of(r),
                    status=enum_value(ProtectionRequestStatus, r["status"]),
                    reviewed_at=timestamp(r["reviewed_at"]),
                )
            )
        if rule_ids:
            selected_ids = sorted(rule_ids)[:100]
            for r in self.rows(
                "rules",
                "reclaim_rules",
                "id,enabled,target_scope,"
                + self.json_column("definition")
                + ","
                + self.json_column("action"),
                "id IN (" + ",".join("?" for _ in selected_ids) + ")",
                tuple(selected_ids),
            ):
                action = object_json(r["action"])
                safe_action = None
                if isinstance(action, dict):
                    safe_action = m.RuleAction(
                        outcome=action.get("outcome")
                        if action.get("outcome") in ("candidate", "protect", "match")
                        else None,
                        auto_delete_enabled=action.get("auto_delete_enabled")
                        if isinstance(action.get("auto_delete_enabled"), bool)
                        else None,
                        auto_delete_delay_days=integer(
                            action.get("auto_delete_delay_days")
                        ),
                        media_server_action=action.get("media_server_action")
                        if action.get("media_server_action") in ("delete", "none")
                        else None,
                        arr_action=action.get("arr_action")
                        if action.get("arr_action")
                        in (
                            "delete",
                            "unmonitor",
                            "unmonitor_only",
                            "unmonitor_delete_monitor_new_seasons",
                            "unmonitor_delete_list_exclusion",
                            "unmonitor_delete_monitor_or_exclude",
                            "change_quality_profile",
                        )
                        else None,
                        move_instead_of_delete=action.get("move_instead_of_delete")
                        if isinstance(action.get("move_instead_of_delete"), bool)
                        else None,
                        radarr_service_config_ids=get_arr_service_config_ids(
                            action, "radarr"
                        )[:100],
                        sonarr_service_config_ids=get_arr_service_config_ids(
                            action, "sonarr"
                        )[:100],
                    )
                result.rules.append(
                    m.RuleInfo(
                        id=r["id"],
                        alias=str(self.export.alias("rule", r["id"])),
                        enabled=bool(r["enabled"]),
                        target_scope=r["target_scope"]
                        if r["target_scope"] in TABLES
                        else None,
                        definition=self.export.rule(object_json(r["definition"])),
                        action=safe_action,
                    )
                )
        return result

    def finish(self) -> m.DiagnosticsReport:
        report = self.report
        report.collection.completed_at = now()
        report.collection.partial |= bool(
            report.collection.omitted or report.collection.truncated
        )
        report.summary = [
            "Stored data only; no providers contacted or rules rerun.",
            "Review titles and playback information before sharing.",
            "Freshness is flagged stale after 24 hours; observation timestamps are included.",
        ]
        if report.sections.identity:
            identity = report.sections.identity
            report.summary.insert(
                0,
                f"{identity.scope.replace('_', ' ').title()}: {identity.title or identity.id}",
            )
        if report.sections.services is not None:
            enabled = [s for s in report.sections.services if s.enabled]
            unavailable = [
                s for s in enabled if s.history_refresh.state == "unavailable"
            ]
            report.summary.append(
                f"{len(enabled)} enabled services; {len(unavailable)} selected history sources have a recorded failure."
            )
        if report.sections.playback and report.target:
            playback = report.sections.playback
            report.summary.append(
                f"Native playback: {playback.native_state.replace('_', ' ')}. Imported playback: {playback.imported_state.replace('_', ' ')}; missing history does not mean zero plays."
            )
        if report.sections.decisions:
            decisions = report.sections.decisions
            report.summary.append(
                f"Included recorded decisions: {len(decisions.candidates)} candidates, {len(decisions.protections)} protections, {len(decisions.deletion_requests)} deletion requests."
            )
        if report.sections.active_jobs is not None:
            report.summary.append(
                f"{len(report.sections.active_jobs)} queued or running jobs included."
            )
        if report.collection.partial:
            report.summary.append(
                "Some sections are unavailable, omitted or sampled. See collection details."
            )
        # Cap the exact pretty-printed snapshot used by the UI. Drop whole typed
        # sections, never cut a JSON byte stream or lose the redaction envelope.
        for name in (
            "decisions",
            "storage",
            "children",
            "playback",
            "libraries",
            "services",
            "configuration",
            "recent_runs",
            "active_jobs",
            "schedules",
            "identity",
            "database",
        ):
            if len(report.model_dump_json(indent=2).encode("utf-8")) <= MAX_BYTES:
                break
            setattr(report.sections, name, None)
            report.collection.partial = True
            report.collection.omitted.append(name + ": report size limit")
        return report

    def collect(self) -> m.DiagnosticsReport:
        try:
            # URI mode=ro prevents accidental creation/writes. Do not use the app
            # engine: its connect hook changes journal mode and other PRAGMAs.
            self.db = sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=0.1,
                isolation_level=None,
            )
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA query_only=ON").close()
            self.db.set_progress_handler(
                lambda: int(time.monotonic() >= self.deadline), 1000
            )
        except (sqlite3.Error, OSError):
            if self.db is not None:
                self.db.close()
            self.db = None
        try:
            self.section("services", self.services)
            self.section("configuration", self.configuration)
            if self.report.target:
                self.section("identity", self.identity)
                self.section("children", self.children)
                self.section("storage", self.storage)
                self.section("playback", self.playback)
                self.section("decisions", self.decisions)
            else:
                self.section("database", self.database)
                self.section("libraries", self.libraries)
                self.section("schedules", self.schedules)
                self.section("active_jobs", self.jobs)
                self.section("recent_runs", self.runs)
                self.section("playback", self.playback)
        finally:
            if self.db is not None:
                self.db.close()
        return self.finish()


def collect_report(
    path: Path, options: m.DiagnosticsOptions, target: m.Target | None = None
) -> m.DiagnosticsReport:
    return Collector(path, options, target).collect()
