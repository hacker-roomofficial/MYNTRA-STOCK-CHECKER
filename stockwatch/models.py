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
    return " ".join(str(label).strip().upper().split())


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
