"""Text helpers shared across the radar."""

import re


def squash(text):
    return re.sub(r"\s+", "", text or "")
