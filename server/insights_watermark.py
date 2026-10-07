"""Zero-width-space watermark for Video.id in Instagram captions.

Encodes a `Video.id` into a short, visually-invisible run of zero-width
Unicode characters, appended to the end of the caption when we queue
the video for posting. The phone types the caption verbatim, Instagram
stores it verbatim, and later when the insights FSM scrapes the caption
back the watermark is decoded on the VPS to get the exact `Video.id` —
no more caption-prefix guessing.

Why not something simpler:
  - `caption.contains(Video.caption.take(50))` breaks on re-posts with
    identical captions and on IG truncation.
  - Position in the grid is unstable the moment a new reel is posted.
  - Published timestamps are visible but locale-dependent and only
    unique within a day.

Design:
  - Alphabet for body: base-2, where `0 = U+200B` (ZWSP) and
    `1 = U+200C` (ZWNJ). Body never contains `U+200D` (ZWJ).
  - Delimiter: a single ZWJ (`U+200D`) marks both the start AND the
    end of the body. Because ZWJ never appears in the body alphabet,
    `ZWJ … ZWJ` is self-synchronising — no ambiguity from
    non-greedy regex overlapping trailer matches.
  - Payload: `Video.id` in binary, up to 32 bits (4.3B IDs).
  - Encoded size: 2 delimiters + N binary digits, where N ≈ log2(id+1).
    For id=42 that's 2 + 6 = 8 chars; for id=1M that's 2 + 20 = 22.

Round-trip:
  - `encode_watermark(x)` returns 3–34 zero-width chars.
  - `extract_watermark(caption)` returns `int | None`.

Known risks:
  - If a human edits the caption and deletes the trailing zero-width
    chars, the watermark is gone. We fall back to the caption-hash +
    published-label matching in `_upsert_insights_snapshot`.
  - Instagram's caption sanitizer today does NOT strip ZWSP/ZWJ/ZWNJ
    (verified via copy-paste with family emojis which rely on ZWJ).
  - Family emojis (`👨‍👩‍👧`) use ZWJ — but surrounded by emoji
    codepoints, not by our body alphabet (ZWSP/ZWNJ only). A lone ZWJ
    pair enclosing only ZWSP/ZWNJ chars is our unambiguous signature.
"""
from __future__ import annotations

import re
from typing import Final


# Alphabet.
_ZWSP: Final[str] = "​"  # U+200B — binary '0'
_ZWNJ: Final[str] = "‌"  # U+200C — binary '1'
_ZWJ: Final[str] = "‍"   # U+200D — delimiter only

_BIT_TO_CHAR: Final[tuple[str, str]] = (_ZWSP, _ZWNJ)
_CHAR_TO_BIT: Final[dict[str, int]] = {_ZWSP: 0, _ZWNJ: 1}

# Delimiter: a single ZWJ on each side of the body.
DELIM: Final[str] = _ZWJ

# Regex: `ZWJ <body of ZWSP|ZWNJ, 1..32 chars> ZWJ`.
# Because ZWJ is excluded from the body class, this is unambiguous
# — the first `ZWJ` after the opening delim that matches the closing
# delim is guaranteed to be the end of THIS body, not a nested one.
_WATERMARK_RE: Final[re.Pattern[str]] = re.compile(
    re.escape(DELIM) + r"([" + re.escape(_ZWSP + _ZWNJ) + r"]{1,32})" + re.escape(DELIM),
)

# Strip helper — removes ALL zero-width chars we care about (plus ZWJ
# for display; real family emojis won't render the same after this so
# we only apply `strip_watermark` for HASHING, not display).
_ZW_RE: Final[re.Pattern[str]] = re.compile(r"[​‌‍]+")


def encode_watermark(video_id: int) -> str:
    """Return the watermark for this video id as a zero-width string."""
    if video_id < 0:
        raise ValueError(f"video_id must be non-negative, got {video_id}")
    if video_id == 0:
        body = _ZWSP
    else:
        bits: list[str] = []
        n = video_id
        while n > 0:
            bits.append(_BIT_TO_CHAR[n & 1])
            n >>= 1
        body = "".join(reversed(bits))
    return DELIM + body + DELIM


def extract_watermark(caption: str | None) -> int | None:
    """Decode the first watermark found in `caption`.

    Returns `None` when no complete marker is present (caption is
    None/empty, no delimiters, or body longer than 32 bits).
    """
    if not caption:
        return None
    m = _WATERMARK_RE.search(caption)
    if m is None:
        return None
    body = m.group(1)
    value = 0
    for ch in body:
        bit = _CHAR_TO_BIT.get(ch)
        if bit is None:
            return None
        value = (value << 1) | bit
    return value


def strip_watermark(caption: str | None) -> str:
    """Remove any watermark AND stray zero-width chars from a caption.

    Used for:
      - computing `insights_caption_hash` (stable even after editing
        or re-stamping)
      - diffing against the previously-scraped caption
      - cleaning the caption before presenting in admin UI
    NOTE: this also strips ZWJ, so family-emoji captions will lose
    their ZWJ joiners. Consumers that need to preserve emoji rendering
    should NOT use this — it's for hashing/matching only.
    """
    if not caption:
        return ""
    return _ZW_RE.sub("", caption)


def stamp_caption(caption: str | None, video_id: int) -> str:
    """Prepend a watermark to `caption`.

    Strips any existing zero-width chars first so re-stamping (e.g.
    retry) replaces rather than stacking markers. The marker is
    **prepended** rather than appended — this matters on the insights
    bottom sheet in the reel player, where IG truncates long captions
    with a trailing "…". A trailing marker gets chopped off in that
    truncation; a leading marker always survives regardless of how
    aggressively IG truncates (the ZWSP chars render at zero width so
    the visible caption still starts with the first human-readable
    character).

    Historical note: from 2026-04-24 to 2026-04-25 the marker was
    appended. We still accept either placement on read (the regex in
    `extract_watermark` searches the full string), so videos stamped
    under the old layout keep decoding fine.
    """
    base = strip_watermark(caption or "")
    return encode_watermark(video_id) + base
