"""Text helpers for Turkish."""

from __future__ import annotations

_FOLD = str.maketrans(
    {
        "İ": "i", "I": "i", "ı": "i",
        "Ş": "s", "ş": "s",
        "Ç": "c", "ç": "c",
        "Ğ": "g", "ğ": "g",
        "Ö": "o", "ö": "o",
        "Ü": "u", "ü": "u",
    }
)  # fmt: skip


def fold_tr(value: str) -> str:
    """Search key that ignores case and Turkish diacritics.

    "isil", "ışıl" and "IŞIL" all find "Işıl", so a search still works from a
    keyboard without Turkish letters.
    """
    return value.translate(_FOLD).casefold()
