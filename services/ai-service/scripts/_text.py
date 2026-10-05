SAMPLE_CHARS = 220


def flatten(text: object, limit: int = 300) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit - 3] + "..."


def one_line(text: str | None, limit: int = SAMPLE_CHARS) -> str:
    if not text:
        return "(empty)"
    return repr(flatten(text, limit))
