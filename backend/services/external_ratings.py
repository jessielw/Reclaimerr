from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import niquests
import niquests.exceptions as niq_exceptions
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from backend.enums import MediaType

DEFAULT_MDBLIST_BASE_URL = "https://api.mdblist.com"
DEFAULT_OMDB_BASE_URL = "https://www.omdbapi.com"

_RATING_FIELDS = (
    "rottentomatoes_tomato_meter",
    "rottentomatoes_tomato_vote_count",
    "rottentomatoes_popcorn_meter",
    "rottentomatoes_popcorn_vote_count",
    "metacritic_metascore",
    "metacritic_vote_count",
    "metacritic_user_score",
    "metacritic_user_vote_count",
    "trakt_rating",
    "trakt_vote_count",
    "letterboxd_score",
    "letterboxd_vote_count",
)


@dataclass(slots=True)
class ProviderRateLimitSnapshot:
    limit: int | None = None
    remaining: int | None = None
    reset_epoch: int | None = None

    def to_dict(self) -> dict[str, int | None]:
        return {
            "limit": self.limit,
            "remaining": self.remaining,
            "reset_epoch": self.reset_epoch,
        }


class ProviderRateLimitError(RuntimeError):
    """Raised when a ratings provider reports that its API limit is exhausted."""

    def __init__(
        self,
        provider: str,
        retry_after: str | None = None,
        rate_limit: ProviderRateLimitSnapshot | None = None,
    ) -> None:
        message = f"{provider} API rate limit exhausted"
        if retry_after:
            message = f"{message}; retry after {retry_after}s"
        super().__init__(message)
        self.provider = provider
        self.retry_after = retry_after
        self.rate_limit = rate_limit


class ProviderTransientError(RuntimeError):
    """Raised for provider failures that are safe to retry."""


@dataclass(slots=True)
class ExternalRatingValues:
    rottentomatoes_tomato_meter: int | None = None
    rottentomatoes_tomato_vote_count: int | None = None
    rottentomatoes_popcorn_meter: int | None = None
    rottentomatoes_popcorn_vote_count: int | None = None
    metacritic_metascore: int | None = None
    metacritic_vote_count: int | None = None
    metacritic_user_score: int | None = None
    metacritic_user_vote_count: int | None = None
    trakt_rating: int | None = None
    trakt_vote_count: int | None = None
    letterboxd_score: int | None = None
    letterboxd_vote_count: int | None = None

    def has_any(self) -> bool:
        return any(getattr(self, field) is not None for field in _RATING_FIELDS)

    def merge_missing(self, other: ExternalRatingValues) -> None:
        for field in _RATING_FIELDS:
            if getattr(self, field) is None:
                setattr(self, field, getattr(other, field))


class MDBListClient:
    """Client for the MDBList API."""

    __slots__ = ("api_key", "base_url", "last_rate_limit")

    def __init__(self, api_key: str, base_url: str = DEFAULT_MDBLIST_BASE_URL) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/") or DEFAULT_MDBLIST_BASE_URL
        self.last_rate_limit: ProviderRateLimitSnapshot | None = None

    async def get_ratings(
        self, media_type: MediaType, tmdb_id: int
    ) -> ExternalRatingValues:
        endpoint = "movie" if media_type is MediaType.MOVIE else "show"
        try:
            payload = await self._get_json(f"/tmdb/{endpoint}/{tmdb_id}")
        except niq_exceptions.HTTPError as exc:
            response = getattr(exc, "response", None)
            if (
                media_type is MediaType.SERIES
                and response is not None
                and getattr(response, "status_code", None) == 404
            ):
                payload = await self._get_json(f"/tmdb/tv/{tmdb_id}")
            else:
                raise
        return parse_mdblist_ratings(payload)

    @retry(
        retry=retry_if_exception_type(
            (
                ProviderTransientError,
                niq_exceptions.ConnectionError,
                niq_exceptions.Timeout,
            )
        ),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _get_json(self, endpoint: str) -> Any:
        async with niquests.AsyncSession() as session:
            response = await session.get(
                f"{self.base_url}{endpoint}",
                params={"apikey": self.api_key},
                timeout=30,
            )
            self.last_rate_limit = _rate_limit_from_headers(response.headers)
            status_code = response.status_code or 0
            if status_code == 429:
                raise ProviderRateLimitError(
                    "MDBList",
                    response.headers.get("Retry-After"),
                    self.last_rate_limit,
                )
            if status_code >= 500:
                raise ProviderTransientError(
                    f"MDBList transient failure: {status_code}"
                )
            response.raise_for_status()
            return response.json()

    @staticmethod
    async def test_service(url: str, api_key: str) -> bool:
        client = MDBListClient(
            api_key=api_key, base_url=url or DEFAULT_MDBLIST_BASE_URL
        )
        payload = await client._get_json("/user")
        return isinstance(payload, dict)


class OMDbClient:
    """Client for the OMDB API."""

    __slots__ = ("api_key", "base_url", "last_rate_limit")

    def __init__(self, api_key: str, base_url: str = DEFAULT_OMDB_BASE_URL) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/") or DEFAULT_OMDB_BASE_URL
        self.last_rate_limit: ProviderRateLimitSnapshot | None = None

    async def get_ratings(self, imdb_id: str) -> ExternalRatingValues:
        payload = await self._get_json(params={"apikey": self.api_key, "i": imdb_id})
        return parse_omdb_ratings(payload)

    @retry(
        retry=retry_if_exception_type(
            (
                ProviderTransientError,
                niq_exceptions.ConnectionError,
                niq_exceptions.Timeout,
            )
        ),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _get_json(self, *, params: dict[str, Any]) -> Any:
        async with niquests.AsyncSession() as session:
            response = await session.get(
                self.base_url,
                params=params,
                timeout=30,
            )
            self.last_rate_limit = _rate_limit_from_headers(response.headers)
            status_code = response.status_code or 0
            if status_code == 429:
                raise ProviderRateLimitError(
                    "OMDb", response.headers.get("Retry-After"), self.last_rate_limit
                )
            if status_code >= 500:
                raise ProviderTransientError(f"OMDb transient failure: {status_code}")
            response.raise_for_status()
            return response.json()

    @staticmethod
    async def test_service(url: str, api_key: str) -> bool:
        client = OMDbClient(api_key=api_key, base_url=url or DEFAULT_OMDB_BASE_URL)
        payload = await client._get_json(
            params={"apikey": client.api_key, "i": "tt3896198"}
        )
        return not (
            isinstance(payload, dict)
            and str(payload.get("Response", "")).lower() == "false"
        )


def parse_omdb_ratings(payload: Any) -> ExternalRatingValues:
    values = ExternalRatingValues()
    if not isinstance(payload, dict):
        return values

    metascore = _parse_score(payload.get("Metascore"))
    if metascore is not None:
        values.metacritic_metascore = metascore

    ratings = payload.get("Ratings")
    if isinstance(ratings, list):
        for item in ratings:
            if not isinstance(item, dict):
                continue
            source = _normalize_key(item.get("Source"))
            score = _parse_score(item.get("Value"))
            if score is None:
                continue
            if "rottentomatoes" in source:
                values.rottentomatoes_tomato_meter = score
            elif "metacritic" in source:
                values.metacritic_metascore = score
    return values


def parse_mdblist_ratings(payload: Any) -> ExternalRatingValues:
    values = ExternalRatingValues()
    if not isinstance(payload, dict):
        return values

    ratings = payload.get("ratings")
    if not isinstance(ratings, list):
        return values

    for item in ratings:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").strip().lower()
        score = _rating_score(item, source)
        votes = _parse_count(item.get("votes"))
        if source == "tomatoes":
            values.rottentomatoes_tomato_meter = score
            values.rottentomatoes_tomato_vote_count = votes
        elif source == "popcorn":
            values.rottentomatoes_popcorn_meter = score
            values.rottentomatoes_popcorn_vote_count = votes
        elif source == "metacritic":
            values.metacritic_metascore = score
            values.metacritic_vote_count = votes
        elif source == "metacriticuser":
            values.metacritic_user_score = score
            values.metacritic_user_vote_count = votes
        elif source == "trakt":
            values.trakt_rating = score
            values.trakt_vote_count = votes
        elif source == "letterboxd":
            values.letterboxd_score = score
            values.letterboxd_vote_count = votes
    return values


def _rating_score(item: dict[str, Any], source: str) -> int | None:
    score = _parse_score(item.get("score"))
    if score is not None:
        return score

    value = _parse_numeric(item.get("value"))
    if value is None:
        return None
    if source == "letterboxd" and value <= 5:
        return max(0, min(100, int(round(value * 20))))
    if source in {"metacriticuser"} and value <= 10:
        return max(0, min(100, int(round(value * 10))))
    return _parse_score(value)


def _parse_score(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        if raw < 0:
            return None
        value = raw * 100 if 0 < raw <= 1 else raw
        return max(0, min(100, int(round(value))))

    text = str(raw).strip()
    if not text or text.upper() == "N/A":
        return None

    if "/" in text:
        left, right = text.split("/", 1)
        try:
            numerator = float(left.strip().replace(",", ""))
            denominator = float(right.strip().replace(",", ""))
        except ValueError:
            return None
        if denominator <= 0:
            return None
        return max(0, min(100, int(round((numerator / denominator) * 100))))

    if text.endswith("%"):
        text = text[:-1]
    try:
        value = float(text.replace(",", ""))
    except ValueError:
        return None
    if value < 0:
        return None
    value = value * 100 if 0 < value <= 1 else value
    return max(0, min(100, int(round(value))))


def _parse_count(raw: Any) -> int | None:
    value = _parse_numeric(raw)
    if value is None or value < 0:
        return None
    return int(round(value))


def _parse_numeric(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if not text or text.upper() == "N/A":
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _normalize_key(raw: Any) -> str:
    return "".join(ch for ch in str(raw or "").strip().lower() if ch.isalnum())


def _rate_limit_from_headers(headers: Any) -> ProviderRateLimitSnapshot | None:
    snapshot = ProviderRateLimitSnapshot(
        limit=_parse_count(headers.get("X-RateLimit-Limit")),
        remaining=_parse_count(headers.get("X-RateLimit-Remaining")),
        reset_epoch=_parse_count(headers.get("X-RateLimit-Reset")),
    )
    if (
        snapshot.limit is None
        and snapshot.remaining is None
        and snapshot.reset_epoch is None
    ):
        return None
    return snapshot
