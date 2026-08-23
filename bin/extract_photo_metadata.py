#!/usr/bin/env python3
"""Extract photo GPS, place descriptions, and capture times into JSON.

The script never modifies source images. GPS and EXIF timestamps are read with
Pillow. Optional reverse geocoding uses the Google Geocoding API, with the API
key read from an environment variable and responses cached on disk. Optional
timezone lookup uses timezonefinder and Python's standard-library zoneinfo.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from PIL import ExifTags, Image, UnidentifiedImageError
except ImportError as exc:  # pragma: no cover - exercised only without Pillow
    raise SystemExit(
        "Pillow is required. Install it with: python -m pip install Pillow"
    ) from exc


IMAGE_SUFFIXES = {
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

GPS_INFO_TAG = 34853
DATETIME_TAGS = (
    (36867, "DateTimeOriginal"),
    (36868, "DateTimeDigitized"),
    (306, "DateTime"),
)
OFFSET_TIME_TAGS = (36881, 36882, 36880)
SUBSECOND_TAGS = (37521, 37522, 37520)


def enable_heif_support() -> bool:
    try:
        import pillow_heif
    except ImportError:
        return False

    pillow_heif.register_heif_opener()
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract GPS, place descriptions, and capture times from photos.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python bin/extract_photo_metadata.py D:\\Photos
  python bin/extract_photo_metadata.py D:\\Photos -o gallery-metadata.json

  $env:GOOGLE_MAPS_API_KEY = "your-key"
  python bin/extract_photo_metadata.py D:\\Photos --geocoder google --language zh-CN

Optional packages:
  python -m pip install pillow-heif timezonefinder
""",
    )
    parser.add_argument("input_dir", type=Path, help="folder containing photos")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="JSON output path (default: <input-folder>/photo-metadata.json)",
    )
    parser.add_argument(
        "--geocoder",
        choices=("none", "google"),
        default="none",
        help="reverse geocoder used for place descriptions (default: none)",
    )
    parser.add_argument(
        "--google-api-key-env",
        default="GOOGLE_MAPS_API_KEY",
        help="environment variable containing the Google API key",
    )
    parser.add_argument(
        "--language",
        default="zh-CN",
        help="preferred geocoding result language (default: zh-CN)",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        help="geocoding cache path (default: next to the output JSON)",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=0.1,
        help="minimum delay between uncached geocoding requests (default: 0.1s)",
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="process only files directly inside the input folder",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing output JSON file",
    )
    return parser.parse_args()


def iter_photos(input_dir: Path, recursive: bool, heif_supported: bool) -> Iterable[Path]:
    suffixes = IMAGE_SUFFIXES | (HEIF_SUFFIXES if heif_supported else set())
    candidates = input_dir.rglob("*") if recursive else input_dir.glob("*")
    for path in sorted(candidates):
        if path.is_file() and path.suffix.lower() in suffixes:
            yield path


def ratio_to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"invalid EXIF rational value: {value!r}") from exc


def dms_to_decimal(values: Any, reference: Any) -> float:
    if not values or len(values) != 3:
        raise ValueError("invalid GPS degree/minute/second tuple")

    degrees = ratio_to_float(values[0])
    minutes = ratio_to_float(values[1])
    seconds = ratio_to_float(values[2])
    decimal = degrees + minutes / 60 + seconds / 3600

    if isinstance(reference, bytes):
        reference = reference.decode("ascii", errors="ignore")
    if str(reference).upper() in {"S", "W"}:
        decimal = -decimal
    return round(decimal, 7)


def read_gps(exif: Any) -> dict[str, Any] | None:
    try:
        gps = exif.get_ifd(GPS_INFO_TAG)
    except (AttributeError, KeyError, TypeError):
        gps = exif.get(GPS_INFO_TAG)

    if not gps:
        return None

    # GPS IFD numeric tags: 1/2 latitude ref/value, 3/4 longitude ref/value.
    try:
        latitude = dms_to_decimal(gps[2], gps[1])
        longitude = dms_to_decimal(gps[4], gps[3])
    except (KeyError, TypeError, ValueError):
        return None

    result: dict[str, Any] = {
        "latitude": latitude,
        "longitude": longitude,
    }

    if 6 in gps:
        try:
            altitude = ratio_to_float(gps[6])
            if gps.get(5, 0) == 1:
                altitude = -altitude
            result["altitude_m"] = round(altitude, 2)
        except (TypeError, ValueError):
            pass

    return result


def first_exif_text(exif: Any, tags: Iterable[int]) -> str | None:
    for tag in tags:
        value = exif.get(tag)
        if value is None:
            continue
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        value = str(value).strip().strip("\x00")
        if value:
            return value
    return None


def parse_offset(value: str | None) -> timezone | None:
    if not value or len(value) != 6 or value[0] not in {"+", "-"} or value[3] != ":":
        return None
    try:
        hours = int(value[1:3])
        minutes = int(value[4:6])
    except ValueError:
        return None
    if hours > 23 or minutes > 59:
        return None

    seconds = (hours * 60 + minutes) * 60
    if value[0] == "-":
        seconds = -seconds
    from datetime import timedelta

    return timezone(timedelta(seconds=seconds))


def load_timezone_finder() -> Any | None:
    try:
        from timezonefinder import TimezoneFinder
    except ImportError:
        return None
    return TimezoneFinder(in_memory=True)


def capture_time_metadata(
    exif: Any,
    gps: dict[str, Any] | None,
    timezone_finder: Any | None,
) -> dict[str, Any] | None:
    raw_time = None
    source_tag = None
    for tag, name in DATETIME_TAGS:
        value = first_exif_text(exif, (tag,))
        if value:
            raw_time = value
            source_tag = name
            break
    if not raw_time:
        return None

    try:
        captured = datetime.strptime(raw_time, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return {"raw": raw_time, "source": source_tag, "parse_error": True}

    subsecond = first_exif_text(exif, SUBSECOND_TAGS)
    if subsecond and subsecond.isdigit():
        captured = captured.replace(microsecond=int((subsecond + "000000")[:6]))

    offset_text = first_exif_text(exif, OFFSET_TIME_TAGS)
    tzinfo = parse_offset(offset_text)
    timezone_name = None
    timezone_source = None

    if tzinfo is not None:
        timezone_source = "EXIF OffsetTime"
    elif gps and timezone_finder is not None:
        timezone_name = timezone_finder.timezone_at(
            lat=gps["latitude"], lng=gps["longitude"]
        )
        if timezone_name:
            try:
                tzinfo = ZoneInfo(timezone_name)
                timezone_source = "GPS"
            except ZoneInfoNotFoundError:
                timezone_name = None

    result: dict[str, Any] = {
        "raw": raw_time,
        "source": source_tag,
        "local": captured.isoformat(),
    }
    if offset_text:
        result["exif_utc_offset"] = offset_text
    if timezone_name:
        result["timezone"] = timezone_name
    if timezone_source:
        result["timezone_source"] = timezone_source
    if tzinfo is not None:
        aware = captured.replace(tzinfo=tzinfo)
        result["local"] = aware.isoformat()
        result["utc"] = aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    return result


def load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: ignoring invalid cache {path}: {exc}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def cache_key(latitude: float, longitude: float, language: str) -> str:
    return f"{latitude:.6f},{longitude:.6f}|{language}"


def address_component(components: list[dict[str, Any]], *types: str) -> str | None:
    for wanted in types:
        for component in components:
            if wanted in component.get("types", []):
                return component.get("long_name")
    return None


def normalize_google_location(payload: dict[str, Any]) -> dict[str, Any]:
    status = payload.get("status")
    if status != "OK" or not payload.get("results"):
        return {
            "status": str(status or "ERROR").lower(),
            "error": payload.get("error_message"),
            "provider": "google",
        }

    first = payload["results"][0]
    components = first.get("address_components", [])
    place = address_component(
        components,
        "point_of_interest",
        "establishment",
        "natural_feature",
        "premise",
        "route",
        "neighborhood",
        "sublocality_level_1",
    )
    city = address_component(
        components,
        "locality",
        "postal_town",
        "administrative_area_level_3",
    )
    country = address_component(components, "country")
    concise_parts: list[str] = []
    for value in (place, city, country):
        if value and value not in concise_parts:
            concise_parts.append(value)

    return {
        "status": "ok",
        "provider": "google",
        "label": "，".join(concise_parts) or first.get("formatted_address"),
    }


def concise_time(captured_at: dict[str, Any] | None) -> str:
    if not captured_at:
        return "未知时间"
    value = captured_at.get("local") or captured_at.get("raw")
    if not value:
        return "未知时间"
    value = str(value).replace("T", " ")
    return value[:16]


def google_reverse_geocode(
    latitude: float,
    longitude: float,
    *,
    api_key: str,
    language: str,
    user_agent: str,
    timeout: float = 20,
) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {
            "latlng": f"{latitude:.7f},{longitude:.7f}",
            "language": language,
            "key": api_key,
        }
    )
    request = urllib.request.Request(
        f"https://maps.googleapis.com/maps/api/geocode/json?{query}",
        headers={"User-Agent": user_agent, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"status": "request_error", "provider": "google", "error": str(exc)}
    return normalize_google_location(payload)


def read_photo(
    path: Path,
    input_dir: Path,
    timezone_finder: Any | None,
) -> dict[str, Any]:
    with Image.open(path) as image:
        exif = image.getexif()
        gps = read_gps(exif) if exif else None
        captured_at = capture_time_metadata(exif, gps, timezone_finder) if exif else None
        record: dict[str, Any] = {
            "file": path.relative_to(input_dir).as_posix(),
            "width": image.width,
            "height": image.height,
            "format": image.format,
            "bytes": path.stat().st_size,
            "gps": gps,
            "location": None,
            "captured_at": captured_at,
        }
        return record


def main() -> int:
    args = parse_args()
    input_dir = args.input_dir.expanduser().resolve()
    if not input_dir.is_dir():
        print(f"error: input folder does not exist: {input_dir}", file=sys.stderr)
        return 2
    if args.request_delay < 0:
        print("error: --request-delay cannot be negative", file=sys.stderr)
        return 2

    output = (
        args.output.expanduser().resolve()
        if args.output
        else input_dir / "photo-metadata.json"
    )
    if output.exists() and not args.overwrite:
        print(f"error: output already exists (use --overwrite): {output}", file=sys.stderr)
        return 2

    cache_path = (
        args.cache.expanduser().resolve()
        if args.cache
        else output.with_name(f"{output.stem}.geocode-cache.json")
    )
    geocode_cache = load_json_object(cache_path)

    google_api_key = None
    if args.geocoder == "google":
        google_api_key = os.environ.get(args.google_api_key_env)
        if not google_api_key:
            print(
                f"error: --geocoder google requires environment variable "
                f"{args.google_api_key_env}",
                file=sys.stderr,
            )
            return 2

    heif_supported = enable_heif_support()
    timezone_finder = load_timezone_finder()
    if timezone_finder is None:
        print(
            "note: install timezonefinder to infer IANA timezones from GPS: "
            "python -m pip install timezonefinder",
            file=sys.stderr,
        )

    photos = list(iter_photos(input_dir, not args.no_recursive, heif_supported))
    records: list[dict[str, str]] = []
    gps_count = 0
    geocoded_count = 0
    failed_count = 0
    last_request_at = 0.0

    for path in photos:
        relative = path.relative_to(input_dir).as_posix()
        try:
            record = read_photo(path, input_dir, timezone_finder)
        except (OSError, ValueError, UnidentifiedImageError) as exc:
            print(f"FAIL  {relative}: {exc}", file=sys.stderr)
            records.append({"file": relative, "caption": "未知时间 - 无法读取图片"})
            failed_count += 1
            continue

        gps = record["gps"]
        if gps:
            gps_count += 1
            if args.geocoder == "google" and google_api_key:
                key = cache_key(gps["latitude"], gps["longitude"], args.language)
                location = geocode_cache.get(key)
                if location is None:
                    wait_for = args.request_delay - (time.monotonic() - last_request_at)
                    if wait_for > 0:
                        time.sleep(wait_for)
                    location = google_reverse_geocode(
                        gps["latitude"],
                        gps["longitude"],
                        api_key=google_api_key,
                        language=args.language,
                        user_agent="heyh31-photo-metadata/1.0",
                    )
                    last_request_at = time.monotonic()
                    geocode_cache[key] = location
                    write_json(cache_path, geocode_cache)
                record["location"] = location
                if location.get("status") == "ok":
                    geocoded_count += 1

        location_label = (record.get("location") or {}).get("label") or "未知地点"
        caption = f"{concise_time(record.get('captured_at'))} - {location_label}"
        print(f"OK    {relative}: {caption}")
        records.append({"file": relative, "caption": caption})

    write_json(output, records)

    print(
        f"\nWrote {output}: photos={len(records)}, gps={gps_count}, "
        f"geocoded={geocoded_count}, failed={failed_count}"
    )
    if args.geocoder == "google":
        print(f"Geocoding cache: {cache_path}")
    return 1 if failed_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
