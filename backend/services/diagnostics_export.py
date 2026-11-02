"""Conservative export policies: unknown text is aliased, never guessed safe."""

import math
import re
from datetime import UTC, datetime

from pydantic import JsonValue

from backend.core.rule_engine import FIELD_LABELS, OPERATOR_LABELS


def timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (
            parsed.replace(tzinfo=UTC)
            if parsed.tzinfo is None
            else parsed.astimezone(UTC)
        )
    except ValueError:
        return None


class Exporter:
    def __init__(self, include_paths: bool):
        self.include_paths = include_paths
        self._aliases: dict[str, dict[str, str]] = {}

    def alias(self, category: str, value: object) -> str | None:
        if value is None or value == "":
            return None
        values = self._aliases.setdefault(category, {})
        key = str(value)
        if key not in values:
            values[key] = f"{category}-{len(values) + 1}"
        return values[key]

    def path(self, value: object) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        if not self.include_paths:
            return self.alias("path", value)
        # Paths may contain URL credentials, query tokens and home account names.
        # Refuse URL-like values entirely; path opt-in never enables credentials.
        if re.search(r"://|[?@#]|(?i:token|password|secret|api[_-]?key)=", value):
            return self.alias("omitted-path", value)
        value = re.sub(
            r"(?i)([/\\](?:users|home)[/\\])([^/\\]+)",
            lambda m: m[1] + str(self.alias("user", m[2])),
            value,
        )
        value = re.sub(
            r"^([/\\]{2})([^/\\]+)",
            lambda m: m[1] + str(self.alias("host", m[2])),
            value,
        )
        return value[:2048]

    def title(self, value: object) -> str | None:
        if not isinstance(value, str):
            return None
        # Media titles are the sole intentional free-text exception.
        if re.search(
            r"://|[\w.+-]+@[\w.-]+|(?i:password|token|secret|api[_-]?key)\s*[:=]", value
        ):
            return self.alias("omitted-title", value)
        return value[:512]

    def literal(self, value: object, field: str, depth: int = 0) -> JsonValue:
        if depth > 8:
            return "omitted: nesting limit"
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, (str, int)) and field == "library.id":
            return self.alias("library-reference", value)
        if isinstance(value, int) and ("usernames" in field or "user_ids" in field):
            return self.alias("user", value)
        if isinstance(value, (int, float)):
            return value if math.isfinite(value) else None
        if isinstance(value, str):
            category = "user" if "user" in field else "literal"
            if "path" in field:
                return self.path(value)
            if field == "library.id":
                category = "library-reference"
            return self.alias(category, value)
        if isinstance(value, list):
            result = [self.literal(v, field, depth + 1) for v in value[:100]]
            if len(value) > 100:
                result.append("omitted: list limit")
            return result
        if isinstance(value, dict):
            # Compound user-duration values have only these documented members.
            return {
                key: self.literal(
                    value[key],
                    "usernames" if key == "usernames" else "amount",
                    depth + 1,
                )
                for key in ("usernames", "amount")
                if key in value
            } or "omitted: unsupported object"
        return "omitted: unsupported value"

    def rule(self, value: object, depth: int = 0) -> JsonValue:
        if depth > 8 or not isinstance(value, dict):
            return "omitted: unsupported rule structure"
        # Both rule groups and leaves are reconstructed; arbitrary keys/text
        # (including saved display strings) never enter the response.
        if "root" in value:
            return {"root": self.rule(value["root"], depth + 1)}
        children = value.get("children", value.get("conditions"))
        if isinstance(children, list):
            logic = value.get("op", value.get("operator", value.get("logic", "and")))
            return {
                "enabled": value.get("enabled") is not False,
                "logic": logic if logic in ("and", "or", "AND", "OR") else "unknown",
                "conditions": [self.rule(v, depth + 1) for v in children[:100]],
                "truncated": len(children) > 100,
            }
        field = value.get("field")
        if not isinstance(field, str) or field not in FIELD_LABELS:
            return "omitted: unknown field"
        operator = value.get("operator", value.get("op"))
        return {
            "enabled": value.get("enabled") is not False,
            "field": field,
            "operator": operator
            if isinstance(operator, str) and operator in OPERATOR_LABELS
            else "unknown",
            "value": self.literal(value.get("value"), field),
        }
