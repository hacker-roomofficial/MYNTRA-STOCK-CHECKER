from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Status(str, Enum):
    OK = "OK"                    # every watched size is in stock
    PARTIAL_OOS = "PARTIAL_OOS"  # some watched sizes out of stock
    ALL_OOS = "ALL_OOS"          # nothing watched is in stock
    NOT_FOUND = "NOT_FOUND"      # style delisted / 404
    ERROR = "ERROR"              # could not be checked (retried later)


ALERT_STATUSES = (Status.PARTIAL_OOS, Status.ALL_OOS)


def norm_size(label: str) -> str:
    """Normalize a size label without relying on a fixed size catalogue.

    Handles alphabetic sizes (including future XXXXS/7XL variants), numeric
    sizes, and Myntra spellings such as ``3 XL``. Unknown labels are retained
    rather than discarded so newly introduced sizes remain matchable.
    """
    value = " ".join(str(label).strip().upper().replace("\u00a0", " ").split())
    compact = value.replace(" ", "")

    # Numeric XL notation maps to the equivalent X-count notation:
    # 2XL -> XXL, 3XL -> XXXL, 5XL -> XXXXXL, etc.
    import re
    match = re.fullmatch(r"([1-9][0-9]?)X([SL])", compact)
    if match:
        count = int(match.group(1))
        if count <= 20:
            return ("X" * count) + match.group(2)

    # Compact common compound labels, but do not rewrite unknown labels.
    if re.fullmatch(r"X{1,20}[SL]", compact):
        return compact
    if re.fullmatch(r"[0-9]{1,3}(?:UK|US|EU|IN)?", compact):
        return compact
    if compact in {"FREESIZE", "ONESIZE"}:
        return compact
    return compact


@dataclass(slots=True)
class Item:
    style_id: str
    watch_sizes: tuple[str, ...] = ()  # empty = watch every size on the page


@dataclass(slots=True)
class SizeInfo:
    label: str
    available: bool
    qty: int | None = None


@dataclass(slots=True)
class StyleResult:
    style_id: str
    status: Status
    name: str = ""
    brand: str = ""
    sizes: list[SizeInfo] = field(default_factory=list)
    oos_sizes: list[str] = field(default_factory=list)
    in_sizes: list[str] = field(default_factory=list)
    error: str = ""
    checked_at: float = field(default_factory=time.time)

    @property
    def url(self) -> str:
        return f"https://www.myntra.com/{self.style_id}"

    @property
    def signature(self) -> str:
        return f"{self.status.value}|{','.join(sorted(self.oos_sizes))}"
