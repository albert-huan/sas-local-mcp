"""Bounded XPORT record windows; ReadStat still owns all value decoding."""

from io import BytesIO
from typing import BinaryIO


_CARD = 80
_BLOCK = 1024 * 1024
_TAIL_BLOCK = 8192


class XportFallback(Exception):
    """The reader or file cannot use the fixed-width fast path."""


def layout(source: BinaryIO, frame, meta) -> tuple[int, int, int, int]:
    """Return data start, row width, EOF and declared observation count (0 if absent)."""
    start = source.tell()
    widths = getattr(meta, "variable_storage_width", None) or {}
    if (not _CARD <= start <= _BLOCK or start % _CARD or not widths
            or len(widths) != len(frame.columns)
            or any(not isinstance(width, int) or width <= 0 for width in widths.values())):
        raise XportFallback
    width = sum(widths.values())
    source.seek(start - _CARD)
    card = source.read(_CARD)
    if (width > _BLOCK or not card.startswith(b"HEADER RECORD*******")
            or card[20:28].strip() not in {b"OBS", b"OBSV8"}):
        raise XportFallback
    end = source.seek(0, 2)
    if end < start or end % _CARD:
        raise XportFallback
    declared = 0
    if card[20:28].strip() == b"OBSV8":
        field = card[48:63].strip()
        if field and not field.isdigit():
            raise XportFallback
        declared = int(field) if field else 0
        if declared > (end - start) // width:
            raise XportFallback
    return start, width, end, declared


def _read_at(source: BinaryIO, offset: int, length: int) -> bytes:
    source.seek(offset)
    data = source.read(length)
    if len(data) != length:
        raise XportFallback
    return data


def count_rows(source: BinaryIO, start: int, width: int, end: int, declared: int) -> int:
    """Match pyreadstat's XPORT reader when the OBS count is known or inferable."""
    if declared:
        return declared
    end -= (end - start) % width  # ReadStat ignores the last incomplete row.
    while end > start:
        offset = max(start, end - _TAIL_BLOCK)
        nonblank = _read_at(source, offset, end - offset).rstrip(b" ")
        if nonblank:
            return (offset + len(nonblank) - start + width - 1) // width
        end = offset
    return 0


def windows(source: BinaryIO, start: int, width: int, rows: int, declared: int, batch_rows: int):
    """Yield independent bounded files, retaining internal blank rows across windows."""
    header = _read_at(source, 0, start)
    step = min(batch_rows, _BLOCK // width)
    offset = 0
    while offset < rows:
        size = min(step if offset else min(1024, step), rows - offset)
        data = _read_at(source, start + offset * width, size * width)
        if not declared and offset + size < rows and not data[-width:].rstrip(b" "):
            # A real, nonblank row lets ReadStat distinguish internal blank rows from
            # padding. The caller caps decoding at `size`, so this row is not returned.
            data += _read_at(source, start + (rows - 1) * width, width)
        yield BytesIO(header + data), size
        offset += size
