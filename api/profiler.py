"""
Sheet profiling — turn an uploaded xlsx/csv into a compact statistical
fingerprint the LLM can reason about without seeing the whole file.

The profile is deliberately small (a few KB) so it fits comfortably in a
prompt alongside the schema-inference instructions, no matter how large
the source sheet is.
"""

from __future__ import annotations

import io
import re
import math
from typing import Any

import pandas as pd

MAX_SAMPLE_VALUES = 8
MAX_PREVIEW_ROWS = 12


def _clean_column_name(name: Any, index: int) -> str:
    """Normalise a raw header into something usable, with a fallback."""
    text = str(name).strip() if name is not None else ""
    if not text or text.lower().startswith("unnamed:"):
        return f"column_{index + 1}"
    return re.sub(r"\s+", " ", text)


def _to_identifier(name: str) -> str:
    """Convert a human column header into a safe property identifier."""
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_")
    if not slug:
        slug = "field"
    if slug[0].isdigit():
        slug = f"f_{slug}"
    # lowerCamelCase reads best in Cypher
    head, *tail = slug.split("_")
    return head.lower() + "".join(p.capitalize() for p in tail if p)


def _json_safe(value: Any) -> Any:
    """
    Coerce a pandas/numpy scalar into something json.dumps can handle.

    The numpy branch matters more than it looks: numpy scalars are NOT
    instances of Python int/float, so without `.item()` every number in the
    sheet would fall through to str() and land in Neo4j as text — silently
    breaking every numeric comparison and aggregation downstream.
    """
    if value is None:
        return None

    # Unwrap numpy / pandas scalars to their Python equivalents first.
    item = getattr(value, "item", None)
    if callable(item) and getattr(value, "ndim", None) == 0:
        try:
            value = value.item()
        except (ValueError, AttributeError):
            pass

    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, str):
        return value

    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass

    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _infer_semantic_type(series: pd.Series) -> str:
    """
    A coarser, more useful type than the pandas dtype.

    The LLM cares about 'is this an identifier, a measure, a date, or a
    category' far more than it cares about int64 vs float64.
    """
    non_null = series.dropna()
    if non_null.empty:
        return "empty"

    if pd.api.types.is_datetime64_any_dtype(series):
        return "date"

    if pd.api.types.is_bool_dtype(series):
        return "boolean"

    if pd.api.types.is_numeric_dtype(series):
        # An all-integer column with near-unique values is usually an ID,
        # not something you'd ever want to sum.
        uniqueness = non_null.nunique() / len(non_null)
        looks_integral = bool((non_null == non_null.round()).all())
        if looks_integral and uniqueness > 0.9:
            return "identifier"
        return "measure"

    text = non_null.astype(str)

    # Try to rescue dates stored as text
    try:
        parsed = pd.to_datetime(text, errors="coerce", format="mixed")
        if parsed.notna().mean() > 0.8:
            return "date"
    except Exception:
        pass

    uniqueness = text.nunique() / len(text)
    if uniqueness > 0.9:
        return "identifier"
    if text.nunique() <= max(2, min(30, len(text) * 0.2)):
        return "category"
    return "text"


def profile_column(series: pd.Series, display_name: str, index: int) -> dict:
    non_null = series.dropna()
    total = len(series)
    distinct = int(non_null.nunique()) if total else 0

    samples = [
        _json_safe(v)
        for v in non_null.drop_duplicates().head(MAX_SAMPLE_VALUES).tolist()
    ]

    profile = {
        "name": display_name,
        "identifier": _to_identifier(display_name),
        "position": index,
        "semanticType": _infer_semantic_type(series),
        "distinctCount": distinct,
        "nullCount": int(total - len(non_null)),
        "fillRate": round(len(non_null) / total, 3) if total else 0.0,
        "uniqueRatio": round(distinct / len(non_null), 3) if len(non_null) else 0.0,
        "sampleValues": samples,
    }

    if pd.api.types.is_numeric_dtype(series) and not non_null.empty:
        profile["min"] = _json_safe(non_null.min())
        profile["max"] = _json_safe(non_null.max())

    return profile


MIN_ROWS_PER_TABLE = 1
MAX_TABLES_PER_FILE = 12


def read_sheet(raw: bytes, filename: str, sheet_name: str | None = None) -> tuple[pd.DataFrame, list[str]]:
    """
    Read one table out of an uploaded file.

    Returns the frame plus the list of available sheet names (empty for CSV),
    so the UI can offer a sheet picker for multi-tab workbooks.
    """
    lower = filename.lower()
    buffer = io.BytesIO(raw)

    if lower.endswith((".csv", ".tsv", ".txt")):
        sep = "\t" if lower.endswith(".tsv") else None
        frame = pd.read_csv(buffer, sep=sep, engine="python")
        return frame, []

    workbook = pd.ExcelFile(buffer)
    names = list(workbook.sheet_names)
    target = sheet_name if sheet_name in names else names[0]
    frame = workbook.parse(target)
    return frame, names


def read_tables(raw: bytes, filename: str) -> list[tuple[str, pd.DataFrame]]:
    """
    Read EVERY table an upload contains.

    A CSV yields one. A workbook yields one per tab, because a multi-tab
    workbook is the most common way people already store related tables —
    orders on one tab, the customer master on another — and those tabs are
    exactly what we want to join in the graph.

    Returns (display name, frame) pairs. Empty tabs are dropped rather than
    surfaced as tables with no columns.
    """
    lower = filename.lower()
    stem = filename.rsplit(".", 1)[0]

    if lower.endswith((".csv", ".tsv", ".txt")):
        sep = "\t" if lower.endswith(".tsv") else None
        frame = clean_frame(pd.read_csv(io.BytesIO(raw), sep=sep, engine="python"))
        return [(stem, frame)] if not frame.empty else []

    workbook = pd.ExcelFile(io.BytesIO(raw))
    tables: list[tuple[str, pd.DataFrame]] = []
    tab_names = list(workbook.sheet_names)[:MAX_TABLES_PER_FILE]

    for tab in tab_names:
        frame = clean_frame(workbook.parse(tab))
        if frame.empty or len(frame) < MIN_ROWS_PER_TABLE or len(frame.columns) < 2:
            continue
        # Only qualify the name when it is ambiguous — "orders" reads better
        # than "orders › Sheet1" when there is only one tab.
        name = stem if len(tab_names) == 1 else f"{stem} › {tab}"
        tables.append((name, frame))

    return tables


def clean_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """
    Normalise a raw sheet: drop all-empty rows and columns, then tidy headers.

    This must run BEFORE any validation of shape, otherwise a sheet with one
    real column plus a trailing empty one passes a 'has at least 2 columns'
    check and then profiles as a single column.
    """
    frame = frame.dropna(axis=1, how="all").dropna(axis=0, how="all")
    renamed: dict = {}
    used: set[str] = set()

    for i, original in enumerate(frame.columns):
        name = _clean_column_name(original, i)
        # Duplicate headers are common in exported reports and would silently
        # collapse columns together.
        if name in used:
            suffix = 2
            while f"{name} ({suffix})" in used:
                suffix += 1
            name = f"{name} ({suffix})"
        used.add(name)
        renamed[original] = name

    return frame.rename(columns=renamed)


def profile_dataframe(
    frame: pd.DataFrame,
    filename: str,
    sheet_names: list[str] | None = None,
    sheet_name: str | None = None,
) -> dict:
    """
    Build the full profile payload sent to the UI and to the LLM.

    `sheetName` is the identity this table is known by everywhere downstream —
    the schema references it, so it must be stable and unique across an upload.
    """
    frame = clean_frame(frame)

    columns = [
        profile_column(frame[name], name, i)
        for i, name in enumerate(frame.columns)
    ]

    preview_frame = frame.head(MAX_PREVIEW_ROWS)
    preview = [
        {col: _json_safe(row[col]) for col in frame.columns}
        for _, row in preview_frame.iterrows()
    ]

    return {
        "sheetName": sheet_name or filename.rsplit(".", 1)[0],
        "filename": filename,
        "sheetNames": sheet_names or [],
        "rowCount": int(len(frame)),
        "columnCount": int(len(frame.columns)),
        "columns": columns,
        "preview": preview,
    }


def frame_to_records(frame: pd.DataFrame) -> list[dict]:
    """Json-safe row dicts, using the same cleaned column names as the profile."""
    frame = clean_frame(frame)

    return [
        {col: _json_safe(row[col]) for col in frame.columns}
        for _, row in frame.iterrows()
    ]
