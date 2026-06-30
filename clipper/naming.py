"""Filename sanitizing.

This is the single source of truth used by BOTH the marking tool and the batch
cutter, so a label always maps to the exact same output filename no matter
which side generates it. Change it here and both sides change together.

Rules:
  * The user types a label naturally ("Sara", search "Interior Search 1").
  * The marker joins participant + search as ``Participant - Search``.
  * On output that becomes a readable filename that keeps spaces and case but
    strips characters a filesystem won't allow: ``Sara - Interior Search 1.mp4``.

There is no sequence-number prefix; the cutter de-duplicates within a batch so
two clips that would share a name don't overwrite each other.
"""

from __future__ import annotations

import re

# Characters illegal in Windows filenames, plus a few we just don't want.
_ILLEGAL = r'<>:"/\\|?*'
_ILLEGAL_RE = re.compile(f"[{re.escape(_ILLEGAL)}]")
_APOSTROPHES = "'‘’ʼ´`"  # straight + curly + accents
_WHITESPACE_RE = re.compile(r"\s+")
_KEEP_RE = re.compile(r"[^A-Za-z0-9 ._&-]")  # note: space and & are kept ("Sara & Tracer")

# Reserved device names on Windows (case-insensitive, with or without ext).
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_FALLBACK = "clip"


def sanitize_label(label: str) -> str:
    """Turn a free-typed label into a safe, readable filename stem (no extension).

    The goal is a name a person would actually want on the file:

    * case and spaces are preserved ("Interior Search 1" stays as-is)
    * apostrophes are dropped (so "Rex's" -> "Rexs", not "Rex s")
    * characters illegal in filenames (``<>:"/\\|?*``) become a space
    * runs of whitespace collapse to a single space
    * anything left that isn't ``[A-Za-z0-9 ._-]`` is dropped
    * leading/trailing spaces, dots and separators are trimmed
    * empty / reserved results fall back to ``clip``
    """
    text = (label or "").strip()
    # Drop apostrophes entirely so contractions read naturally.
    text = "".join(ch for ch in text if ch not in _APOSTROPHES)
    # Illegal filename chars -> space (preserve the word boundary).
    text = _ILLEGAL_RE.sub(" ", text)
    # Drop any remaining unusual characters (accented letters, emoji, etc.).
    text = _KEEP_RE.sub("", text)
    # Collapse whitespace to single spaces and tidy the edges.
    text = _WHITESPACE_RE.sub(" ", text).strip(" ._-")

    if not text:
        return _FALLBACK
    if text.upper() in _RESERVED or text.split(".")[0].upper() in _RESERVED:
        text = f"{text}_{_FALLBACK}"
    return text


def build_filename(label: str, *, ext: str = "mp4") -> str:
    """Build ``<sanitized label>.<ext>`` -- e.g. ``Sara - Interior Search 1.mp4``."""
    stem = sanitize_label(label)
    ext = ext.lstrip(".")
    return f"{stem}.{ext}"
