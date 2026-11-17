from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session
from test_support_diagnostics import database

from backend.database.models import BackgroundJob
from backend.database.models import SupportOperationEvent
from backend.database.models import (
    CandidateResyncIdentity,
    DeleteRequest,
    DeleteRequestResyncIdentity,
    ProtectedMedia,
    ReclaimCandidate,
    ScheduledDeletion,
    Season,
    GeneralSettings,
    ReclaimRule,
    NativePlaybackAggregate,
    PlaybackHistoryAggregate,
    PlaybackHistoryEvent,
    UpgradeLeftover,
)
from backend.enums import MediaType, ProtectionRequestStatus
from backend.enums import BackgroundJobStatus, BackgroundJobType, Task
from backend.models.diagnostics import BundleOptions, Target, TimelineEntry
from backend.services.support_bundle import (
    bundle_cache,
    prepare_bundle,
    prioritize_timeline,
)


def test_existing_job_results_survive_all_bundle_formats(database):
    now = datetime.now(UTC)
    with Session(database[2]) as db:
        job = BackgroundJob(
            job_type=BackgroundJobType.TASK_RUN,
            status=BackgroundJobStatus.COMPLETED,
            scheduled_at=now,
            attempts=2,
            payload={
                "task": Task.DELETE_CLEANUP_CANDIDATES.value,
                "trigger": "scheduled",
                "result": {
                    "skipped": 1,
                    "revalidated_out": 1,
                    "playback_unavailable": 1,
                    "deleted": 0,
                    "failed": True,
                    "deferred": "COUNTER_SECRET",
                    "errors": ["ERROR_SECRET"],
                    "api_key": "RESULT_SECRET",
                },
                "progress": {
                    "completed_items": 3,
                    "percent": 100,
                    "current_item_label": "LABEL_SECRET",
                },
                "api_key": "PAYLOAD_SECRET",
            },
        )
        job.started_at = now
        job.completed_at = now
        db.add(job)
        db.commit()
        job_id = job.id
    before = hashlib.sha256(database[0].read_bytes()).hexdigest()
    preview = prepare_bundle(
        database[0],
        BundleOptions(
            support_code=f"job-{job_id}",
            sections=["tasks", "timeline"],
            include_sqlite=True,
        ),
        1,
    )
    exported = next(e.job for e in preview.timeline if e.job)
    assert exported.task == Task.DELETE_CLEANUP_CANDIDATES.value
    assert exported.trigger == "scheduled" and exported.attempts == 2
    assert exported.result == {
        "skipped": 1,
        "revalidated_out": 1,
        "playback_unavailable": 1,
        "deleted": 0,
    }
    assert exported.progress == {"completed_items": 3, "percent": 100}
    assert exported.scope == "app_wide"
    assert preview.report.sections.recent_jobs[0].result == exported.result
    data, _ = bundle_cache.get(1, preview.download_token)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert b"SECRET" not in b"".join(archive.read(n) for n in archive.namelist())
        assert any(
            e["job"] and e["job"]["result"] == exported.result
            for e in json.loads(archive.read("evidence.json"))["timeline"]
        )
    assert hashlib.sha256(database[0].read_bytes()).hexdigest() == before


def test_preflight_failure_survives_refresh_noise_as_app_wide_context(database):
    now = datetime.now(UTC)
    with Session(database[2]) as db:
        failed = BackgroundJob(
            job_type=BackgroundJobType.TASK_RUN,
            status=BackgroundJobStatus.FAILED,
            scheduled_at=now,
            payload={
                "task": Task.DELETE_CLEANUP_CANDIDATES.value,
                "trigger": "scheduled",
            },
        )
        failed.completed_at = now
        failed.error_message = "connection failed password=FAILURE_SECRET"
        db.add(failed)
        db.flush()
        job_id = failed.id
        for _ in range(130):
            refresh = BackgroundJob(
                job_type=BackgroundJobType.TASK_RUN,
                status=BackgroundJobStatus.COMPLETED,
                scheduled_at=now,
                payload={"task": Task.REFRESH_PLAYBACK_HISTORY.value},
            )
            refresh.completed_at = now
            db.add(refresh)
        db.commit()
    preview = prepare_bundle(
        database[0],
        BundleOptions(
            target=Target(scope="movie_version", id=database[1]["movie_version"]),
            sections=["tasks", "timeline"],
        ),
        1,
    )
    job = next(j for j in preview.report.sections.recent_jobs if j.id == job_id)
    assert job.error_category == "connection_failed"
    assert job.scope == "app_wide"
    assert not any(e.job_id == job_id for e in preview.timeline)
    assert any(t.section == "recent_jobs" for t in preview.report.collection.truncated)
    assert "FAILURE_SECRET" not in preview.model_dump_json()


@pytest.mark.parametrize(
    "payload",
    [
        [],
        "malformed",
        {"task": {}, "result": []},
        {
            "task": "PRIVATE_TASK",
            "trigger": "SECRET_TRIGGER",
            "result": {"skipped": -1, "deleted": 2**64},
        },
        {"result": {"skipped": 1}, "secret": "x" * 70000},
    ],
)
def test_untrusted_job_payloads_are_bounded(database, payload):
    with Session(database[2]) as db:
        job = BackgroundJob(
            job_type=BackgroundJobType.SERVICE_TOGGLE,
            scheduled_at=datetime.now(UTC),
            payload=payload,
        )
        db.add(job)
        db.commit()
    preview = prepare_bundle(database[0], BundleOptions(sections=["tasks"]), 1)
    assert preview.report.sections.recent_jobs
    job = preview.report.sections.recent_jobs[0]
    assert job.task is None and job.trigger is None
    assert job.result in (None, {})
    assert "PRIVATE_TASK" not in preview.model_dump_json()
    if isinstance(payload, dict) and "secret" in payload:
        assert any("payload" in value for value in preview.report.collection.omitted)


@pytest.mark.parametrize("exact", [False, True])
def test_final_recovery_and_decision_survive_error_flood(database, exact):
    code = str(uuid4())
    now = datetime.now(UTC) - timedelta(minutes=2)
    with Session(database[2]) as db:
        for index in range(101):
            db.add(
                SupportOperationEvent(
                    correlation_id=code,
                    event_type="operation_step",
                    occurred_at=now + timedelta(seconds=index),
                    target_scope="movie_version",
                    target_id=database[1]["movie_version"],
                    candidate_id=1,
                    movie_id=database[1]["movie"],
                    error_category="permission_denied",
                    detail={
                        "action": "delete",
                        "stage": "provider_delete",
                        "outcome": "failed",
                        "subject": "provider_request",
                    },
                )
            )
        for event_type, detail in (
            (
                "item_outcome",
                {
                    "action": "delete",
                    "stage": "completed",
                    "outcome": "succeeded",
                    "subject": "candidate",
                },
            ),
            (
                "auto_delete_decision",
                {"stage": "dispatch", "outcome": "authorized", "reason": "authorized"},
            ),
        ):
            db.add(
                SupportOperationEvent(
                    correlation_id=code,
                    event_type=event_type,
                    occurred_at=now,
                    target_scope="movie_version",
                    target_id=database[1]["movie_version"],
                    candidate_id=1,
                    movie_id=database[1]["movie"],
                    detail=detail,
                )
            )
        db.commit()
    preview = prepare_bundle(
        database[0],
        BundleOptions(
            target=Target(scope="movie_version", id=database[1]["movie_version"]),
            support_code=f"operation-{code}" if exact else None,
            sections=["timeline"],
        ),
        1,
    )
    assert any(
        e.event == "item_outcome" and e.operation.outcome == "succeeded"
        for e in preview.timeline
    )
    assert any(
        e.auto_delete and e.auto_delete.reason == "authorized" for e in preview.timeline
    )
    assert any(
        e.operation and e.operation.outcome == "failed" for e in preview.timeline
    )
    assert any(
        t.section == "operation_events" and t.total == 103
        for t in preview.report.collection.truncated
    )


def test_timeline_limit_keeps_terminal_evidence_before_newer_noise():
    now = datetime.now(UTC)
    code = str(uuid4())
    target = Target(scope="movie_version", id=1)
    final = TimelineEntry(
        occurred_at=now - timedelta(days=1),
        event="item_outcome",
        correlation_id=code,
        candidate_id=1,
        target=target,
    )
    newer = final.model_copy(update={"occurred_at": now - timedelta(hours=1)})
    noise = [
        TimelineEntry(
            occurred_at=now,
            event="operation_step",
            correlation_id=code,
            error_category="permission_denied",
        )
        for _ in range(250)
    ]
    selected = prioritize_timeline([final, newer, *noise])[:200]
    assert selected[0] is newer
    assert all(e is not final for e in selected)
    assert len(selected) == 200


@pytest.mark.parametrize("scope", ["movie_version", "season"])
def test_sibling_blockers_keep_their_actual_scope(database, scope):
    with Session(database[2]) as db:
        if scope == "season":
            sibling = Season(series_id=database[1]["series"], season_number=2)
            db.add(sibling)
            db.flush()
            ids = {"series_id": database[1]["series"], "season_id": sibling.id}
            media_type = MediaType.SERIES
        else:
            ids = {
                "movie_id": database[1]["movie"],
                "movie_version_id": database[1]["movie_version"] + 1,
            }
            media_type = MediaType.MOVIE
        protection = ProtectedMedia(
            media_type=media_type, protected_by_user_id=1, **ids
        )
        request = DeleteRequest(media_type=media_type, requested_by_user_id=1, **ids)
        db.add_all([protection, request])
        db.commit()
    preview = prepare_bundle(
        database[0],
        BundleOptions(
            target=Target(scope=scope, id=database[1][scope]),
            sections=["decisions"],
        ),
        1,
    )
    decisions = preview.report.sections.decisions
    assert decisions is not None
    assert decisions.protections[0].relationship == "sibling"
    assert decisions.protections[0].active
    assert decisions.deletion_requests[0].relationship == "sibling"
    assert (
        decisions.protections[0].target.id
        == ids["season_id" if scope == "season" else "movie_version_id"]
    )


def test_stored_holds_schedules_and_request_deferrals_are_exported(database):
    now = datetime.now(UTC)
    with Session(database[2]) as db:
        candidate = db.get(ReclaimCandidate, 1)
        candidate.matched_criteria = {"_unavailable_rule_ids": [1, True, "HOLD_SECRET"]}
        db.add(
            CandidateResyncIdentity(
                candidate_id=1,
                movie_id=database[1]["movie"],
                path="/private/RESYNC_SECRET.mkv",
            )
        )
        request = DeleteRequest(
            media_type=MediaType.MOVIE,
            requested_by_user_id=1,
            movie_id=database[1]["movie"],
            movie_version_id=database[1]["movie_version"],
            status=ProtectionRequestStatus.APPROVED,
            playback_deferral={
                "code": "currently_playing",
                "checked_at": now.isoformat(),
                "server_config_ids": [1],
                "message": "DEFERRAL_SECRET",
            },
        )
        db.add(request)
        db.flush()
        db.add(
            DeleteRequestResyncIdentity(
                delete_request_id=request.id,
                movie_id=database[1]["movie"],
                path="/private/REQUEST_SECRET.mkv",
            )
        )
        db.add(
            ScheduledDeletion(
                scheduled_at=now + timedelta(days=2),
                target={
                    "media_type": "movie",
                    "media_id": database[1]["movie"],
                    "scope": "movie",
                    "title": "TARGET_SECRET",
                },
                created_by_username="ACTOR_SECRET",
                state="blocked",
                last_error="permission denied ERROR_SECRET",
            )
        )
        db.commit()
    preview = prepare_bundle(
        database[0],
        BundleOptions(
            target=Target(scope="movie_version", id=database[1]["movie_version"]),
            sections=["decisions"],
            include_sqlite=True,
        ),
        1,
    )
    decisions = preview.report.sections.decisions
    assert decisions is not None
    candidate = decisions.candidates[0]
    assert candidate.unavailable_rule_ids == [1]
    assert candidate.resync_pending and candidate.resync_path.startswith("path-")
    request = decisions.deletion_requests[0]
    assert request.playback_deferral.code == "currently_playing"
    assert request.error is None and request.resync_pending
    schedule = decisions.scheduled_deletions[0]
    assert schedule.state == "blocked" and schedule.holds_selected_scope
    assert schedule.scheduled_at == now + timedelta(days=2)
    assert schedule.error_category == "permission_denied"
    assert schedule.actor.startswith("user-")
    assert any(
        i.section == "scheduled_deletions" and i.records == 1
        for i in preview.manifest.inventory
    )
    assert "SECRET" not in preview.model_dump_json()


def test_manual_schedule_path_hold_can_cross_media_ids(database):
    with Session(database[2]) as db:
        db.add(
            ScheduledDeletion(
                scheduled_at=datetime.now(UTC) + timedelta(days=1),
                target={
                    "media_type": "movie",
                    "media_id": 9999,
                    "scope": "movie",
                    "inventory": {
                        "files": {"/remote/movie-0.mkv": 1},
                        "directories": [],
                    },
                },
                created_by_username="PRIVATE_ACTOR",
                state="failed",
            )
        )
        db.commit()
    preview = prepare_bundle(
        database[0],
        BundleOptions(
            target=Target(scope="movie_version", id=database[1]["movie_version"]),
            sections=["decisions"],
        ),
        1,
    )
    schedule = preview.report.sections.decisions.scheduled_deletions[0]
    assert schedule.stored_path_overlap and not schedule.holds_selected_scope
    assert schedule.media_target.id == 9999
    assert "PRIVATE_ACTOR" not in preview.model_dump_json()


def test_cleanup_settings_and_action_targets_keep_identity_relationships(database):
    with Session(database[2]) as db:
        settings = db.query(GeneralSettings).one()
        settings.default_arr_delete_behavior = "unmonitor_only"
        settings.favorites_ignore_enabled = True
        settings.favorites_protect_all_users = False
        settings.favorites_usernames = [" PrivateUser "]
        settings.requester_watch_ignore_request_date = True
        settings.requester_watch_user_mappings = [
            {
                "seerr_service_config_id": 7,
                "seerr_user_id": 44,
                "seerr_username": "MAPPING_SECRET",
                "media_user_key": "PRIVATEUSER",
                "service_type": "plex",
                "token": "TOKEN_SECRET",
            }
        ]
        rule = db.get(ReclaimRule, 1)
        rule.action = {
            "arr_action": "change_quality_profile",
            "quality_profile_id": 42,
            "trigger_search": False,
            "radarr_service_config_ids": [5, 6],
            "arr_tag": "TAG_SECRET",
            "tag_enabled": True,
        }
        rule.definition = {
            "root": {
                "type": "group",
                "op": "and",
                "children": [
                    {
                        "field": "playback.usernames",
                        "operator": "contains_any",
                        "value": ["privateuser"],
                    },
                    {
                        "field": "seerr.requested_by_user_ids",
                        "operator": "contains_any",
                        "value": ["7:44"],
                    },
                ],
            }
        }
        db.commit()
    preview = prepare_bundle(
        database[0],
        BundleOptions(
            target=Target(scope="movie_version", id=database[1]["movie_version"]),
            sections=["configuration", "decisions", "playback"],
            include_sqlite=True,
        ),
        1,
    )
    config = preview.report.sections.configuration
    assert config.default_arr_delete_behavior == "unmonitor_only"
    assert config.add_arr_import_exclusions_on_delete
    assert config.favorites_ignore_enabled and not config.favorites_protect_all_users
    assert config.requester_watch_ignore_request_date
    mapping = config.requester_watch_user_mappings[0]
    assert mapping.seerr_service_config_id == 7 and mapping.service_type == "plex"
    assert mapping.media_user_key == config.favorites_usernames[0]
    rule = preview.report.sections.decisions.rules[0]
    assert rule.action.quality_profile_id == 42 and rule.action.trigger_search is False
    assert rule.action.radarr_service_config_ids == [5, 6]
    assert rule.action.arr_tag.startswith("tag-")
    assert (
        rule.definition["root"]["conditions"][0]["value"] == config.favorites_usernames
    )
    assert rule.definition["root"]["conditions"][1]["value"] == [mapping.seerr_user_id]
    assert "SECRET" not in preview.model_dump_json()
    assert "privateuser" not in preview.model_dump_json().lower()


@pytest.mark.parametrize("activity,complete", [(True, False), (False, True)])
def test_playback_field_provenance_distinguishes_unknown_from_no_activity(
    database, activity, complete
):
    with Session(database[2]) as db:
        native = db.query(NativePlaybackAggregate).one()
        native.has_activity = activity
        native.usernames_complete = complete
        native.last_activity_at = None
        native.refreshed_at = datetime.now(UTC)
        db.add(
            PlaybackHistoryAggregate(
                target_scope="movie_version",
                target_id=database[1]["movie_version"],
                media_type=MediaType.MOVIE,
                play_count=1,
                usernames_complete=False,
            )
        )
        for event in db.query(PlaybackHistoryEvent).all():
            event.identity_evidence = {
                "conflict": True,
                "unverified": False,
                "raw": "IDENTITY_SECRET",
            }
        db.commit()
    preview = prepare_bundle(
        database[0],
        BundleOptions(
            target=Target(scope="movie_version", id=database[1]["movie_version"]),
            sections=["playback"],
        ),
        1,
    )
    playback = preview.report.sections.playback
    native = next(a for a in playback.aggregates if a.kind == "native")
    assert native.has_activity is activity and native.usernames_complete is complete
    if activity:
        assert native.unavailable_fields == {
            "playback.usernames": "incomplete_usernames",
            "playback.last_activity_at": "activity_date_unknown",
            "playback.days_since_last_activity": "activity_date_unknown",
        }
    else:
        assert native.unavailable_fields == {}
    imported = next(a for a in playback.aggregates if a.kind == "imported")
    assert imported.has_activity is None
    assert imported.unavailable_fields == {"playback.usernames": "incomplete_usernames"}
    assert all(
        e.identity_conflict is True and e.identity_unverified is False
        for e in playback.events
    )
    assert "IDENTITY_SECRET" not in preview.model_dump_json()


@pytest.mark.parametrize(
    "scope", ["movie_version", "movie", "episode", "season", "series"]
)
def test_duplicate_job_lookup_and_target_timeline_without_operation_history(
    database, scope
):
    with Session(database[2]) as db:
        job = BackgroundJob(
            job_type=BackgroundJobType.DUPLICATE_DELETE,
            scheduled_at=datetime.now(UTC),
            status=BackgroundJobStatus.FAILED,
            payload={
                "items": [
                    {
                        "media_type": "movie",
                        "item_id": database[1]["movie"],
                        "version_ids": [database[1]["movie_version"]],
                        "display_label": "DUPLICATE_SECRET",
                    },
                    {
                        "media_type": "series",
                        "item_id": database[1]["episode"],
                        "version_ids": [1],
                    },
                ]
            },
        )
        job.completed_at = datetime.now(UTC)
        db.add(job)
        db.commit()
        job_id = job.id
    from backend.services.support_lookup import lookup_support

    match = lookup_support(database[0], f"job-{job_id}").matches[0]
    assert set((t.scope, t.id) for t in match.targets) == {
        ("movie_version", database[1]["movie_version"]),
        ("episode", database[1]["episode"]),
    }
    preview = prepare_bundle(
        database[0],
        BundleOptions(
            target=Target(scope=scope, id=database[1][scope]), sections=["timeline"]
        ),
        1,
    )
    assert any(e.job_id == job_id and e.job for e in preview.timeline)
    assert "DUPLICATE_SECRET" not in preview.model_dump_json()


def test_leftover_job_resolves_existing_movie_target(database):
    with Session(database[2]) as db:
        leftover = UpgradeLeftover(
            service_config_id=1,
            arr_movie_id=20,
            movie_id=database[1]["movie"],
            title="LEFTOVER_SECRET",
            dropped_path="/private/file.mkv",
            local_path="/private/file.mkv",
            size=1,
            link_count=1,
        )
        db.add(leftover)
        db.flush()
        job = BackgroundJob(
            job_type=BackgroundJobType.DUPLICATE_DELETE,
            scheduled_at=datetime.now(UTC),
            payload={
                "leftovers": [{"id": leftover.id, "display_label": "LABEL_SECRET"}]
            },
        )
        db.add(job)
        db.commit()
        job_id = job.id
    from backend.services.support_lookup import lookup_support

    match = lookup_support(database[0], f"job-{job_id}").matches[0]
    assert match.targets == [Target(scope="movie", id=database[1]["movie"])]
    preview = prepare_bundle(
        database[0], BundleOptions(target=match.targets[0], sections=["timeline"]), 1
    )
    assert any(e.job_id == job_id for e in preview.timeline)
    assert "SECRET" not in preview.model_dump_json()
