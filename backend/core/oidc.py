from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from inspect import isawaitable
from typing import Any, Literal

from authlib.integrations.starlette_client import OAuth

_CLIENT_NAME = "reclaimerr_oidc"
TokenEndpointAuthMethod = Literal["client_secret_basic", "client_secret_post"]


@dataclass(frozen=True, slots=True)
class OIDCProviderMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str | None = None


class OIDCError(Exception):
    """Base class for OIDC flow errors."""


class OIDCConfigError(OIDCError):
    """Configuration validation failed."""


class OIDCExchangeError(OIDCError):
    """Authorization code/token exchange failed."""


class OIDCValidationError(OIDCError):
    """Token validation failed."""


def normalize_scopes(scopes: str) -> str:
    """Normalize scopes to a deduped, space-separated string."""
    parts: list[str] = []
    seen: set[str] = set()
    for part in scopes.split():
        normalized = part.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        parts.append(normalized)

    if "openid" not in seen:
        parts.insert(0, "openid")
    return " ".join(parts) if parts else "openid profile email"


def normalize_issuer_url(issuer_url: str) -> str:
    return issuer_url.strip().rstrip("/")


def normalize_token_endpoint_auth_method(
    value: str | None,
) -> TokenEndpointAuthMethod:
    if value == "client_secret_post":
        return "client_secret_post"
    return "client_secret_basic"


def oidc_discovery_url(issuer_url: str) -> str:
    normalized = normalize_issuer_url(issuer_url)
    if not normalized:
        raise OIDCConfigError("OIDC issuer URL is required")
    return f"{normalized}/.well-known/openid-configuration"


def create_oidc_client(
    *,
    issuer_url: str,
    client_id: str,
    client_secret: str,
    scopes: str,
    token_endpoint_auth_method: TokenEndpointAuthMethod = "client_secret_basic",
) -> Any:
    """Create an Authlib OIDC client from persisted settings."""
    normalized_issuer = normalize_issuer_url(issuer_url)
    if not normalized_issuer:
        raise OIDCConfigError("OIDC issuer URL is required")
    if not client_id.strip():
        raise OIDCConfigError("OIDC client ID is required")
    if not client_secret.strip():
        raise OIDCConfigError("OIDC client secret is required")

    oauth = OAuth()
    oauth.register(
        name=_CLIENT_NAME,
        client_id=client_id.strip(),
        client_secret=client_secret,
        server_metadata_url=oidc_discovery_url(normalized_issuer),
        client_kwargs={
            "scope": normalize_scopes(scopes),
            "token_endpoint_auth_method": token_endpoint_auth_method,
        },
    )
    client = oauth.create_client(_CLIENT_NAME)
    if client is None:
        raise OIDCConfigError("Failed to create OIDC client")
    return client


def _read_metadata_str(metadata: Mapping[str, Any], key: str) -> str:
    value = metadata.get(key)
    if not isinstance(value, str) or not value.strip():
        raise OIDCValidationError(f"OIDC provider metadata missing required '{key}'")
    return value.strip()


def parse_provider_metadata(metadata: Mapping[str, Any]) -> OIDCProviderMetadata:
    userinfo_endpoint = metadata.get("userinfo_endpoint")
    return OIDCProviderMetadata(
        issuer=_read_metadata_str(metadata, "issuer").rstrip("/"),
        authorization_endpoint=_read_metadata_str(metadata, "authorization_endpoint"),
        token_endpoint=_read_metadata_str(metadata, "token_endpoint"),
        jwks_uri=_read_metadata_str(metadata, "jwks_uri"),
        userinfo_endpoint=(
            userinfo_endpoint.strip()
            if isinstance(userinfo_endpoint, str) and userinfo_endpoint.strip()
            else None
        ),
    )


async def fetch_provider_metadata(
    issuer_url: str,
    *,
    client_id: str = "metadata-test",
    client_secret: str = "metadata-test",
    scopes: str = "openid profile email",
    token_endpoint_auth_method: TokenEndpointAuthMethod = "client_secret_basic",
    force_refresh: bool = False,
) -> OIDCProviderMetadata:
    """Fetch OpenID discovery metadata through Authlib."""
    _ = force_refresh  # authlib owns discovery/cache behavior for runtime clients.
    client = create_oidc_client(
        issuer_url=issuer_url,
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
        token_endpoint_auth_method=token_endpoint_auth_method,
    )
    return await load_provider_metadata(client, issuer_url=issuer_url)


async def load_provider_metadata(
    client: Any,
    *,
    issuer_url: str | None = None,
) -> OIDCProviderMetadata:
    """Load OpenID discovery metadata from an Authlib client."""
    try:
        metadata = await client.load_server_metadata()
    except OIDCError:
        raise
    except Exception as exc:
        discovery_url = oidc_discovery_url(issuer_url) if issuer_url else "provider"
        raise OIDCExchangeError(
            f"Failed to fetch OIDC discovery metadata from {discovery_url}"
        ) from exc

    if not isinstance(metadata, Mapping):
        raise OIDCValidationError("OIDC discovery metadata is not a JSON object")
    return parse_provider_metadata(metadata)


async def extract_userinfo(
    client: Any,
    token_payload: Mapping[str, Any],
    *,
    required_claim: str | None = None,
) -> dict[str, Any]:
    """Read parsed ID token claims, falling back to UserInfo when available."""
    claims: dict[str, Any] = {}
    userinfo = token_payload.get("userinfo")
    if isinstance(userinfo, Mapping):
        claims.update(userinfo)
        if required_claim is None or claims.get(required_claim) is not None:
            return claims

    userinfo_method = getattr(client, "userinfo", None)
    if not callable(userinfo_method) or not token_payload.get("access_token"):
        return claims

    try:
        payload: Any = userinfo_method(token=token_payload)
        if isawaitable(payload):
            payload = await payload
    except Exception as exc:
        raise OIDCExchangeError("Failed to fetch OIDC userinfo payload") from exc

    if isinstance(payload, Mapping):
        claims.update(payload)
    return claims


def extract_claim_as_string(
    claims: Mapping[str, Any],
    claim_name: str,
) -> str | None:
    """Extract a claim as a normalized string."""
    value = claims.get(claim_name)
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    normalized = str(value).strip()
    return normalized or None
