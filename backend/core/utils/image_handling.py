import uuid
from io import BytesIO
from os import PathLike
from pathlib import Path

from core.logger import LOG
from core.settings import settings
from PIL import Image


def delete_avatar(image_path: PathLike[str]) -> None:
    """Remove an avatar image file from the filesystem.

    Args:
        image_path: Name of the image file with suffix.
    """
    try:
        old = settings.avatars_dir / image_path
        old.unlink(missing_ok=True)
    except Exception as e:
        LOG.error(f"Error deleting image {image_path}: {e}")
        raise


def save_picture_from_bytes(
    image_bytes: bytes,
    original_filename: str,
    del_old_path: PathLike[str] | None = None,
) -> str:
    """Save user's avatar from bytes data.

    Args:
        image_bytes: Raw image bytes
        original_filename: Original filename to extract extension
        del_old_path: Optional path to old avatar to delete

    Returns:
        The new avatar filename
    """
    # validate and normalize file extension
    ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    ext = Path(original_filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        ext = ".png"  # default to PNG for safety

    # create new picture path
    picture_fn = f"{uuid.uuid4().hex}{ext}"
    picture_path = settings.avatars_dir / picture_fn

    try:
        # protect against decompression bombs
        Image.MAX_IMAGE_PIXELS = 25_000_000  # ~5000x5000

        # open image from bytes and verify it's a real image
        i = Image.open(BytesIO(image_bytes))
        i.verify()

        # re-open after verify() (verify closes the file)
        i = Image.open(BytesIO(image_bytes))

        # check if the image is a GIF, if so, save without modifications
        if i.format == "GIF":
            i.save(picture_path, format="GIF", optimize=False, save_all=True)
        else:
            # convert the image to RGBA mode to preserve transparency for formats like PNG
            i = i.convert("RGBA")
            # resize the image if it's larger than 500x500
            if i.width > 500 or i.height > 500:
                i.thumbnail((500, 500), resample=Image.Resampling.LANCZOS)
            # save the converted image to the new picture path
            i.save(picture_path, format="PNG", quality=95)

        # delete the old picture if provided
        if del_old_path:
            delete_avatar(del_old_path)

        return picture_fn
    except Exception as e:
        LOG.error(f"Error saving picture: {e}")
        # clean up the new file if it was created but something failed
        picture_path.unlink(missing_ok=True)
        raise
