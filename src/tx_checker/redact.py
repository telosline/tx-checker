import re

# Matches the apikey query parameter in any case, up to the next separator.
_APIKEY_RE = re.compile(r"(?i)(apikey=)[^&\s\"'#]*")

REDACTED = "***"


def redact(text: str) -> str:
    """Replace the value of every `apikey=` parameter with `***`."""
    return _APIKEY_RE.sub(lambda m: m.group(1) + REDACTED, text)
