"""Explicit, bounded checks against configured resources; never arbitrary input URLs/paths."""

import asyncio
import concurrent.futures
import sqlite3
import stat
import threading
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from backend.core.encryption import fer_decrypt
from backend.core.utils.filesystem import mapped_path_variants
from backend.enums import Service
from backend.models import diagnostics as m
from backend.services.diagnostics import TABLES, Collector, now
from backend.services.support_history import error_category

check_slots = threading.BoundedSemaphore(2)


class ChecksBusy(Exception):
    pass


def inspect_stored(path: Path, options: m.DiagnosticCheckRequest):
    collector = Collector(path, options, options.target)
    with closing(
        sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=0.1,
            isolation_level=None,
        )
    ) as db:
        collector.db = db
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        if options.kind == "provider":
            rows = collector.query(
                "SELECT service_type,base_url,api_key FROM service_configs WHERE id=?",
                (options.service_config_id,),
            )
            if not rows:
                raise ValueError("configured service not found")
            return rows[0]
        target = options.target
        if target is None:
            raise ValueError("target required")
        rows = collector.query(
            f"SELECT {collector.identity_columns(target.scope)} FROM {TABLES[target.scope]} WHERE id=?",
            (target.id,),
        )
        if not rows:
            return [
                m.CheckObservation(
                    subject="Selected target",
                    status="failed",
                    detail="Target no longer exists in stored media.",
                )
            ]
        row = rows[0]
        if options.kind == "relationships":
            observations = [
                m.CheckObservation(
                    subject="Selected target",
                    status="passed",
                    detail="Stored target exists.",
                )
            ]
            for parent, column in (
                ("movie", "movie_id"),
                ("series", "series_id"),
                ("season", "season_id"),
            ):
                if row.get(column):
                    found = collector.query(
                        f"SELECT id FROM {TABLES[parent]} WHERE id=?", (row[column],)
                    )
                    observations.append(
                        m.CheckObservation(
                            subject=parent + " relationship",
                            status="passed" if found else "failed",
                            detail="Referenced parent exists."
                            if found
                            else "Referenced parent is missing; no repair attempted.",
                        )
                    )
            services = collector.services()
            for service in services:
                if service.enabled and service.service in ("plex", "jellyfin", "emby"):
                    observations.append(
                        m.CheckObservation(
                            subject=service.alias + " history binding",
                            status="passed"
                            if service.selection_state == "available"
                            else "unknown",
                            detail="Selected history source resolves from saved configuration."
                            if service.selection_state == "available"
                            else "No authoritative history binding could be established from saved configuration.",
                        )
                    )
            observations.append(
                m.CheckObservation(
                    subject="Coverage",
                    status="unknown",
                    detail="Only this target's parents and configured history bindings were checked; this is not a whole-database integrity scan.",
                )
            )
            return observations
        collector.configuration()
        collector.services()
        if target.scope == "movie":
            paths = collector.query(
                "SELECT path,service FROM movie_versions WHERE movie_id=? ORDER BY id LIMIT 5",
                (target.id,),
            )
        elif target.scope == "series":
            paths = collector.query(
                "SELECT path,service FROM series_service_refs WHERE series_id=? ORDER BY id LIMIT 5",
                (target.id,),
            )
        else:
            paths = [{"path": row.get("path"), "service": None}]
            if target.scope == "movie_version":
                paths = collector.query(
                    "SELECT path,service FROM movie_versions WHERE id=?", (target.id,)
                )
        observations = []
        ownership = collector.ownership()
        for item in paths:
            raw = item["path"]
            if not raw:
                continue
            service = (
                Service[item["service"]]
                if item["service"] in Service.__members__
                else None
            )
            owner = (
                ownership.main_config_id_for(service)
                if service
                else ownership.main_config_id
            )
            variants = mapped_path_variants(
                raw,
                collector.mappings,
                service_type=service.value
                if service
                else ownership.main_service.value
                if ownership.main_service
                else None,
                service_config_id=owner,
            )
            for variant in sorted(variants)[:3]:
                subject = str(collector.export.path(variant))
                try:
                    info = Path(variant).stat()
                    kind = (
                        "file"
                        if stat.S_ISREG(info.st_mode)
                        else "directory"
                        if stat.S_ISDIR(info.st_mode)
                        else "filesystem object"
                    )
                    observations.append(
                        m.CheckObservation(
                            subject=subject,
                            status="passed",
                            detail=f"Metadata lookup found a {kind}. File contents and write access were not tested.",
                        )
                    )
                except FileNotFoundError:
                    observations.append(
                        m.CheckObservation(
                            subject=subject,
                            status="failed",
                            detail="Path was not found from the Reclaimerr process.",
                        )
                    )
                except PermissionError:
                    observations.append(
                        m.CheckObservation(
                            subject=subject,
                            status="failed",
                            detail="Permission denied for metadata lookup.",
                        )
                    )
                except OSError:
                    observations.append(
                        m.CheckObservation(
                            subject=subject,
                            status="unknown",
                            detail="Filesystem metadata could not be read.",
                        )
                    )
        return observations or [
            m.CheckObservation(
                subject="Stored path",
                status="unknown",
                detail="No stored path is available to check.",
            )
        ]


async def run_check(
    path: Path, options: m.DiagnosticCheckRequest
) -> m.DiagnosticCheckResult:
    if options.kind == "provider" and options.service_config_id is None:
        raise ValueError("Choose a configured service")
    if options.kind != "provider" and options.target is None:
        raise ValueError("Choose a media item")
    if not check_slots.acquire(blocking=False):
        raise ChecksBusy
    future: concurrent.futures.Future = concurrent.futures.Future()

    def worker():
        try:
            future.set_result(inspect_stored(path, options))
        except Exception as exc:
            future.set_exception(exc)
        finally:
            check_slots.release()

    # A stuck network mount cannot queue unbounded workers or hold app shutdown.
    # Its slot remains occupied until the OS call returns, even after HTTP timeout.
    threading.Thread(target=worker, daemon=True).start()
    result = m.DiagnosticCheckResult(
        check_id=str(uuid4()), kind=options.kind, checked_at=now(), observations=[]
    )
    try:
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(
            lambda done: done.exception() if not done.cancelled() else None
        )
        stored = await asyncio.wait_for(asyncio.shield(wrapped), 5)
        if options.kind != "provider":
            result.observations = stored
            return result
        if not check_slots.acquire(blocking=False):
            raise ChecksBusy
        try:
            from backend.core.service_manager import service_manager

            service = Service[stored["service_type"]]
            key = fer_decrypt(stored["api_key"])
            checked = await asyncio.wait_for(
                service_manager.test_service(service, stored["base_url"], key), 5
            )
            if checked is None:
                result.observations = [
                    m.CheckObservation(
                        subject="Configured provider",
                        status="unknown",
                        detail="This provider has no supported connection check.",
                    )
                ]
            else:
                success, error = checked
                category = error_category(error) or "provider_check_failed"
                result.observations = [
                    m.CheckObservation(
                        subject="Configured provider",
                        status="passed" if success else "failed",
                        detail="The configured provider's read-only connection test succeeded."
                        if success
                        else category.replace("_", " ")
                        + "; raw provider response excluded.",
                    )
                ]
        finally:
            check_slots.release()
    except TimeoutError:
        result.observations = [
            m.CheckObservation(
                subject="Requested check",
                status="unknown",
                detail="Check exceeded its five-second phase budget. No successful result is assumed.",
            )
        ]
    except ChecksBusy:
        raise
    except Exception as exc:
        result.observations = [
            m.CheckObservation(
                subject="Requested check",
                status="unknown",
                detail=(error_category(exc) or "check_unavailable").replace("_", " ")
                + "; sensitive error text omitted.",
            )
        ]
    return result
