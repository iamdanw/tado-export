"""Write the collected data out as CSV or Parquet."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd

from . import frames

# name -> loader(conn, home_id, zone_ids, start, end)
_TABLES = {
    "measurements": lambda c, h, z, s, e: frames.measurements(c, h, z, s, e),
    "setpoints": lambda c, h, z, s, e: frames.setpoints(c, h, z, s, e),
    "call_for_heat": lambda c, h, z, s, e: frames.call_for_heat(c, h, z, s, e),
    "stripes": lambda c, h, z, s, e: frames.stripes(c, h, z, s, e),
    "weather": lambda c, h, z, s, e: frames.weather(c, h, s, e),
    # These reach further back than the dayReport tables above.
    "running_times": lambda c, h, z, s, e: frames.running_times(c, h, z, s, e),
    "energy": lambda c, h, z, s, e: frames.energy(c, h, s, e),
    "meter_readings": lambda c, h, z, s, e: frames.meter_readings(c, h),
    "tariffs": lambda c, h, z, s, e: frames.tariffs(c, h),
}


def export(
    conn: sqlite3.Connection,
    home_id: int,
    out_dir: Path,
    *,
    zone_ids: list[int] | None = None,
    start: date | None = None,
    end: date | None = None,
    fmt: str = "csv",
    tz: str | None = None,
    tables: list[str] | None = None,
) -> dict[str, tuple[Path, int]]:
    """Write one file per dataset. Returns {name: (path, row_count)}."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, tuple[Path, int]] = {}

    wanted = tables or ["enriched", *_TABLES]

    for name in wanted:
        if name == "enriched":
            frame = frames.enriched(conn, home_id, zone_ids, start, end, tz=tz)
        elif name in _TABLES:
            frame = _TABLES[name](conn, home_id, zone_ids, start, end)
        else:
            raise ValueError(f"Unknown table '{name}'. Choose from: enriched, {', '.join(_TABLES)}")

        path = _write(frame, out_dir / f"{name}.{ 'parquet' if fmt == 'parquet' else 'csv'}", fmt)
        written[name] = (path, len(frame))

    return written


def _write(frame: pd.DataFrame, path: Path, fmt: str) -> Path:
    if fmt == "parquet":
        try:
            frame.to_parquet(path, index=False)
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise SystemExit(
                "Parquet export needs pyarrow. Install it with:\n"
                "    pip install 'tado-export[parquet]'"
            ) from exc
    else:
        out = frame.copy()
        # Timezone-aware columns render as ISO-8601 with offset, which Excel and
        # Grafana both parse.
        for column in out.columns:
            if pd.api.types.is_datetime64_any_dtype(out[column]):
                out[column] = out[column].dt.strftime("%Y-%m-%dT%H:%M:%S%z")
        out.to_csv(path, index=False)
    return path
