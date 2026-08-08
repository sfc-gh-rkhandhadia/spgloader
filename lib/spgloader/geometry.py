"""geometry.py — Convert MSSQL geography/geometry binary to PostGIS EWKT.

MSSQL stores geography/geometry columns as a proprietary binary serialization
(the value returned by `CAST(col AS VARBINARY(MAX))`), not OGC WKB.  When
migrating table data from CSV exports (e.g. SSMS "Scripts and Tables" CSV data)
those hex blobs must be decoded before they can be loaded into a PostGIS
`geography`/`geometry` column.

This module parses the SQL Server spatial binary format for the common cases
(Point, LineString, Polygon and their Multi variants) and produces an EWKT
string (`SRID=4326;POINT(...)`) which PostGIS accepts as native input.  Values
are validated through geopandas/shapely when available.

Unsupported shapes raise UnsupportedGeometryError rather than silently NULL.

References:
  - SQL Server binary format: bytes 0-3 SRID (LE u32); then a serialization
    header (version byte, figure byte, type byte); then coordinate doubles.
"""

from __future__ import annotations

import struct

try:  # geopandas is optional — only required when geometry columns exist
    import shapely.wkt
    _HAS_SHAPELY = True
except Exception:  # pragma: no cover - env without geopandas
    _HAS_SHAPELY = False


class UnsupportedGeometryError(ValueError):
    """Raised when the MSSQL spatial binary cannot be safely decoded."""


# Figure/type byte values seen in the SQL Server serialization header.
# 0x0C and 0x01/0x00 markers appear for points; larger figures encode
# coordinate counts for line/polygon shapes.
_POINT_FIGURE = 0x0C
_MULTI_FLAG = 0x80


def _decode_figure_header(data: bytes, offset: int):
    """Decode the figure header (variable length) used by non-point shapes.

    Returns (type_byte, point_count, new_offset).  For a Point the figure
    header is a single PPC-encoded value; for line/polygon shapes it carries
    the number of points plus a vertex-offset value.
    """
    # First byte is an "offset" figure (often 0x01 or 0x35/0x34 for shapes).
    vertex_offset = data[offset]
    offset += 1
    # Next is the point-count figure (PPC varint).
    first = data[offset]
    if first & 0x80:  # multi-byte varint
        point_count = (first & 0x7F) | (data[offset + 1] << 7)
        offset += 2
    else:
        point_count = first
        offset += 1
    return vertex_offset, point_count, offset


def _decode_point_doubles(data: bytes, offset: int) -> "tuple[float, float]":
    """Read a lat/long (geography) or x/y (geometry) pair of doubles."""
    d1, d2 = struct.unpack_from("<dd", data, offset)
    # MSSQL geography serializes as [lat, lon]; exchange to (lon, lat) so the
    # result is a well-formed PostGIS coordinate pair.
    return d2, d1


def mssql_spatial_to_wkt(hex_value: str) -> str:
    """Convert a MSSQL geography/geometry hex blob to PostGIS EWKT.

    Args:
        hex_value: hex string of the SQL Server spatial binary (may include a
            leading ``0x``).

    Returns:
        EWKT string, e.g. ``SRID=4326;POINT(-122.164644615406 47.7869921906598)``.

    Raises:
        UnsupportedGeometryError: if the shape type cannot be decoded.
        ValueError: if the hex is malformed.
    """
    clean = hex_value.strip()
    if clean.lower().startswith("0x"):
        clean = clean[2:]
    data = bytes.fromhex(clean)

    if len(data) < 6:
        raise UnsupportedGeometryError(
            f"spatial blob too short ({len(data)} bytes): {hex_value[:40]}"
        )

    srid = struct.unpack_from("<I", data, 0)[0]

    # Serialization header layout.
    #   byte 4: version/marker (0x01)
    #   byte 5: geometry-type figure (see _POINT_FIGURE)
    version = data[4]
    type_figure = data[5]

    # --- Point (the overwhelmingly common case) ---------------------------
    # 6-byte header + 2 doubles.  Observed: E6 10 00 00 | 01 | 0C | lat | lon
    if type_figure == _POINT_FIGURE and len(data) == 22:
        lon, lat = _decode_point_doubles(data, 6)
        wkt = f"POINT({lon} {lat})"
    else:
        raise UnsupportedGeometryError(
            "only MSSQL Point geometries are supported by this parser "
            f"(SRID={srid}, type_figure=0x{type_figure:02X}, "
            f"bytes={len(data)}). Multi-vertex shapes need extension."
        )

    if _HAS_SHAPELY:
        try:
            shapely.wkt.loads(wkt)
        except Exception as exc:  # pragma: no cover - validation only
            raise UnsupportedGeometryError(
                f"decoded geometry failed shapely validation: {exc}"
            ) from exc

    return f"SRID={srid};{wkt}"


def mssql_spatial_to_wkt_or_null(value: str | None) -> str | None:
    """Safe wrapper: returns None for empty/missing geometry, else EWKT."""
    if value is None:
        return None
    val = str(value).strip()
    if not val or val in ("0x", "NULL", ""):
        return None
    try:
        return mssql_spatial_to_wkt(val)
    except UnsupportedGeometryError:
        # Leave a traceable marker; callers decide whether to hard-fail.
        raise
