import re
from datetime import timedelta


_TOKEN_RE = re.compile(r"(\d+)\s*([a-zA-Z]+)")

_UNIT_SECONDS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "w": 604800,
    "week": 604800,
    "weeks": 604800,
    # The requester explicitly asked for "2m" as months, and "min" for minutes.
    "m": 2592000,
    "mo": 2592000,
    "mon": 2592000,
    "month": 2592000,
    "months": 2592000,
}


def parse_duration_to_seconds(duration_text: str) -> int:
    """Parse duration text like '1d 2m 5min' into total seconds."""
    if duration_text is None:
        raise ValueError("Duration is required.")

    text = duration_text.strip().lower()
    if not text:
        raise ValueError("Duration is required.")

    total_seconds = 0
    cursor = 0

    for match in _TOKEN_RE.finditer(text):
        between = text[cursor:match.start()]
        if between and not all(ch in {" ", ",", "+"} for ch in between):
            raise ValueError("Invalid duration format.")

        value = int(match.group(1))
        unit = match.group(2)
        if unit not in _UNIT_SECONDS:
            raise ValueError(f"Unsupported unit: {unit}")

        total_seconds += value * _UNIT_SECONDS[unit]
        cursor = match.end()

    trailing = text[cursor:]
    if trailing and not all(ch in {" ", ",", "+"} for ch in trailing):
        raise ValueError("Invalid duration format.")

    if total_seconds <= 0:
        raise ValueError("Duration must be greater than 0.")

    return total_seconds


def parse_duration_to_timedelta(duration_text: str) -> timedelta:
    return timedelta(seconds=parse_duration_to_seconds(duration_text))


def format_duration(total_seconds: int) -> str:
    seconds = max(0, int(total_seconds))
    if seconds == 0:
        return "0 seconds"

    units = [
        ("month", 2592000),
        ("day", 86400),
        ("hour", 3600),
        ("minute", 60),
        ("second", 1),
    ]

    parts = []
    for name, unit_seconds in units:
        count, seconds = divmod(seconds, unit_seconds)
        if count:
            suffix = "" if count == 1 else "s"
            parts.append(f"{count} {name}{suffix}")

    return " ".join(parts)