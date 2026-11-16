"""Text normalisation shared by the proposition and slot code."""


def norm(text):
    return " ".join((text or "").split()).casefold()
