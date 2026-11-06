"""Text helpers shared across the radar."""

import re


def squash(text):
    """Text with every whitespace character removed: how quotes, names and search terms are compared with
    the material and the question they come from, whose spacing differs."""
    return re.sub(r"\s+", "", text or "")
