"""Chunking. Ids are created once here and never renumbered.

Invariant 2: chunk ids are immutable. Every factual claim in a generated
artefact cites a chunk_id, so if ids shift the citations become meaningless
(TC-0114).
"""

from __future__ import annotations

from app.graph.state import Chunk

# ~800 tokens with overlap (ARCHITECTURE.md §2). Character-based because the
# splitter is character-based; ~4 chars/token is the usual approximation.
CHUNK_SIZE_CHARS = 3200
CHUNK_OVERLAP_CHARS = 320


def split(text: str, source_id: str, page_map: list[tuple[int, int]] | None = None) -> list[Chunk]:
    """Split text into stable, sequentially-numbered chunks.

    Ids are c1, c2, ... in document order. Deterministic for identical input:
    re-reading a source yields byte-identical ids (TC-0114).

    page_map, when supplied, is [(char_offset, page_number)] used to attribute
    each chunk to a page for citation display.
    """
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    text = (text or "").strip()
    if not text:
        return []

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE_CHARS,
        chunk_overlap=CHUNK_OVERLAP_CHARS,
        separators=["\n\n", "\n", ". ", " ", ""],
        length_function=len,
    )

    chunks: list[Chunk] = []
    cursor = 0
    for i, piece in enumerate(splitter.split_text(text), start=1):
        piece = piece.strip()
        if not piece:
            continue
        # Track position so a chunk can be attributed to a page. find() from the
        # running cursor keeps this O(n) and correct when a passage repeats.
        offset = text.find(piece, cursor)
        if offset == -1:
            offset = cursor
        cursor = offset + len(piece)
        chunks.append(
            Chunk(
                id=f"c{i}",
                text=piece,
                page=_page_for(offset, page_map),
                source_id=source_id,
            )
        )
    return chunks


def _page_for(offset: int, page_map: list[tuple[int, int]] | None) -> int | None:
    if not page_map:
        return None
    page = None
    for start, number in page_map:
        if offset >= start:
            page = number
        else:
            break
    return page


def page_map_from_pages(pages: list[str], joiner: str = "\n\n") -> list[tuple[int, int]]:
    """Build [(char_offset, page_number)] from per-page text, 1-indexed."""
    out: list[tuple[int, int]] = []
    offset = 0
    for i, page_text in enumerate(pages, start=1):
        out.append((offset, i))
        offset += len(page_text) + len(joiner)
    return out
