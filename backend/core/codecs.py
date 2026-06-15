import re

from backend.user_types import AudioCodecFamily, VideoCodecFamily

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalize_token(codec: str) -> tuple[str, str]:
    """Return lowercased and punctuation collapsed forms to help with matching."""
    lowered = codec.strip().lower()
    collapsed = _NON_ALNUM.sub("", lowered)
    return lowered, collapsed


def normalize_video_codec_family(codec: str | None) -> VideoCodecFamily | None:
    """Map raw codec strings into stable video codec families for filtering/UI."""
    if not codec or not codec.strip():
        return None

    lowered, collapsed = _normalize_token(codec)

    # H.265 / HEVC
    if (
        re.search(r"\b(?:hevc|h[.\s_-]?265|x265|hvc1|hev1)\b", lowered)
        or collapsed.startswith("h265")
        or collapsed.startswith("hevc")
    ):
        return "h265"

    # H.266 / VVC
    if re.search(
        r"\b(?:h[.\s_-]?266|vvc|hvc2|hev2)\b", lowered
    ) or collapsed.startswith("h266"):
        return "h266"

    # H.264 / AVC
    if (
        re.search(r"\b(?:h[.\s_-]?264|x264|avc1?|davc)\b", lowered)
        or collapsed.startswith("h264")
        or collapsed.startswith("avc")
    ):
        return "h264"

    if re.search(r"\b(?:av1|av01)\b", lowered) or collapsed.startswith("av1"):
        return "av1"

    if re.search(r"\bvp9\b", lowered) or "vp9" in collapsed:
        return "vp9"

    if re.search(r"\bvp8\b", lowered) or "vp8" in collapsed:
        return "vp8"

    if re.search(r"\b(?:xvid|divx)\b", lowered) or "xvid" in collapsed:
        return "xvid"

    if re.search(r"\bvc[-\s_.]?1\b", lowered) or collapsed.startswith("vc1"):
        return "vc1"

    if re.search(r"\bmpeg[-\s_.]?4\b", lowered) or collapsed.startswith("mpeg4"):
        return "mpeg4"

    if re.search(r"\bmpeg[-\s_.]?2\b", lowered) or collapsed.startswith("mpeg2"):
        return "mpeg2"

    if re.search(r"\bmpeg[-\s_.]?1\b", lowered) or collapsed.startswith("mpeg1"):
        return "mpeg1"

    if re.search(r"\bh[.\s_-]?263\b", lowered) or collapsed.startswith("h263"):
        return "h263"

    if re.search(r"\bprores\b", lowered) or "prores" in collapsed:
        return "prores"

    if re.search(r"\btheora\b", lowered) or "theora" in collapsed:
        return "theora"

    if re.search(r"\b(?:wmv|windows media video)\b", lowered) or collapsed.startswith(
        "wmv"
    ):
        return "wmv"

    return "other"


def normalize_audio_codec_family(codec: str | None) -> AudioCodecFamily | None:
    """Map raw codec strings into stable audio codec families for filtering/UI."""
    if not codec or not codec.strip():
        return None

    lowered, collapsed = _normalize_token(codec)

    # dolby digital plus / E-AC-3 before AC-3 to avoid false matches
    if (
        re.search(
            r"\b(?:e[-\s_.]?ac[-\s_.]?3|ec[-\s_.]?3|dd\+|dolby digital plus)\b",
            lowered,
        )
        or collapsed.startswith("eac3")
        or collapsed.startswith("ec3")
    ):
        return "eac3"

    if re.search(r"\b(?:ac[-\s_.]?3|dolby digital)\b", lowered) or collapsed.startswith(
        "ac3"
    ):
        return "ac3"

    if re.search(r"\b(?:ac[-\s_.]?4)\b", lowered) or collapsed.startswith("ac4"):
        return "ac4"

    if (
        re.search(r"\b(?:true[-\s_.]?hd|mlp(?:fba)?)\b", lowered)
        or "truehd" in collapsed
    ):
        return "truehd"

    # dts-hd and dts:x before plain dts
    if (
        re.search(
            r"\b(?:dts[-\s_.]?(?:hd|ma|hra|x)|dca[-\s_.]?(?:ma|hra|x))\b", lowered
        )
        or "dtshd" in collapsed
    ):
        return "dtshd"

    if re.search(r"\b(?:dts|dca)\b", lowered) or collapsed.startswith("dts"):
        return "dts"

    if re.search(r"\b(?:aac|mp4a|he[-\s_.]?aac)\b", lowered) or collapsed.startswith(
        "aac"
    ):
        return "aac"

    if re.search(r"\b(?:opus|a_opus)\b", lowered) or "opus" in collapsed:
        return "opus"

    if re.search(r"\b(?:vorbis|a_vorbis)\b", lowered) or "vorbis" in collapsed:
        return "vorbis"

    if re.search(r"\bflac\b", lowered) or collapsed.startswith("flac"):
        return "flac"

    if re.search(r"\balac\b", lowered) or collapsed.startswith("alac"):
        return "alac"

    if re.search(r"\bmp3\b", lowered) or "mpeglayer3" in collapsed:
        return "mp3"

    if re.search(r"\bmp2\b", lowered) or "mpeglayer2" in collapsed:
        return "mp2"

    if re.search(r"\b(?:pcm|lpcm)\b", lowered) or collapsed.startswith("pcm"):
        return "pcm"

    if re.search(r"\bwma\b", lowered) or collapsed.startswith("wma"):
        return "wma"

    if re.search(r"\bamr\b", lowered) or collapsed.startswith("amr"):
        return "amr"

    return "other"
