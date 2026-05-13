from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.api.candidate_views import normalize_reason_parts, reason_tokens
from backend.core.auth import get_current_user, has_permission
from backend.core.utils.datetime_utils import to_utc_isoformat
from backend.database import get_db
from backend.database.models import (
    DeleteRequest,
    Episode,
    Movie,
    MovieArrRef,
    MovieVersion,
    ProtectedMedia,
    ProtectionRequest,
    ReclaimCandidate,
    ReclaimHistory,
    Season,
    Series,
    SeriesArrRef,
    SeriesServiceRef,
    ServiceMediaLibrary,
    User,
)
from backend.enums import MediaType, Permission, ProtectionRequestStatus, UserRole
from backend.models.media import (
    ArrRefResponse,
    CandidateEntry,
    CandidateLibraryRef,
    DeleteCandidatesRequest,
    DeleteCandidatesResponse,
    MediaStatusInfo,
    MoveCandidatesRequest,
    MoveCandidatesResponse,
    MovieVersionResponse,
    MovieWithStatus,
    PaginatedCandidatesResponse,
    PaginatedMediaResponse,
    PaginatedReclaimHistoryResponse,
    ReclaimHistoryEntry,
    SeasonWithStatus,
    SeriesServiceRefResponse,
    SeriesWithStatus,
)
from backend.tasks.cleanup import delete_specific_candidates, move_specific_candidates

router = APIRouter(prefix="/api/media", tags=["media"])


def extract_genre_names(genres: list[dict] | None) -> list[str] | None:
    """
    Extract genre names from TMDB genre objects.

    Comes in format [{'id': 16, 'name': 'Animation'}, ...] but we only want the names.
    """
    if not genres:
        return None
    return [g["name"] for g in genres]


@router.get("/movies", response_model=PaginatedMediaResponse)
async def get_movies(
    _user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    sort_by: str = Query("title", pattern="^(title|added_at|size|vote_average|year)$"),
    sort_order: str = Query("asc", pattern="^(asc|desc)$"),
    search: str | None = Query(None, max_length=200),
    candidates_only: bool = Query(False),
):
    """
    Get all movies with status information.

    Includes whether each movie is:
    - A deletion candidate
    - Protected
    - Has pending exception request
    """
    # build base query
    query = (
        select(Movie)
        .where(Movie.removed_at.is_(None))
        .options(selectinload(Movie.versions))
    )
    count_query = (
        select(func.count()).select_from(Movie).where(Movie.removed_at.is_(None))
    )

    # apply search filter
    if search:
        search_term = f"%{search}%"
        query = query.where(Movie.title.ilike(search_term))
        count_query = count_query.where(Movie.title.ilike(search_term))

    # apply candidates filter
    # Note: use distinct() to guard against row multiplication if a movie ever ends up
    # with more than one ReclaimCandidate row (this should really not ever happen)
    if candidates_only:
        query = query.join(
            ReclaimCandidate, ReclaimCandidate.movie_id == Movie.id
        ).distinct()
        count_query = count_query.join(
            ReclaimCandidate, ReclaimCandidate.movie_id == Movie.id
        ).distinct()

    # apply sorting
    order_column = getattr(Movie, sort_by)
    if sort_order == "desc":
        query = query.order_by(order_column.desc())
    else:
        query = query.order_by(order_column.asc())

    # get total count
    total = (await db.execute(count_query)).scalar_one()

    # apply pagination
    offset = (page - 1) * per_page
    query = query.offset(offset).limit(per_page)

    # execute query
    result = await db.execute(query)
    movies = result.scalars().all()

    # fetch status information for all movies
    movie_ids = [m.id for m in movies]

    # get candidates
    candidates_result = await db.execute(
        select(ReclaimCandidate).where(ReclaimCandidate.movie_id.in_(movie_ids))
    )
    candidates = {c.movie_id: c for c in candidates_result.scalars().all()}

    # get protected entries
    now = datetime.now(UTC)
    protected_result = await db.execute(
        select(ProtectedMedia).where(
            ProtectedMedia.movie_id.in_(movie_ids),
            or_(
                ProtectedMedia.permanent.is_(True),
                ProtectedMedia.expires_at.is_(None),
                ProtectedMedia.expires_at > now,
            ),
        )
    )
    protected = {b.movie_id: b for b in protected_result.scalars().all()}

    # get exception requests
    requests_result = await db.execute(
        select(ProtectionRequest).where(
            ProtectionRequest.movie_id.in_(movie_ids),
            ProtectionRequest.status == ProtectionRequestStatus.PENDING,
        )
    )
    requests = {r.movie_id: r for r in requests_result.scalars().all()}

    delete_requests_result = await db.execute(
        select(DeleteRequest).where(
            DeleteRequest.movie_id.in_(movie_ids),
            DeleteRequest.status == ProtectionRequestStatus.PENDING,
        )
    )
    delete_requests = {r.movie_id: r for r in delete_requests_result.scalars().all()}

    # build response with status
    items = []
    for movie in movies:
        candidate = candidates.get(movie.id)
        protection_entry = protected.get(movie.id)
        request = requests.get(movie.id)
        delete_request = delete_requests.get(movie.id)

        status = MediaStatusInfo(
            is_candidate=candidate is not None,
            candidate_id=candidate.id if candidate else None,
            candidate_reason=candidate.reason if candidate else None,
            candidate_space_bytes=candidate.estimated_space_bytes
            if candidate
            else None,
            is_protected=protection_entry is not None,
            protected_reason=protection_entry.reason if protection_entry else None,
            protected_permanent=protection_entry.permanent
            if protection_entry
            else True,
            has_pending_request=request is not None,
            request_id=request.id if request else None,
            request_status=request.status if request else None,
            request_reason=request.reason if request else None,
            has_pending_delete_request=delete_request is not None,
            delete_request_id=delete_request.id if delete_request else None,
            delete_request_status=delete_request.status if delete_request else None,
            delete_request_reason=delete_request.reason if delete_request else None,
        )

        movie_arr_refs_result = await db.execute(
            select(MovieArrRef).where(MovieArrRef.movie_id == movie.id)
        )
        movie_arr_refs = movie_arr_refs_result.scalars().all()

        movie_dict = {
            "id": movie.id,
            "title": movie.title,
            "year": movie.year,
            "tmdb_id": movie.tmdb_id,
            "size": movie.size,
            "versions": [
                MovieVersionResponse(
                    id=v.id,
                    service=v.service.value,
                    service_item_id=v.service_item_id,
                    service_media_id=v.service_media_id,
                    library_id=v.library_id,
                    library_name=v.library_name,
                    path=v.path,
                    size=v.size,
                    added_at=to_utc_isoformat(v.added_at),
                    file_name=v.file_name,
                    container=v.container,
                    duration=v.duration,
                    video_track_count=v.video_track_count,
                    video_codec=v.video_codec,
                    video_codec_family=v.video_codec_family,
                    video_hdr=v.video_hdr,
                    video_dolby_vision=v.video_dolby_vision,
                    video_dolby_vision_profile=v.video_dolby_vision_profile,
                    video_bitrate=v.video_bitrate,
                    video_bit_depth=v.video_bit_depth,
                    video_width=v.video_width,
                    video_height=v.video_height,
                    video_resolution=v.video_resolution,
                    video_color_primaries=v.video_color_primaries,
                    video_color_space=v.video_color_space,
                    video_color_transfer=v.video_color_transfer,
                    video_fps=v.video_fps,
                    audio_count=v.audio_count,
                    audio_languages=v.audio_languages,
                    audio_codec=v.audio_codec,
                    audio_codec_family=v.audio_codec_family,
                    audio_title=v.audio_title,
                    audio_language=v.audio_language,
                    audio_channels=v.audio_channels,
                    audio_channel_layout=v.audio_channel_layout,
                    audio_bitrate=v.audio_bitrate,
                    audio_sample_rate=v.audio_sample_rate,
                    subtitle_count=v.subtitle_count,
                    subtitle_has_forced=v.subtitle_has_forced,
                    subtitle_languages=v.subtitle_languages,
                    has_chapters=v.has_chapters,
                )
                for v in movie.versions
            ],
            "arr_refs": [
                ArrRefResponse(
                    service_type="radarr",
                    service_config_id=ref.service_config_id,
                    arr_id=ref.arr_movie_id,
                )
                for ref in movie_arr_refs
            ],
            "imdb_id": movie.imdb_id,
            "tmdb_title": movie.tmdb_title,
            "original_title": movie.original_title,
            "tmdb_release_date": to_utc_isoformat(movie.tmdb_release_date),
            "original_language": movie.original_language,
            "poster_url": movie.poster_url,
            "backdrop_url": movie.backdrop_url,
            "overview": movie.overview,
            "genres": extract_genre_names(movie.genres),  # type: ignore
            "popularity": movie.popularity,
            "vote_average": movie.vote_average,
            "vote_count": movie.vote_count,
            "runtime": movie.runtime,
            "tagline": movie.tagline,
            "last_viewed_at": to_utc_isoformat(movie.last_viewed_at),
            "view_count": movie.view_count,
            "status": status,
            "added_at": to_utc_isoformat(movie.added_at),
        }
        items.append(MovieWithStatus(**movie_dict))

    total_pages = (total + per_page - 1) // per_page

    return PaginatedMediaResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )


@router.get("/series", response_model=PaginatedMediaResponse)
async def get_series(
    _user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    sort_by: str = Query("title", pattern="^(title|added_at|size|vote_average|year)$"),
    sort_order: str = Query("asc", pattern="^(asc|desc)$"),
    search: str | None = Query(None, max_length=200),
    candidates_only: bool = Query(False),
):
    """
    Get all series with status information.

    Includes whether each series is:
    - A deletion candidate
    - Protected
    - Has pending exception request
    """
    # build base query
    query = (
        select(Series)
        .where(Series.removed_at.is_(None))
        .options(selectinload(Series.service_refs))
    )
    count_query = (
        select(func.count()).select_from(Series).where(Series.removed_at.is_(None))
    )

    # apply search filter
    if search:
        search_term = f"%{search}%"
        query = query.where(Series.title.ilike(search_term))
        count_query = count_query.where(Series.title.ilike(search_term))

    # apply candidates filter
    # Note: we're using distinct() to avoid row multiplication when a series has multiple
    # season level candidates (each shares the same series_id)
    if candidates_only:
        query = query.join(
            ReclaimCandidate, ReclaimCandidate.series_id == Series.id
        ).distinct()
        count_query = count_query.join(
            ReclaimCandidate, ReclaimCandidate.series_id == Series.id
        ).distinct()

    # apply sorting
    order_column = getattr(Series, sort_by)
    if sort_order == "desc":
        query = query.order_by(order_column.desc())
    else:
        query = query.order_by(order_column.asc())

    # get total count
    total = (await db.execute(count_query)).scalar_one()

    # apply pagination
    offset = (page - 1) * per_page
    query = query.offset(offset).limit(per_page)

    # execute query
    result = await db.execute(query)
    series_list = result.scalars().all()

    # fetch status information for all series
    series_ids = [s.id for s in series_list]

    # get series level candidates (no season)
    candidates_result = await db.execute(
        select(ReclaimCandidate).where(
            ReclaimCandidate.series_id.in_(series_ids),
            ReclaimCandidate.season_id.is_(None),
        )
    )
    candidates = {c.series_id: c for c in candidates_result.scalars().all()}

    # collect series_ids that have at least one season level candidate
    season_cands_result = await db.execute(
        select(ReclaimCandidate.series_id).where(
            ReclaimCandidate.series_id.in_(series_ids),
            ReclaimCandidate.season_id.isnot(None),
        )
    )
    series_with_season_cands: set[int] = {
        row[0] for row in season_cands_result.all() if row[0] is not None
    }

    # get protected entries
    now = datetime.now(UTC)
    protected_result = await db.execute(
        select(ProtectedMedia).where(
            ProtectedMedia.series_id.in_(series_ids),
            or_(
                ProtectedMedia.permanent.is_(True),
                ProtectedMedia.expires_at.is_(None),
                ProtectedMedia.expires_at > now,
            ),
        )
    )
    protected = {b.series_id: b for b in protected_result.scalars().all()}

    # get exception requests
    requests_result = await db.execute(
        select(ProtectionRequest).where(
            ProtectionRequest.series_id.in_(series_ids),
            ProtectionRequest.status == ProtectionRequestStatus.PENDING,
        )
    )
    requests = {r.series_id: r for r in requests_result.scalars().all()}

    delete_requests_result = await db.execute(
        select(DeleteRequest).where(
            DeleteRequest.series_id.in_(series_ids),
            DeleteRequest.status == ProtectionRequestStatus.PENDING,
        )
    )
    delete_requests = {r.series_id: r for r in delete_requests_result.scalars().all()}

    # build response with status
    items = []
    for series in series_list:
        candidate = candidates.get(series.id)
        protection_entry = protected.get(series.id)
        request = requests.get(series.id)
        delete_request = delete_requests.get(series.id)

        status = MediaStatusInfo(
            is_candidate=candidate is not None,
            candidate_id=candidate.id if candidate else None,
            candidate_reason=candidate.reason if candidate else None,
            candidate_space_bytes=candidate.estimated_space_bytes
            if candidate
            else None,
            is_protected=protection_entry is not None,
            protected_reason=protection_entry.reason if protection_entry else None,
            protected_permanent=protection_entry.permanent
            if protection_entry
            else True,
            has_pending_request=request is not None,
            request_id=request.id if request else None,
            request_status=request.status if request else None,
            request_reason=request.reason if request else None,
            has_pending_delete_request=delete_request is not None,
            delete_request_id=delete_request.id if delete_request else None,
            delete_request_status=delete_request.status if delete_request else None,
            delete_request_reason=delete_request.reason if delete_request else None,
        )

        series_arr_refs_result = await db.execute(
            select(SeriesArrRef).where(SeriesArrRef.series_id == series.id)
        )
        series_arr_refs = series_arr_refs_result.scalars().all()

        series_dict = {
            "id": series.id,
            "title": series.title,
            "year": series.year,
            "tmdb_id": series.tmdb_id,
            "size": series.size,
            "service_refs": [
                SeriesServiceRefResponse(
                    service=ref.service.value,
                    service_id=ref.service_id,
                    library_id=ref.library_id,
                    library_name=ref.library_name,
                    path=ref.path,
                )
                for ref in series.service_refs
            ],
            "arr_refs": [
                ArrRefResponse(
                    service_type="sonarr",
                    service_config_id=ref.service_config_id,
                    arr_id=ref.arr_series_id,
                )
                for ref in series_arr_refs
            ],
            "imdb_id": series.imdb_id,
            "tvdb_id": series.tvdb_id,
            "tmdb_title": series.tmdb_title,
            "original_title": series.original_title,
            "tmdb_first_air_date": to_utc_isoformat(series.tmdb_first_air_date),
            "tmdb_last_air_date": to_utc_isoformat(series.tmdb_last_air_date),
            "original_language": series.original_language,
            "poster_url": series.poster_url,
            "backdrop_url": series.backdrop_url,
            "overview": series.overview,
            "genres": extract_genre_names(series.genres),  # type: ignore
            "popularity": series.popularity,
            "vote_average": series.vote_average,
            "vote_count": series.vote_count,
            "season_count": series.season_count,
            "tagline": series.tagline,
            "last_viewed_at": to_utc_isoformat(series.last_viewed_at),
            "view_count": series.view_count,
            "has_hdr": series.has_hdr,
            "has_dolby_vision": series.has_dolby_vision,
            "max_video_width": series.max_video_width,
            "max_video_height": series.max_video_height,
            "video_codec_families": series.video_codec_families,
            "audio_codec_families": series.audio_codec_families,
            "max_audio_channels": series.max_audio_channels,
            "subtitle_languages": series.subtitle_languages,
            "status": status,
            "has_season_candidates": series.id in series_with_season_cands
            and candidate is None,
            "added_at": to_utc_isoformat(series.added_at),
        }
        items.append(SeriesWithStatus(**series_dict))

    total_pages = (total + per_page - 1) // per_page

    return PaginatedMediaResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )


@router.get("/series/{series_id}/seasons", response_model=list[SeasonWithStatus])
async def get_series_seasons(
    series_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Get per-season status for a series."""
    series_result = await db.execute(
        select(Series).where(Series.id == series_id, Series.removed_at.is_(None))
    )
    if series_result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Series not found"
        )

    seasons_result = await db.execute(
        select(Season)
        .where(Season.series_id == series_id)
        .order_by(Season.season_number)
    )
    seasons = seasons_result.scalars().all()

    season_ids = [s.id for s in seasons]
    if not season_ids:
        return []

    # season level reclaim candidates
    cand_result = await db.execute(
        select(ReclaimCandidate).where(ReclaimCandidate.season_id.in_(season_ids))
    )
    season_candidates = {c.season_id: c for c in cand_result.scalars().all()}

    # season level protection entries
    now = datetime.now(UTC)
    prot_result = await db.execute(
        select(ProtectedMedia).where(
            ProtectedMedia.season_id.in_(season_ids),
            or_(
                ProtectedMedia.permanent.is_(True),
                ProtectedMedia.expires_at.is_(None),
                ProtectedMedia.expires_at > now,
            ),
        )
    )
    season_protected = {p.season_id: p for p in prot_result.scalars().all()}
    delete_req_result = await db.execute(
        select(DeleteRequest).where(
            DeleteRequest.season_id.in_(season_ids),
            DeleteRequest.status == ProtectionRequestStatus.PENDING,
        )
    )
    season_delete_requests = {r.season_id: r for r in delete_req_result.scalars().all()}

    items: list[SeasonWithStatus] = []
    for season in seasons:
        cand = season_candidates.get(season.id)
        prot = season_protected.get(season.id)
        delete_req = season_delete_requests.get(season.id)
        season_status = MediaStatusInfo(
            is_candidate=cand is not None,
            candidate_id=cand.id if cand else None,
            candidate_reason=cand.reason if cand else None,
            candidate_space_bytes=cand.estimated_space_bytes if cand else None,
            is_protected=prot is not None,
            protected_reason=prot.reason if prot else None,
            protected_permanent=prot.permanent if prot else True,
            has_pending_delete_request=delete_req is not None,
            delete_request_id=delete_req.id if delete_req else None,
            delete_request_status=delete_req.status if delete_req else None,
            delete_request_reason=delete_req.reason if delete_req else None,
        )
        items.append(
            SeasonWithStatus(
                id=season.id,
                season_number=season.season_number,
                episode_count=season.episode_count,
                size=season.size,
                view_count=season.view_count or 0,
                added_at=to_utc_isoformat(season.added_at),
                last_viewed_at=to_utc_isoformat(season.last_viewed_at),
                air_date=to_utc_isoformat(season.air_date),
                has_hdr=season.has_hdr,
                has_dolby_vision=season.has_dolby_vision,
                max_video_width=season.max_video_width,
                max_video_height=season.max_video_height,
                video_codec_families=season.video_codec_families,
                audio_codec_families=season.audio_codec_families,
                audio_languages=season.audio_languages,
                max_audio_channels=season.max_audio_channels,
                subtitle_languages=season.subtitle_languages,
                status=season_status,
            )
        )

    return items


@router.get("/candidates", response_model=PaginatedCandidatesResponse)
async def get_candidates(
    _user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
    sort_by: str = Query(
        "created_at",
        pattern="^(created_at|media_title|estimated_space_bytes)$",
    ),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    search: str | None = Query(None, max_length=200),
    media_type: MediaType | None = Query(None),
):
    """Get all reclaim candidates with media info and pending request status."""
    base_query = (
        select(
            ReclaimCandidate,
            #### movies ####
            Movie.title.label("movie_title"),
            Movie.year.label("movie_year"),
            Movie.size.label("movie_size"),
            # movie tmdb data
            Movie.tmdb_id.label("movie_tmdb_id"),
            Movie.poster_url.label("movie_poster_url"),
            Movie.genres.label("movie_genres"),
            Movie.popularity.label("movie_popularity"),
            Movie.vote_average.label("movie_vote_average"),
            Movie.vote_count.label("movie_vote_count"),
            Movie.status.label("movie_status"),
            # movie version
            MovieVersion.service.label("version_service"),
            MovieVersion.library_id.label("version_library_id"),
            MovieVersion.library_name.label("version_library_name"),
            MovieVersion.video_codec_family.label("version_video_codec_family"),
            MovieVersion.audio_codec_family.label("version_audio_codec_family"),
            MovieVersion.video_width.label("version_video_width"),
            MovieVersion.video_height.label("version_video_height"),
            MovieVersion.video_resolution.label("version_video_resolution"),
            MovieVersion.video_hdr.label("version_video_hdr"),
            MovieVersion.video_dolby_vision.label("version_video_dolby_vision"),
            MovieVersion.audio_channels.label("version_audio_channels"),
            MovieVersion.audio_languages.label("version_audio_languages"),
            MovieVersion.size.label("version_size"),
            MovieVersion.path.label("version_path"),
            MovieVersion.file_name.label("version_file_name"),
            MovieVersion.subtitle_languages.label("version_subtitle_languages"),
            #### series ####
            Series.title.label("series_title"),
            Series.year.label("series_year"),
            Series.size.label("series_size"),
            Series.poster_url.label("series_poster_url"),
            Season.season_number.label("season_number"),
            Season.size.label("season_size"),
            Season.has_hdr.label("season_has_hdr"),
            Season.has_dolby_vision.label("season_has_dolby_vision"),
            Season.max_video_width.label("season_max_video_width"),
            Season.max_video_height.label("season_max_video_height"),
            Season.video_codec_families.label("season_video_codec_families"),
            Season.audio_codec_families.label("season_audio_codec_families"),
            Season.audio_languages.label("season_audio_languages"),
            Season.subtitle_languages.label("season_subtitle_languages"),
            # series tmdb data
            Series.tmdb_id.label("series_tmdb_id"),
            Series.genres.label("series_genres"),
            Series.popularity.label("series_popularity"),
            Series.vote_average.label("series_vote_average"),
            Series.vote_count.label("series_vote_count"),
            Series.status.label("series_status"),
            # episode
            Episode.episode_number.label("episode_number"),
            Episode.name.label("episode_name"),
        )
        .outerjoin(Movie, ReclaimCandidate.movie_id == Movie.id)
        .outerjoin(MovieVersion, ReclaimCandidate.movie_version_id == MovieVersion.id)
        .outerjoin(Series, ReclaimCandidate.series_id == Series.id)
        .outerjoin(Season, ReclaimCandidate.season_id == Season.id)
        .outerjoin(Episode, ReclaimCandidate.episode_id == Episode.id)
    )

    if media_type:
        base_query = base_query.where(ReclaimCandidate.media_type == media_type)

    if search:
        search_term = f"%{search}%"
        base_query = base_query.where(
            or_(
                Movie.title.ilike(search_term),
                Series.title.ilike(search_term),
                ReclaimCandidate.reason.ilike(search_term),
            )
        )

    count_query = (
        select(func.count(ReclaimCandidate.id))
        .outerjoin(Movie, ReclaimCandidate.movie_id == Movie.id)
        .outerjoin(MovieVersion, ReclaimCandidate.movie_version_id == MovieVersion.id)
        .outerjoin(Series, ReclaimCandidate.series_id == Series.id)
        .outerjoin(Season, ReclaimCandidate.season_id == Season.id)
    )

    if media_type:
        count_query = count_query.where(ReclaimCandidate.media_type == media_type)

    if search:
        search_term = f"%{search}%"
        count_query = count_query.where(
            or_(
                Movie.title.ilike(search_term),
                Series.title.ilike(search_term),
                ReclaimCandidate.reason.ilike(search_term),
            )
        )

    total = (await db.execute(count_query)).scalar_one() or 0

    media_title_expr = func.coalesce(Movie.title, Series.title)
    if sort_by == "media_title":
        order_expr = media_title_expr
    elif sort_by == "estimated_space_bytes":
        order_expr = ReclaimCandidate.estimated_space_bytes
    else:
        order_expr = ReclaimCandidate.created_at

    if sort_order == "desc":
        order_expr = order_expr.desc()
        id_tiebreak = ReclaimCandidate.id.desc()
    else:
        order_expr = order_expr.asc()
        id_tiebreak = ReclaimCandidate.id.asc()

    offset = (page - 1) * per_page
    result = await db.execute(
        base_query.order_by(order_expr, id_tiebreak).offset(offset).limit(per_page)
    )
    rows = result.all()

    # collect IDs to check for pending exception requests in one query each
    movie_ids = [
        r.ReclaimCandidate.movie_id
        for r in rows
        if r.ReclaimCandidate.media_type is MediaType.MOVIE
        and r.ReclaimCandidate.movie_id
    ]
    series_ids = [
        r.ReclaimCandidate.series_id for r in rows if r.ReclaimCandidate.series_id
    ]

    pending_movies_whole: set[int] = set()
    pending_movie_versions: set[tuple[int, int]] = set()
    pending_series: set[int] = set()

    if movie_ids:
        req_result = await db.execute(
            select(
                ProtectionRequest.movie_id, ProtectionRequest.movie_version_id
            ).where(
                ProtectionRequest.movie_id.in_(movie_ids),
                ProtectionRequest.status == ProtectionRequestStatus.PENDING,
            )
        )
        for movie_id, movie_version_id in req_result.all():
            if movie_id is None:
                continue
            if movie_version_id is None:
                pending_movies_whole.add(movie_id)
            else:
                pending_movie_versions.add((movie_id, movie_version_id))

    if series_ids:
        req_result = await db.execute(
            select(ProtectionRequest.series_id).where(
                ProtectionRequest.series_id.in_(series_ids),
                ProtectionRequest.status == ProtectionRequestStatus.PENDING,
            )
        )
        pending_series = {r[0] for r in req_result.all()}

    global_library_name_by_id: dict[str, str] = {}
    libraries_result = await db.execute(
        select(ServiceMediaLibrary.library_id, ServiceMediaLibrary.library_name)
    )
    for library_id, library_name in libraries_result.all():
        if not library_id or not library_name:
            continue
        if library_id not in global_library_name_by_id:
            global_library_name_by_id[library_id] = library_name

    series_library_refs_by_id: dict[int, list[CandidateLibraryRef]] = {}
    if series_ids:
        refs_result = await db.execute(
            select(
                SeriesServiceRef.series_id,
                SeriesServiceRef.service,
                SeriesServiceRef.library_id,
                SeriesServiceRef.library_name,
            ).where(SeriesServiceRef.series_id.in_(series_ids))
        )
        for series_id, service, library_id, library_name in refs_result.all():
            if series_id is None or not library_id or not library_name:
                continue
            refs = series_library_refs_by_id.setdefault(series_id, [])
            if any(ref.library_id == library_id for ref in refs):
                continue
            refs.append(
                CandidateLibraryRef(
                    library_id=library_id,
                    library_name=library_name,
                    service=service.value if service is not None else None,
                )
            )

    items_out: list[CandidateEntry] = []
    for row in rows:
        c = row.ReclaimCandidate
        is_movie = c.media_type is MediaType.MOVIE
        media_id = c.movie_id if is_movie else c.series_id
        media_title = row.movie_title if is_movie else row.series_title
        media_year = row.movie_year if is_movie else row.series_year
        poster_url = row.movie_poster_url if is_movie else row.series_poster_url
        tmdb_id = row.movie_tmdb_id if is_movie else row.series_tmdb_id
        genres = extract_genre_names(
            row.movie_genres if is_movie else row.series_genres  # type: ignore[arg-type]
        )
        popularity = row.movie_popularity if is_movie else row.series_popularity
        vote_average = row.movie_vote_average if is_movie else row.series_vote_average
        vote_count = row.movie_vote_count if is_movie else row.series_vote_count
        tmdb_status = row.movie_status if is_movie else row.series_status
        library_name_by_id = dict(global_library_name_by_id)
        if row.version_library_id and row.version_library_name:
            library_name_by_id[row.version_library_id] = row.version_library_name
        for ref in series_library_refs_by_id.get(c.series_id or -1, []):
            library_name_by_id[ref.library_id] = ref.library_name
        reason_parts = normalize_reason_parts(c.reason_data, library_name_by_id)

        has_pending = (
            (
                (c.movie_id in pending_movies_whole)
                or (
                    c.movie_id is not None
                    and c.movie_version_id is not None
                    and (c.movie_id, c.movie_version_id) in pending_movie_versions
                )
            )
            if is_movie
            else c.series_id in pending_series
        )

        if media_id is None or media_title is None:
            continue

        estimated_space_bytes = (
            row.version_size
            if row.version_size is not None
            else row.season_size
            if row.season_size is not None
            else row.movie_size
            if is_movie and row.movie_size is not None
            else row.series_size
            if (not is_movie and row.series_size is not None)
            else c.estimated_space_bytes
        )

        items_out.append(
            CandidateEntry(
                id=c.id,
                media_type=c.media_type.value,
                media_id=media_id,
                media_title=media_title,
                media_year=media_year,
                tmdb_id=tmdb_id,
                poster_url=poster_url,
                genres=genres,
                popularity=popularity,
                vote_average=vote_average,
                vote_count=vote_count,
                tmdb_status=tmdb_status,
                movie_version_id=c.movie_version_id,
                version_service=row.version_service
                if row.version_service is not None
                else None,
                version_library_id=row.version_library_id,
                version_library_name=row.version_library_name,
                version_video_codec_family=row.version_video_codec_family,
                version_audio_codec_family=row.version_audio_codec_family,
                version_video_width=row.version_video_width,
                version_video_height=row.version_video_height,
                version_video_resolution=row.version_video_resolution,
                version_video_hdr=row.version_video_hdr,
                version_video_dolby_vision=row.version_video_dolby_vision,
                version_audio_channels=row.version_audio_channels,
                version_audio_languages=row.version_audio_languages,
                version_size=row.version_size,
                version_path=row.version_path,
                version_file_name=row.version_file_name,
                version_subtitle_languages=row.version_subtitle_languages,
                reason_parts=reason_parts,
                reason_tokens=reason_tokens(reason_parts),
                estimated_space_bytes=estimated_space_bytes,
                has_pending_request=has_pending,
                created_at=to_utc_isoformat(c.created_at) or "",
                season_id=c.season_id,
                season_number=row.season_number,
                series_title=row.series_title if c.season_id is not None else None,
                season_has_hdr=row.season_has_hdr if c.season_id is not None else None,
                season_has_dolby_vision=row.season_has_dolby_vision
                if c.season_id is not None
                else None,
                season_max_video_width=row.season_max_video_width
                if c.season_id is not None
                else None,
                season_max_video_height=row.season_max_video_height
                if c.season_id is not None
                else None,
                season_video_codec_families=row.season_video_codec_families
                if c.season_id is not None
                else None,
                season_audio_codec_families=row.season_audio_codec_families
                if c.season_id is not None
                else None,
                season_audio_languages=row.season_audio_languages
                if c.season_id is not None
                else None,
                season_subtitle_languages=row.season_subtitle_languages
                if c.season_id is not None
                else None,
                series_library_refs=series_library_refs_by_id.get(c.series_id or -1)
                if c.season_id is not None
                else None,
                episode_id=c.episode_id,
                episode_number=row.episode_number if c.episode_id is not None else None,
                episode_name=row.episode_name if c.episode_id is not None else None,
            )
        )

    total_pages = (total + per_page - 1) // per_page if total else 0
    return PaginatedCandidatesResponse(
        items=items_out,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )


@router.post("/candidates/delete", response_model=DeleteCandidatesResponse)
async def delete_candidates(
    request: DeleteCandidatesRequest,
    user: Annotated[User, Depends(get_current_user)],
    _db: AsyncSession = Depends(get_db),
):
    """Deletes specific reclaim candidates, removing them from the media server.

    Requires admin or manage_reclaim permission. Uses same deletion priority as
    the automated task: Radarr/Sonarr first, then media server fallback.
    """
    if not (
        user.role is UserRole.ADMIN or has_permission(user, Permission.MANAGE_RECLAIM)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Manage reclaim permission required",
        )

    if not request.candidate_ids:
        return DeleteCandidatesResponse(deleted=0, failed=0)

    deleted, failed = await delete_specific_candidates(
        request.candidate_ids, approved_by=user.username
    )
    return DeleteCandidatesResponse(deleted=deleted, failed=failed)


@router.post("/candidates/move", response_model=MoveCandidatesResponse)
async def move_candidates(
    request: MoveCandidatesRequest,
    user: Annotated[User, Depends(get_current_user)],
    _db: AsyncSession = Depends(get_db),
):
    """Move specific reclaim candidates to the configured destination instead of deleting.

    Requires admin or manage_reclaim permission and move must be enabled in General Settings.
    """
    if not (
        user.role is UserRole.ADMIN or has_permission(user, Permission.MANAGE_RECLAIM)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Manage reclaim permission required",
        )

    if not request.candidate_ids:
        return MoveCandidatesResponse(moved=0, failed=0)

    moved, failed = await move_specific_candidates(
        request.candidate_ids, approved_by=user.username
    )
    return MoveCandidatesResponse(moved=moved, failed=failed)


@router.get("/reclaim-history", response_model=PaginatedReclaimHistoryResponse)
async def get_reclaim_history(
    _user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    media_type: MediaType | None = Query(None),
    search: str | None = Query(None, max_length=200),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
):
    """Get paginated reclaim history records."""
    base = select(ReclaimHistory)

    if media_type is not None:
        base = base.where(ReclaimHistory.media_type == media_type)
    if search and search.strip():
        base = base.where(ReclaimHistory.name.ilike(f"%{search.strip()}%"))

    count_result = await db.execute(select(func.count()).select_from(base.subquery()))
    total: int = count_result.scalar_one()

    rows_result = await db.execute(
        base.order_by(
            ReclaimHistory.created_at.asc()
            if sort_order == "asc"
            else ReclaimHistory.created_at.desc()
        )
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    rows = rows_result.scalars().all()

    items = [
        ReclaimHistoryEntry(
            id=row.id,
            approved_by=row.approved_by,
            media_type=row.media_type.value,
            tmdb_id=row.tmdb_id,
            name=row.name,
            size=row.size,
            action=row.action or "deleted",
            destination_path=row.destination_path,
            created_at=to_utc_isoformat(row.created_at) or "",
        )
        for row in rows
    ]

    total_pages = (total + per_page - 1) // per_page if total else 0
    return PaginatedReclaimHistoryResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )
