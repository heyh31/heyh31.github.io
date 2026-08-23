#!/usr/bin/env python3
"""Generate Jekyll gallery data by listing images in Cloudflare R2.

Credentials are read only from environment variables:

  R2_ACCOUNT_ID
  R2_ACCESS_KEY_ID
  R2_SECRET_ACCESS_KEY

Bucket and URL settings can be supplied either as command-line arguments or
environment variables. Run ``python bin/sync_r2_gallery.py --help`` for details.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlparse


IMAGE_SUFFIXES = {
    ".avif",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".webp",
}

REQUIRED_ENVIRONMENT = (
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
)


def environment_default(name: str, fallback: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else fallback


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List gallery images in Cloudflare R2 and generate _data/gallery.yml.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""PowerShell example:
  $env:R2_ACCOUNT_ID = "your-account-id"
  $env:R2_ACCESS_KEY_ID = "your-read-only-access-key-id"
  $env:R2_SECRET_ACCESS_KEY = "your-read-only-secret-access-key"
  $env:R2_BUCKET = "your-bucket"
  $env:R2_PUBLIC_BASE_URL = "https://photos.example.com"
  $env:R2_IMAGE_RESIZER_BASE_URL = "https://example.com"
  python bin/sync_r2_gallery.py

The generated file is ordered newest upload first. No image is downloaded.
""",
    )
    parser.add_argument(
        "--bucket",
        default=environment_default("R2_BUCKET"),
        help="R2 bucket name (or R2_BUCKET)",
    )
    parser.add_argument(
        "--prefix",
        default=environment_default("R2_PREFIX", "gallery/"),
        help="object-key prefix to scan (default: R2_PREFIX or gallery/)",
    )
    parser.add_argument(
        "--public-base-url",
        default=environment_default("R2_PUBLIC_BASE_URL"),
        help="public R2 custom-domain URL (or R2_PUBLIC_BASE_URL)",
    )
    parser.add_argument(
        "--image-resizer-base-url",
        default=environment_default("R2_IMAGE_RESIZER_BASE_URL"),
        help=(
            "Cloudflare zone URL used for /cdn-cgi/image (or "
            "R2_IMAGE_RESIZER_BASE_URL); omit to use original images as thumbnails"
        ),
    )
    parser.add_argument(
        "--thumbnail-options",
        default=environment_default(
            "R2_THUMBNAIL_OPTIONS", "width=720,quality=82,format=auto"
        ),
        help="Cloudflare image transformation options",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("_data/gallery.yml"),
        help="generated Jekyll data file (default: _data/gallery.yml)",
    )
    parser.add_argument(
        "--sort",
        choices=("newest", "key-asc", "key-desc"),
        default="newest",
        help="gallery order (default: newest)",
    )
    parser.add_argument(
        "--skip-if-unconfigured",
        action="store_true",
        help="exit successfully without changing output when configuration is missing",
    )
    return parser.parse_args()


def valid_base_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def missing_configuration(args: argparse.Namespace) -> list[str]:
    missing = [name for name in REQUIRED_ENVIRONMENT if not environment_default(name)]
    if not args.bucket:
        missing.append("R2_BUCKET/--bucket")
    if not args.public_base_url:
        missing.append("R2_PUBLIC_BASE_URL/--public-base-url")
    return missing


def normalize_prefix(prefix: str) -> str:
    normalized = prefix.strip().replace("\\", "/").lstrip("/")
    if normalized and not normalized.endswith("/"):
        normalized += "/"
    return normalized


def create_s3_client() -> Any:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - depends on local environment
        raise RuntimeError(
            "boto3 is required; install it with: python -m pip install boto3"
        ) from exc

    account_id = environment_default("R2_ACCOUNT_ID")
    access_key_id = environment_default("R2_ACCESS_KEY_ID")
    secret_access_key = environment_default("R2_SECRET_ACCESS_KEY")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 5, "mode": "standard"},
        ),
    )


def list_images(client: Any, bucket: str, prefix: str) -> list[dict[str, Any]]:
    paginator = client.get_paginator("list_objects_v2")
    images: list[dict[str, Any]] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            key = str(item.get("Key", ""))
            if not key or key.endswith("/"):
                continue
            if PurePosixPath(key).suffix.lower() not in IMAGE_SUFFIXES:
                continue
            images.append(item)
    return images


def sort_images(images: list[dict[str, Any]], order: str) -> None:
    if order == "key-asc":
        images.sort(key=lambda item: str(item["Key"]).casefold())
    elif order == "key-desc":
        images.sort(key=lambda item: str(item["Key"]).casefold(), reverse=True)
    else:
        def modified_timestamp(item: dict[str, Any]) -> float:
            value = item.get("LastModified")
            return value.timestamp() if isinstance(value, datetime) else 0.0

        images.sort(
            key=lambda item: (modified_timestamp(item), str(item["Key"])),
            reverse=True,
        )


def object_url(base_url: str, key: str) -> str:
    return f"{base_url.rstrip('/')}/{quote(key, safe='/')}"


def thumbnail_url(
    full_url: str,
    image_resizer_base_url: str | None,
    options: str,
) -> str:
    if not image_resizer_base_url:
        return full_url
    return (
        f"{image_resizer_base_url.rstrip('/')}/cdn-cgi/image/"
        f"{options.strip('/')}/{full_url}"
    )


def alt_from_key(key: str) -> str:
    stem = PurePosixPath(key).stem.replace("_", " ").replace("-", " ")
    return " ".join(stem.split()) or "Photo"


def make_records(
    images: list[dict[str, Any]],
    public_base_url: str,
    image_resizer_base_url: str | None,
    thumbnail_options: str,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for item in images:
        key = str(item["Key"])
        full = object_url(public_base_url, key)
        records.append(
            {
                "full": full,
                "thumb": thumbnail_url(
                    full, image_resizer_base_url, thumbnail_options
                ),
                "alt": alt_from_key(key),
            }
        )
    return records


def yaml_scalar(value: str) -> str:
    # A JSON string is also a valid quoted YAML scalar.
    return json.dumps(value, ensure_ascii=False)


def render_yaml(records: list[dict[str, str]]) -> str:
    header = "# Generated by bin/sync_r2_gallery.py. Do not edit manually.\n"
    if not records:
        return f"{header}[]\n"

    lines = [header.rstrip("\n")]
    for record in records:
        lines.append(f"- full: {yaml_scalar(record['full'])}")
        lines.append(f"  thumb: {yaml_scalar(record['thumb'])}")
        lines.append(f"  alt: {yaml_scalar(record['alt'])}")
    return "\n".join(lines) + "\n"


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    missing = missing_configuration(args)
    if missing:
        message = "missing R2 configuration: " + ", ".join(missing)
        if args.skip_if_unconfigured:
            print(f"Gallery sync skipped ({message}).")
            return 0
        print(f"error: {message}", file=sys.stderr)
        return 2

    assert args.public_base_url is not None
    if not valid_base_url(args.public_base_url):
        print(
            f"error: invalid public base URL: {args.public_base_url}",
            file=sys.stderr,
        )
        return 2
    if args.image_resizer_base_url and not valid_base_url(
        args.image_resizer_base_url
    ):
        print(
            f"error: invalid image resizer base URL: {args.image_resizer_base_url}",
            file=sys.stderr,
        )
        return 2

    prefix = normalize_prefix(args.prefix or "")
    try:
        client = create_s3_client()
        images = list_images(client, args.bucket, prefix)
    except Exception as exc:  # boto3 exposes several service-specific exceptions
        print(f"error: could not list R2 bucket: {exc}", file=sys.stderr)
        return 1

    sort_images(images, args.sort)
    records = make_records(
        images,
        args.public_base_url,
        args.image_resizer_base_url,
        args.thumbnail_options,
    )
    output = args.output.expanduser().resolve()
    write_atomic(output, render_yaml(records))
    print(
        f"Wrote {output} with {len(records)} image(s) "
        f"from s3://{args.bucket}/{prefix}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
