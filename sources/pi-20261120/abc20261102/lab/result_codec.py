"""Standard JSON or gzip-compressed JSON; no field or numeric transformation."""
import gzip
import io
import json
import zlib


GZIP_MAGIC = b"\x1f\x8b"
DEFAULT_MAX_UNCOMPRESSED_BYTES = 8 * 1024 * 1024


def encode_json(value, compress=False, min_compress_bytes=1024):
    raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
    if not compress or len(raw) < min_compress_bytes:
        return raw
    packed = gzip.compress(raw, compresslevel=1, mtime=0)
    return packed if len(packed) < len(raw) else raw


def decode_json(payload, max_uncompressed_bytes=DEFAULT_MAX_UNCOMPRESSED_BYTES):
    if not isinstance(max_uncompressed_bytes, int) or max_uncompressed_bytes < 1:
        raise ValueError("max_uncompressed_bytes must be a positive integer")
    if isinstance(payload, str):
        raw = payload.encode("utf-8")
    else:
        raw = bytes(payload)
    if raw.startswith(GZIP_MAGIC):
        # Limit decompressed memory use, including concatenated standard gzip members.
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as stream:
                raw = stream.read(max_uncompressed_bytes + 1)
        except (OSError, EOFError, zlib.error) as error:
            raise ValueError("Invalid gzip result JSON") from error
    if len(raw) > max_uncompressed_bytes:
        raise ValueError("Result JSON exceeds the uncompressed size limit")
    return json.loads(raw)
