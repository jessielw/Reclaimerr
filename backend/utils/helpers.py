__all__ = ["normalize_leaving_soon_collection_title"]


def normalize_leaving_soon_collection_title(value: str | None) -> str:
    """Helper for normalizing the leaving soon collection title, ensuring it is not
    empty or just whitespace."""
    if value is None:
        return "Leaving Soon"
    title = str(value or "").strip()
    return title or "Leaving Soon"
