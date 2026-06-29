"""Load a participant roster from a CSV into a list of display labels.

The videographer's roster typically has separate **handler** and **dog**
columns; those are joined per row (handler first) into one participant label
like "Smith Rex" (which sanitizes to ``Smith_Rex`` on export). The parser is
forgiving:

  * with a header, it prefers handler/dog columns, else a single name column,
    else joins all non-id columns;
  * without a header, it joins all columns in the row in order.

Blank rows are skipped. Returns labels in file order (duplicates kept).
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

_HANDLER_KEYS = ("handler", "owner", "person", "competitor", "exhibitor")
_DOG_KEYS = ("dog", "dog name", "call name", "callname", "k9")
_NAME_KEYS = ("participant", "name", "label", "entry")
_ID_KEYS = ("bib", "no", "no.", "number", "#", "id", "armband", "run")
_NUMERIC_RE = re.compile(r"^\d+$")


def _looks_like_header(row: list[str]) -> bool:
    """A header row has no purely-numeric cells and at least one known word."""
    cells = [c.strip().lower() for c in row]
    if any(_NUMERIC_RE.match(c) for c in cells):
        return False
    known = set(_HANDLER_KEYS + _DOG_KEYS + _NAME_KEYS + _ID_KEYS)
    return any(c in known for c in cells)


def _find(cols: list[str], keys: tuple[str, ...]) -> int | None:
    for i, c in enumerate(cols):
        if c in keys:
            return i
    return None


def load_participants(path: str | Path) -> list[str]:
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        rows = [r for r in csv.reader(fh)]
    rows = [r for r in rows if any(cell.strip() for cell in r)]
    if not rows:
        return []

    use_cols: list[int] | None = None
    data = rows
    if _looks_like_header(rows[0]):
        header = [c.strip().lower() for c in rows[0]]
        data = rows[1:]
        hi = _find(header, _HANDLER_KEYS)
        di = _find(header, _DOG_KEYS)
        ni = _find(header, _NAME_KEYS)
        if di is not None:
            # Pair the dog column with the person column (handler, else
            # participant/name, else the first other non-id column).
            partner = hi if hi is not None else ni
            if partner is None:
                partner = next(
                    (i for i in range(len(header)) if i != di and header[i] not in _ID_KEYS),
                    None,
                )
            use_cols = [partner, di] if (partner is not None and partner != di) else [di]
        elif ni is not None:
            use_cols = [ni]
        else:
            # join everything except obvious id columns
            use_cols = [i for i, h in enumerate(header) if h not in _ID_KEYS] or list(range(len(header)))

    labels: list[str] = []
    for row in data:
        if use_cols is not None:
            parts = [row[c] for c in use_cols if c < len(row)]
        else:
            parts = list(row)
        label = " ".join(p.strip() for p in parts if p.strip())
        if label:
            labels.append(label)
    return labels
