"""Parse model indexing suggestions without changing memory authorization."""


def _parse_embedding_decision(item: dict) -> tuple[bool, str]:
    """Only a real boolean can opt out; missing or malformed advice keeps indexing."""
    enabled = item.get("embedding_enabled")
    reason = item.get("embedding_reason")
    return (enabled if isinstance(enabled, bool) else True,
            reason[:200] if isinstance(reason, str) else "")
