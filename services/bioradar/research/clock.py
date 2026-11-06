"""Times as records and Fabric give them."""

from datetime import UTC, datetime


def parse_time(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value)
    if len(text) == 10:  # day precision: the whole day counts as available
        return datetime.fromisoformat(text).replace(hour=23, minute=59, tzinfo=UTC)
    if len(text) == 7:
        return datetime.fromisoformat(text + "-28").replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
