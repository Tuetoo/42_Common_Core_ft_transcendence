import re
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any
import structlog
from pypdf import PdfReader
from shared.enums import DocumentCategory

logger = structlog.get_logger(__name__)


def _parse_pdf(content: bytes) -> str:
    reader = PdfReader(BytesIO(content))
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)


def _parse_text(content: bytes) -> str:
    return content.decode("utf-8", errors="replace")


_PARSERS = {"pdf": _parse_pdf, "txt": _parse_text, "md": _parse_text}


def parse_document(content: bytes, document_type: str) -> str:
    parser = _PARSERS.get(document_type)
    if parser is None:
        raise ValueError(
            f"No parser for document_type={document_type!r}. Supported: "
            f"{', '.join(sorted(_PARSERS))}.")
    return parser(content)


@dataclass
class ChunkConfig:
    target_words: int
    overlap_words: int


_CATEGORY_CONFIG: dict[DocumentCategory, ChunkConfig] = {
    DocumentCategory.FILING: ChunkConfig(target_words=600, overlap_words=100),
    DocumentCategory.ANALYST_REPORT: ChunkConfig(
        target_words=400, overlap_words=80),
    DocumentCategory.EARNINGS_CALL: ChunkConfig(
        target_words=350, overlap_words=70),
    DocumentCategory.NEWS: ChunkConfig(target_words=250, overlap_words=40),
    DocumentCategory.MACRO_RESEARCH: ChunkConfig(
        target_words=400, overlap_words=80),
    DocumentCategory.RISK_ANALYSIS: ChunkConfig(
        target_words=400, overlap_words=80)
}


@dataclass
class Chunk:
    text: str
    chunk_index: int
    category: DocumentCategory
    metadata: dict[str, Any] = field(default_factory=dict)


def _split_long_paragraph(
        words: list[str], target_words: int,
        overlap_words: int) -> list[list[str]]:
    if len(words) <= target_words:
        return [words]
    windows = []
    step = max(target_words - overlap_words, 1)
    for start in range(0, len(words), step):
        window = words[start:start + target_words]
        if window:
            windows.append(window)
        if start + target_words >= len(words):
            break
    return windows


_MAX_WORD_CHARS = 200


def _break_up_long_words(text: str, max_chars: int = _MAX_WORD_CHARS) -> str:
    fixed_words: list[str] = []
    for word in text.split():
        if len(word) > max_chars:
            fixed_words.extend(
                word[i:i + max_chars] for i in range(0, len(word), max_chars))
        else:
            fixed_words.append(word)
    return " ".join(fixed_words)


_BOILERPLATE_MARKERS = (
    "has not been reviewed or authorized",
    "has not been reviewed and authorized", "accepts no responsibility",
    "no responsibility for", "registered broker-dealer",
    "financial services licence", "financial services license",
    "dfsa reference number", "proprietary position", "registration number")


def _paragraph_marker_hits(paragraph: str) -> frozenset[str]:
    lowered = paragraph.lower()
    return frozenset(
        marker for marker in _BOILERPLATE_MARKERS if marker in lowered)


def _boilerplate_run_indices(paragraphs: list[str]) -> set[int]:
    hit_sets = [_paragraph_marker_hits(p) for p in paragraphs]
    flagged: set[int] = set()
    run_start: int | None = None
    run_union: set[str] = set()

    def _close_run(end: int) -> None:
        if run_start is not None and len(run_union) >= 2:
            flagged.update(range(run_start, end))

    for i, hits in enumerate(hit_sets):
        if hits:
            if run_start is None:
                run_start = i
                run_union = set(hits)
            else:
                run_union |= hits
        else:
            _close_run(i)
            run_start = None
            run_union = set()
    _close_run(len(hit_sets))
    return flagged


_TAG_LIKE_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*:[A-Za-z]")


def _is_tag_soup_paragraph(paragraph: str) -> bool:
    words = paragraph.split()
    if not words:
        return False
    tag_like = sum(1 for w in words if _TAG_LIKE_RE.match(w))
    return tag_like / len(words) > 0.25


def chunk_document(
        text: str, category: DocumentCategory, *,
        metadata: dict[str, Any] | None = None) -> list[Chunk]:
    config = _CATEGORY_CONFIG[category]
    metadata = metadata or {}

    raw_paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    boilerplate_run_idxs = _boilerplate_run_indices(raw_paragraphs)
    paragraphs: list[str] = []
    filtered_count = 0
    for i, raw_paragraph in enumerate(raw_paragraphs):
        if (i in boilerplate_run_idxs
                or _is_tag_soup_paragraph(raw_paragraph)):
            filtered_count += 1
            continue
        paragraphs.append(_break_up_long_words(raw_paragraph))

    if filtered_count and not paragraphs and len(raw_paragraphs) <= 2:
        logger.warning(
            "chunking.filter_would_empty_document_falling_back_to_unfiltered",
            category=category.value, paragraph_count=len(raw_paragraphs))
        paragraphs = [_break_up_long_words(p) for p in raw_paragraphs]
        filtered_count = 0

    if filtered_count:
        logger.info(
            "chunking.filtered_low_value_paragraphs", category=category.value,
            count=filtered_count)
    if not paragraphs:
        return []

    chunks: list[Chunk] = []
    current_words: list[str] = []

    def _flush() -> None:
        if current_words:
            chunks.append(
                Chunk(
                    text=" ".join(current_words), chunk_index=len(chunks),
                    category=category, metadata=dict(metadata)))

    for paragraph in paragraphs:
        windows = _split_long_paragraph(
            paragraph.split(), config.target_words, config.overlap_words)
        for window_idx, para_words in enumerate(windows):
            if (current_words and len(current_words) + len(para_words)
                    > config.target_words):
                _flush()
                overlap_len = (
                    0 if window_idx > 0 else min(
                        config.overlap_words,
                        max(config.target_words - len(para_words), 0)))
                overlap = current_words[-overlap_len:] if overlap_len else []
                current_words = [*overlap, *para_words]
            else:
                current_words.extend(para_words)
    _flush()

    return chunks
