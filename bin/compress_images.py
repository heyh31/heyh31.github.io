#!/usr/bin/env python3
"""Batch-convert images in a folder to optimized JPEG files.

The script preserves the input directory, mirrors its subdirectory structure in
the output directory, corrects EXIF orientation, and preserves EXIF metadata
(including GPS) by default. Pillow is required; HEIC/HEIF input additionally
needs pillow-heif.
"""

from __future__ import annotations

import argparse
import re
import sys
from io import BytesIO
from pathlib import Path
from typing import Iterable

try:
    from PIL import Image, ImageColor, ImageOps, UnidentifiedImageError
except ImportError as exc:  # pragma: no cover - exercised only without Pillow
    raise SystemExit(
        "Pillow is required. Install it with: python -m pip install Pillow"
    ) from exc


STANDARD_SUFFIXES = {
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}
HEIF_SUFFIXES = {".heic", ".heif"}


def enable_heif_support() -> bool:
    """Register Pillow's optional HEIF opener when pillow-heif is installed."""
    try:
        import pillow_heif
    except ImportError:
        return False

    pillow_heif.register_heif_opener()
    return True


def quality_value(value: str) -> int:
    quality = int(value)
    if not 1 <= quality <= 95:
        raise argparse.ArgumentTypeError("quality must be between 1 and 95")
    return quality


def non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("value must be zero or greater")
    return number


def non_negative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("value must be zero or greater")
    return number


def background_color(value: str) -> tuple[int, int, int]:
    try:
        color = ImageColor.getrgb(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "background must be a CSS color such as '#ffffff' or 'black'"
        ) from exc

    if len(color) == 4:
        return color[:3]
    return color


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recursively convert a folder of images to optimized JPEGs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python bin/compress_images.py D:\\Photos
  python bin/compress_images.py D:\\Photos -o D:\\Photos_web --quality 82 --max-edge 2560
  python bin/compress_images.py D:\\Photos --quality 95 --min-quality 85 --max-size-mb 10
  python bin/compress_images.py D:\\Photos --rename-sequential --strip-exif
  python bin/compress_images.py D:\\Photos --dry-run
""",
    )
    parser.add_argument("input_dir", type=Path, help="folder containing source images")
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        help="output folder (default: <input-folder>_jpeg next to the input folder)",
    )
    parser.add_argument(
        "-q",
        "--quality",
        type=quality_value,
        default=82,
        help="JPEG quality from 1 to 95 (default: 82)",
    )
    parser.add_argument(
        "--max-edge",
        type=non_negative_int,
        default=0,
        help="resize so the longest edge is at most this many pixels; 0 keeps size",
    )
    parser.add_argument(
        "--max-size-mb",
        type=non_negative_float,
        default=0,
        help=(
            "keep each JPEG at or below this size in MiB; 0 disables the limit "
            "(default: 0)"
        ),
    )
    parser.add_argument(
        "--min-quality",
        type=quality_value,
        default=80,
        help=(
            "lowest JPEG quality allowed before target-size mode reduces dimensions "
            "(default: 80)"
        ),
    )
    parser.add_argument(
        "--background",
        type=background_color,
        default=(255, 255, 255),
        help="background used for transparent images (default: #ffffff)",
    )
    parser.add_argument(
        "--strip-exif",
        action="store_true",
        help="remove EXIF metadata; by default EXIF, including GPS, is retained",
    )
    parser.add_argument(
        "--rename-sequential",
        action="store_true",
        help=(
            "rename outputs globally as 0001.jpg, 0002.jpg, ...; continue after "
            "the highest existing number in the output folder"
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace JPEGs that already exist in the output folder",
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="process only files directly inside the input folder",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show planned conversions without creating files",
    )
    return parser.parse_args()


def is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def natural_sort_key(value: str) -> tuple[tuple[int, object], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", value)
    )


def next_sequence_number(output_dir: Path) -> tuple[int, int]:
    """Return the next numeric JPEG name and the existing padding width."""
    highest = 0
    digits = 4

    if not output_dir.is_dir():
        return 1, digits

    for path in output_dir.iterdir():
        if (
            not path.is_file()
            or path.suffix.lower() not in {".jpg", ".jpeg"}
            or not path.stem.isdigit()
        ):
            continue
        highest = max(highest, int(path.stem))
        digits = max(digits, len(path.stem))

    next_number = highest + 1
    return next_number, max(digits, len(str(next_number)))


def iter_images(
    input_dir: Path,
    output_dir: Path,
    recursive: bool,
    heif_supported: bool,
) -> Iterable[Path]:
    suffixes = STANDARD_SUFFIXES | (HEIF_SUFFIXES if heif_supported else set())
    candidates = input_dir.rglob("*") if recursive else input_dir.glob("*")

    for path in sorted(
        candidates,
        key=lambda item: natural_sort_key(item.relative_to(input_dir).as_posix()),
    ):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if is_within(resolved, output_dir):
            continue
        if path.suffix.lower() in suffixes:
            yield path


def destination_for(
    source: Path,
    input_dir: Path,
    output_dir: Path,
    reserved: set[Path],
    *,
    sequence_number: int | None = None,
    sequence_digits: int = 4,
) -> Path:
    if sequence_number is not None:
        destination = output_dir / f"{sequence_number:0{sequence_digits}d}.jpg"
        reserved.add(destination)
        return destination

    relative = source.relative_to(input_dir)
    destination = (output_dir / relative).with_suffix(".jpg")

    if destination not in reserved:
        reserved.add(destination)
        return destination

    source_type = source.suffix.lower().lstrip(".") or "image"
    candidate = destination.with_name(f"{destination.stem}-{source_type}.jpg")
    counter = 2
    while candidate in reserved:
        candidate = destination.with_name(
            f"{destination.stem}-{source_type}-{counter}.jpg"
        )
        counter += 1
    reserved.add(candidate)
    return candidate


def flatten_to_rgb(image: Image.Image, background: tuple[int, int, int]) -> Image.Image:
    has_alpha = image.mode in {"RGBA", "LA"} or "transparency" in image.info
    if not has_alpha:
        return image.convert("RGB")

    rgba = image.convert("RGBA")
    flattened = Image.new("RGB", rgba.size, background)
    flattened.paste(rgba, mask=rgba.getchannel("A"))
    return flattened


def encode_jpeg(
    image: Image.Image,
    *,
    quality: int,
    icc_profile: bytes | None,
    exif_bytes: bytes | None,
) -> bytes:
    output = BytesIO()
    save_options: dict[str, object] = {
        "format": "JPEG",
        "quality": quality,
        "optimize": True,
        "progressive": True,
        "subsampling": "4:2:0",
    }
    if icc_profile:
        save_options["icc_profile"] = icc_profile
    if exif_bytes:
        save_options["exif"] = exif_bytes
    image.save(output, **save_options)
    return output.getvalue()


def encode_with_size_limit(
    image: Image.Image,
    *,
    max_bytes: int,
    max_quality: int,
    min_quality: int,
    icc_profile: bytes | None,
    exif_bytes: bytes | None,
) -> tuple[bytes, Image.Image, int]:
    current = image

    for _ in range(12):
        low = min_quality
        high = max_quality
        best: tuple[bytes, int] | None = None
        smallest: bytes | None = None

        while low <= high:
            quality = (low + high) // 2
            encoded = encode_jpeg(
                current,
                quality=quality,
                icc_profile=icc_profile,
                exif_bytes=exif_bytes,
            )
            if quality == min_quality:
                smallest = encoded
            if len(encoded) <= max_bytes:
                best = (encoded, quality)
                low = quality + 1
            else:
                high = quality - 1

        if best is not None:
            return best[0], current, best[1]

        if smallest is None:
            smallest = encode_jpeg(
                current,
                quality=min_quality,
                icc_profile=icc_profile,
                exif_bytes=exif_bytes,
            )

        width, height = current.size
        if max(width, height) <= 640:
            break
        scale = min(0.95, (max_bytes / len(smallest)) ** 0.5 * 0.97)
        next_size = (
            max(1, round(width * scale)),
            max(1, round(height * scale)),
        )
        if next_size == current.size:
            next_size = (max(1, width - 1), max(1, height - 1))
        current = current.resize(next_size, Image.Resampling.LANCZOS)

    encoded = encode_jpeg(
        current,
        quality=min_quality,
        icc_profile=icc_profile,
        exif_bytes=exif_bytes,
    )
    if len(encoded) > max_bytes:
        raise ValueError(
            f"could not reach target size; smallest result is {format_size(len(encoded))}"
        )
    return encoded, current, min_quality


def convert_image(
    source: Path,
    destination: Path,
    *,
    quality: int,
    max_edge: int,
    max_size_bytes: int,
    min_quality: int,
    background: tuple[int, int, int],
    keep_exif: bool,
) -> tuple[int, int, bool, int, tuple[int, int], tuple[int, int]]:
    with Image.open(source) as opened:
        animated = bool(getattr(opened, "is_animated", False))
        opened.seek(0)
        icc_profile = opened.info.get("icc_profile")
        image = ImageOps.exif_transpose(opened)
        original_dimensions = image.size

        exif_bytes = None
        if keep_exif:
            exif = image.getexif()
            if exif:
                exif_bytes = exif.tobytes()

        image = flatten_to_rgb(image, background)
        if max_edge and max(image.size) > max_edge:
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)

        destination.parent.mkdir(parents=True, exist_ok=True)
        if max_size_bytes:
            encoded, image, used_quality = encode_with_size_limit(
                image,
                max_bytes=max_size_bytes,
                max_quality=quality,
                min_quality=min_quality,
                icc_profile=icc_profile,
                exif_bytes=exif_bytes,
            )
        else:
            used_quality = quality
            encoded = encode_jpeg(
                image,
                quality=used_quality,
                icc_profile=icc_profile,
                exif_bytes=exif_bytes,
            )

        temporary = destination.with_name(f"{destination.name}.tmp")
        temporary.write_bytes(encoded)
        temporary.replace(destination)

    return (
        source.stat().st_size,
        destination.stat().st_size,
        animated,
        used_quality,
        original_dimensions,
        image.size,
    )


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def main() -> int:
    args = parse_args()
    input_dir = args.input_dir.expanduser().resolve()
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else input_dir.parent / f"{input_dir.name}_jpeg"
    )

    if not input_dir.is_dir():
        print(f"error: input folder does not exist: {input_dir}", file=sys.stderr)
        return 2
    if output_dir == input_dir:
        print("error: output folder must be different from the input folder", file=sys.stderr)
        return 2
    if args.min_quality > args.quality:
        print(
            "error: --min-quality cannot be greater than --quality",
            file=sys.stderr,
        )
        return 2

    heif_supported = enable_heif_support()
    unsupported_heif = [
        path
        for path in input_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in HEIF_SUFFIXES
    ]
    if unsupported_heif and not heif_supported:
        print(
            "warning: HEIC/HEIF files will be skipped; install support with "
            "'python -m pip install pillow-heif'",
            file=sys.stderr,
        )

    sources = list(
        iter_images(
            input_dir,
            output_dir,
            recursive=not args.no_recursive,
            heif_supported=heif_supported,
        )
    )
    if not sources:
        print(f"No supported images found in: {input_dir}")
        return 0

    print(f"Input : {input_dir}")
    print(f"Output: {output_dir}")
    print(
        f"Options: quality={args.quality}, max-edge={args.max_edge or 'unchanged'}, "
        f"max-size={f'{args.max_size_mb:g} MiB' if args.max_size_mb else 'unlimited'}, "
        f"min-quality={args.min_quality}, recursive={not args.no_recursive}, "
        f"keep-exif={not args.strip_exif}, "
        f"rename-sequential={args.rename_sequential}"
    )

    converted = 0
    skipped = 0
    failed = 0
    source_total = 0
    output_total = 0
    reserved: set[Path] = set()
    sequence_start = 1
    sequence_digits = max(4, len(str(len(sources))))
    if args.rename_sequential:
        sequence_start, existing_digits = next_sequence_number(output_dir)
        last_sequence_number = sequence_start + len(sources) - 1
        sequence_digits = max(
            sequence_digits,
            existing_digits,
            len(str(last_sequence_number)),
        )
        if sequence_start > 1:
            print(
                f"Sequential naming continues at "
                f"{sequence_start:0{sequence_digits}d}.jpg"
            )

    for index, source in enumerate(sources, start=sequence_start):
        destination = destination_for(
            source,
            input_dir,
            output_dir,
            reserved,
            sequence_number=index if args.rename_sequential else None,
            sequence_digits=sequence_digits,
        )
        relative_source = source.relative_to(input_dir)
        relative_destination = destination.relative_to(output_dir)

        if destination.exists() and not args.overwrite:
            print(f"SKIP  {relative_destination} (already exists)")
            skipped += 1
            continue
        if args.dry_run:
            print(f"PLAN  {relative_source} -> {relative_destination}")
            continue

        try:
            (
                source_size,
                output_size,
                animated,
                used_quality,
                original_dimensions,
                output_dimensions,
            ) = convert_image(
                source,
                destination,
                quality=args.quality,
                max_edge=args.max_edge,
                max_size_bytes=round(args.max_size_mb * 1024 * 1024),
                min_quality=args.min_quality,
                background=args.background,
                keep_exif=not args.strip_exif,
            )
        except (OSError, ValueError, UnidentifiedImageError) as exc:
            print(f"FAIL  {relative_source}: {exc}", file=sys.stderr)
            failed += 1
            continue

        note = " (first frame only)" if animated else ""
        adjustments: list[str] = []
        if used_quality != args.quality:
            adjustments.append(f"quality={used_quality}")
        if output_dimensions != original_dimensions:
            adjustments.append(
                f"{original_dimensions[0]}x{original_dimensions[1]}"
                f"->{output_dimensions[0]}x{output_dimensions[1]}"
            )
        if adjustments:
            note += f" ({', '.join(adjustments)})"
        print(
            f"OK    {relative_source} -> {relative_destination}: "
            f"{format_size(source_size)} -> {format_size(output_size)}{note}"
        )
        converted += 1
        source_total += source_size
        output_total += output_size

    if args.dry_run:
        print(f"\nDry run complete: {len(sources)} image(s) found; no files created.")
        return 0

    saved = source_total - output_total
    saved_percent = (saved / source_total * 100) if source_total else 0
    print(
        f"\nDone: converted={converted}, skipped={skipped}, failed={failed}, "
        f"size={format_size(source_total)} -> {format_size(output_total)} "
        f"({saved_percent:.1f}% smaller)"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
