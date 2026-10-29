"""Turn a Myntra product payload into a StyleResult.

Three payload shapes are understood, because the same data reaches us three ways:
  1. gateway JSON      {"style": {... "sizes": [...]}}      (fast HTTP engine)
  2. window.__myx      {"pdpData": {... "sizes": [...]}}    (HTML page / browser engine)
  3. a bare pdpData dict                                    (page.evaluate in browser)

Everything is read defensively - a missing key must never crash a 1-lakh-style run.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .models import Item, SizeInfo, Status, StyleResult, norm_size

_MYX = re.compile(r"window\.__myx\s*=\s*")
_decoder = json.JSONDecoder()


def _sizes_from(raw: Any) -> list[SizeInfo]:
    out: list[SizeInfo] = []
    for s in raw or []:
        if not isinstance(s, dict):
            continue
        label = str(s.get("label") or s.get("size") or "").strip()
        if not label:
            continue
        counts = [
            d.get("availableCount")
            for d in (s.get("sizeSellerData") or [])
            if isinstance(d, dict) and isinstance(d.get("availableCount"), (int, float))
        ]
        qty = int(sum(counts)) if counts else None
        avail = s.get("available")
        if avail is None:
            avail = bool(qty and qty > 0)
        out.append(SizeInfo(label=label, available=bool(avail), qty=qty))
    return out


def build_result(item: Item, name: str, brand: str, sizes: list[SizeInfo]) -> StyleResult:
    """Apply the watch-list and decide OK / PARTIAL_OOS / ALL_OOS."""
    by_label = {norm_size(s.label): s for s in sizes}
    watch = [norm_size(w) for w in item.watch_sizes if str(w).strip()]

    if watch:
        relevant = list(dict.fromkeys(watch))
        # a watched size that is not even listed on the page counts as out of stock
        oos = [w for w in relevant if w not in by_label or not by_label[w].available]
        ins = [w for w in relevant if w in by_label and by_label[w].available]
    else:
        relevant = list(by_label)
        oos = [k for k in relevant if not by_label[k].available]
        ins = [k for k in relevant if by_label[k].available]

    if not relevant:  # product page exists but exposes no sizes at all
        status, oos = Status.ALL_OOS, ["ALL"]
    elif not oos:
        status = Status.OK
    elif not ins:
        status = Status.ALL_OOS
    else:
        status = Status.PARTIAL_OOS

    return StyleResult(
        style_id=item.style_id,
        status=status,
        name=name,
        brand=brand,
        sizes=sizes,
        oos_sizes=oos,
        in_sizes=ins,
    )


def _from_style_dict(item: Item, d: dict) -> StyleResult:
    brand = d.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name", "")
    return build_result(item, str(d.get("name") or ""), str(brand or ""), _sizes_from(d.get("sizes")))


def parse_pdp(item: Item, pdp: dict) -> StyleResult | None:
    if not isinstance(pdp, dict) or ("sizes" not in pdp and "id" not in pdp):
        return None
    return _from_style_dict(item, pdp)


def parse_gateway_json(item: Item, payload: Any) -> StyleResult | None:
    if not isinstance(payload, dict):
        return None
    d = payload.get("style") or payload.get("pdpData") or payload
    return parse_pdp(item, d)


def parse_html(item: Item, html: str) -> StyleResult | None:
    m = _MYX.search(html)
    if not m:
        return None
    try:
        obj, _ = _decoder.raw_decode(html, m.end())
    except ValueError:
        return None
    return parse_pdp(item, obj.get("pdpData") or {})
