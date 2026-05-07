from __future__ import annotations

from typing import Any, NamedTuple

__all__ = ["guesstimate_resolution", "infer_resolution", "ResolutionResult"]


from typing import NamedTuple

# reference bases: (label, base_height, canonical_width)
_BASES: tuple[tuple[str, int, int], ...] = (
    ("480", 480, 854),  # 16:9 SD
    ("480", 480, 640),  # 4:3  SD
    ("576", 576, 1024),  # 16:9 PAL
    ("576", 576, 768),  # 4:3  PAL
    ("720", 720, 1280),  # 16:9 HD
    ("720", 720, 960),  # 4:3  HD
    ("1080", 1080, 1920),  # 16:9 Full HD
    ("1080", 1080, 1440),  # 4:3  Full HD
    ("1080", 1080, 2048),  # DCI 2K
    ("1080", 1080, 2560),  # 21:9 ultrawide FHD
    ("1440", 1440, 2560),  # 16:9 QHD
    ("1440", 1440, 1920),  # 4:3  QHD
    ("1440", 1440, 3440),  # 21:9 ultrawide QHD
    ("1440", 1440, 1366),  # common laptop display
    ("2160", 2160, 3840),  # 16:9 4K UHD
    ("2160", 2160, 2880),  # 4:3  4K
    ("2160", 2160, 4096),  # DCI 4K
    ("4320", 4320, 7680),  # 16:9 8K
    ("4320", 4320, 5760),  # 4:3  8K
    ("8640", 8640, 15360),  # 16:9 16K
    ("8640", 8640, 11520),  # 4:3  16K
)

_ABS_TOL = 8  # pixel slack for rounding errors
_REL_TOL = 0.03  # 3% relative tolerance
_MIN_CROP_FRAC = 0.78  # min fraction of canonical dim to accept as crop


class ResolutionResult(NamedTuple):
    width: int
    height: int
    base_label: str  # "1080"
    base_height: int  # 1080

    @property
    def label(self) -> str:
        return f"{self.base_label}p"


def _infer_one(w: int, h: int) -> tuple[str, int, float]:
    """Classify a single orientation. Returns (label, base_height, best_err)."""
    if h == 0:
        return ("480", 480, float("inf"))

    obs_ar = w / h
    best_err = float("inf")
    best_label, best_bh = "480", 480

    for label, bh, bw in _BASES:
        tol_h = max(_ABS_TOL, _REL_TOL * bh)
        tol_w = max(_ABS_TOL, _REL_TOL * bw)
        base_ar = bw / bh
        dw = abs(w - bw) / bw
        dh = abs(h - bh) / bh
        ar_err = abs(obs_ar - base_ar) / base_ar

        # full frame (height weighted 2x as it defines the tier)
        err = (dw + 2.0 * dh) / 3.0 + 0.20 * ar_err
        if err < best_err:
            best_err, best_label, best_bh = err, label, bh
            if err < 0.01:
                return (label, bh, err)

        # letterbox (full width, cropped height)
        if w >= _MIN_CROP_FRAC * bw and h <= bh + tol_h:
            deficit = max(0.0, (bh - h) / bh)
            err = dw + 0.5 * deficit + 0.20 * ar_err
            if err < best_err:
                best_err, best_label, best_bh = err, label, bh
                if err < 0.01:
                    return (label, bh, err)

        # pillarbox (full height, cropped width)
        if h >= _MIN_CROP_FRAC * bh and w <= bw + tol_w:
            deficit = max(0.0, (bw - w) / bw)
            err = dh + 0.5 * deficit + 0.20 * ar_err
            if err < best_err:
                best_err, best_label, best_bh = err, label, bh
                if err < 0.01:
                    return (label, bh, err)

    return (best_label, best_bh, best_err)


def infer_resolution(width: int, height: int) -> ResolutionResult:
    """Infer commercial resolution tier from pixel dimensions."""
    l_label, l_bh, l_err = _infer_one(width, height)
    p_label, p_bh, p_err = _infer_one(height, width)
    label, bh = (l_label, l_bh) if l_err <= p_err else (p_label, p_bh)
    return ResolutionResult(
        width=width, height=height, base_label=label, base_height=bh
    )


def guesstimate_resolution(
    width: Any,
    height: Any,
    unknown_label: str | None = "Unknown",
    raise_on_error: bool = False,
) -> str | None:
    """Return the standard resolution label, e.g. ``'1080p'``.

    If fails it'll return ``unknown_label``.
    """
    try:
        width_int = int(width)
        height_int = int(height)
        return infer_resolution(width_int, height_int).label
    except Exception:
        if raise_on_error:
            raise
        return unknown_label
