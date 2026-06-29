"""Small custom widgets for the marking tool.

* :class:`ParticipantList` -- the roster panel. Clicking a name assigns that
  participant to the current clip; the window wires :meth:`QListWidget.itemClicked`.
"""

from __future__ import annotations

from PySide6.QtWidgets import QAbstractItemView, QListWidget


class ParticipantList(QListWidget):
    """Roster list; clicking an item assigns that participant to the current clip."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setUniformItemSizes(True)
        self.setAlternatingRowColors(True)
