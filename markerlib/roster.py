"""Load a participant roster from a CSV into a list of display labels.

The videographer's roster typically has separate **handler** and **dog**
columns; those are joined per row into one participant label as
``First & Dog`` -- the handler's **first name** and the dog name (e.g. handler
"Sara Johnson", dog "Tracer" -> ``Sara & Tracer``). That participant becomes the
output **folder** per run, with the search/event label as the filename inside it.
The parser is forgiving:

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


def _handler_dog_label(handler: str, dog: str) -> str:
    """``First & Dog`` from a handler + dog pair: the handler's first name (first
    word) and the dog name. Falls back gracefully if either is missing."""
    handler, dog = handler.strip(), dog.strip()
    first = handler.split()[0] if handler.split() else ""
    if first and dog:
        return f"{first} & {dog}"
    return first or dog


def load_participants(path: str | Path) -> list[str]:
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        rows = [r for r in csv.reader(fh)]
    rows = [r for r in rows if any(cell.strip() for cell in r)]
    if not rows:
        return []

    use_cols: list[int] | None = None
    person_col: int | None = None   # set (with dog_col) when we have a handler+dog pair
    dog_col: int | None = None
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
            if partner is not None and partner != di:
                person_col, dog_col = partner, di     # -> "First & Dog"
                use_cols = [partner, di]
            else:
                use_cols = [di]
        elif ni is not None:
            use_cols = [ni]
        else:
            # join everything except obvious id columns
            use_cols = [i for i, h in enumerate(header) if h not in _ID_KEYS] or list(range(len(header)))

    labels: list[str] = []
    for row in data:
        if person_col is not None and dog_col is not None:
            handler = row[person_col] if person_col < len(row) else ""
            dog = row[dog_col] if dog_col < len(row) else ""
            label = _handler_dog_label(handler, dog)
        elif use_cols is not None:
            label = " ".join(row[c].strip() for c in use_cols if c < len(row) and row[c].strip())
        else:
            label = " ".join(p.strip() for p in row if p.strip())
        if label:
            labels.append(label)
    return labels
