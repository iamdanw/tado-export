# tado-export

Export the measurement history from your tado° devices into a local SQLite
database, then explore it in an offline dashboard.

Everything stays on your machine: one database file, one local web page, no
third-party service in between.

---

## What it collects

For every zone and every day it fetches the tado `dayReport`, which carries a
full day of:

| Data | Resolution | Table |
|---|---|---|
| Inside temperature | 15 min | `measurement.inside_temp_c` |
| Relative humidity | 15 min | `measurement.humidity_pct` |
| Target temperature / power | intervals | `setpoint` |
| Boiler demand (`NONE`/`LOW`/`MEDIUM`/`HIGH`) | intervals | `call_for_heat` |
| Home / away / open window / overlay | intervals | `stripe` |
| Hot water production | intervals | `hot_water` |
| Outside temperature & weather state | intervals | `weather_condition` |
| Device connectivity | intervals | `device_connected` |

Everything tado returns is kept, including fields not yet broken out into
columns (the 4-hourly `weather.slots` forecast, the min/max summary blocks).
`reparse` can extract those later without spending an API call.

### Sources that outlive the 13-month window

`dayReport` is not the only history tado holds. Two further endpoints reach back
to when the home was created, and `sync` collects them too:

| Data | Granularity | Table | Source |
|---|---|---|---|
| Heating runtime per zone | per day | `running_time` | `minder.tado.com` |
| Whole-home boiler runtime | per day | `running_time_day` | `minder.tado.com` |
| Gas / energy consumption and cost | per day | `energy_consumption` | Energy IQ |
| Meter readings you entered | per reading | `meter_reading` | Energy IQ |
| Tariff history | per period | `tariff` | Energy IQ |

Running times come back for the whole span in a **single** API call; consumption
is one call per month. Together that is a few dozen calls for several years of
history, so there is no reason to skip it (`--no-extras` if you must).

This matters: on a home created in Nov 2022, `dayReport` only still serves data
from Jul 2025, but running times and consumption go all the way back.

The verbatim API response for each zone-day is also kept, gzipped, in
`day_report_raw`, and the other sources in `raw_document`. That is what makes `tado-export reparse` able to rebuild every
table above without spending a single API call.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

`tado-export` is a package, not a set of loose scripts — run the installed
command (or `python -m tado_export`), never `python tado_export/cli.py`.

## Use

```bash
tado-export login             # approve once in a browser
tado-export homes             # list homes and zone ids
tado-export sync --days 30    # pull the last 30 days
tado-export status            # coverage and gaps per zone
tado-export dashboard         # http://127.0.0.1:8050
```

### Backfilling everything

With no date flags, `sync` walks back to the day each zone was created:

```bash
tado-export sync                          # whole history, all zones
tado-export sync --since 2024-01-01       # from a specific day
tado-export sync --zone 1 --zone 3        # only some zones
```

It is safe to interrupt. Progress is recorded per zone-day in `sync_day`, so
re-running the same command resumes rather than restarts. The last two days are
always re-fetched, because today is partial and tado backfills late-arriving
measurements for a short while (`--refresh-window`).

### One shareable file

```bash
tado-export report --out home.html          # self-contained, ~6 MB
tado-export report --out home.html --cdn    # ~2 MB, needs internet to draw
```

A single HTML page with the plotly bundle, all chart data, styles, the filter
controls and a light/dark toggle inlined — no server, no network, no
dependencies. It opens from an email attachment or a USB stick.

**It filters like the dashboard, without a server.** Whoever opens it gets:

- period presets — day, week, month, year, all — plus explicit from/to dates and
  earlier/later paging
- a checkbox per zone
- a **Show** dropdown on the demand heatmap, to narrow just that chart to one
  zone without changing the others (the line and bar charts get the same thing
  free by clicking legend entries; a heatmap has no legend)

Every chart, stat tile and table on the page recomputes from those controls. The
series are embedded once at hourly resolution together with per-zone-per-day
sums, so any range aggregates exactly rather than approximately, and spans over
60 days automatically switch to daily means instead of thinning points.

Contents: stat tiles, temperature (with target temperature at hourly zoom),
humidity, a day-by-hour heating-demand heatmap, heating hours per day, and
per-zone and per-month tables.

Two things worth knowing when reading it:

- The demand heatmap **averages across zones**, so one hot zone among eight reads
  lower than it truly is. Use its own **Show** dropdown to isolate a zone; the
  caption always says what is in the mean.
- `--since` / `--to` set what data goes *into* the file. The in-page controls
  then filter within that.

### Exporting

```bash
tado-export export --out ./export                       # CSV, all datasets
tado-export export --format parquet --since 2025-01-01  # needs the [parquet] extra
```

`enriched.csv` is the wide, analysis-ready one: every reading joined to the
setpoint, boiler demand, presence and outside temperature in force at that
moment, with both UTC and home-local timestamps.

## The API rate limit

tado allows **100 requests/day** on a free account and **20,000/day** with
Auto-Assist or AI Assist. One call covers one zone for one day, so a full
backfill costs roughly `zones x days` calls — four zones over two years is about
2,900 calls, comfortably inside the subscriber limit but around a month of
patience on the free tier.

The client reads tado's `ratelimit` response header and stops cleanly before the
quota runs out, telling you what is left. `--max-calls` caps a single run.

## Multiple tado accounts

Each account gets a named profile with its own stored token, and they can all
sync into the same database because rows are keyed by tado's home id:

```bash
tado-export login --profile holiday
tado-export sync  --profile holiday
```

`tado-export status` lists the linked profiles and every home it has data for;
the dashboard gets a home selector.

## tado only keeps about 13 months

Past its retention horizon the `dayReport` endpoint keeps answering `200`, but
with a **synthetic day** rather than an error: inside temperature pinned to a
single value for every sample, humidity likewise, one whole-day `NONE`
call-for-heat interval, and no weather at all.

Stored naively this looks like a perfectly flat 20 °C / 50 % history stretching
back years. `tado-export` detects those days, records them as `placeholder`, and
keeps them out of the tables, charts and exports. `tado-export status` shows the
count in a `filler` column.

Two consequences worth knowing:

- A backfill stops walking further back once a zone returns filler for ten
  consecutive days, instead of spending thousands of calls on nothing.
- **Data you have already collected is never overwritten by filler.** As days age
  past the horizon they begin returning synthetic values; if a later sync
  re-fetches such a day, the stored measurements and the stored raw payload are
  both left untouched, and the run reports how many days it protected. This holds
  even under `--force`.

The practical upshot: sync at least every few months and your archive keeps
growing past what tado itself will still tell you.

Note the horizon applies to `dayReport` only — running times and Energy IQ
consumption still reach back to the home's creation, so a first sync recovers
those in full however long you have had the system.

## Notes on the data

A few things the dashboard is deliberately careful about, because getting them
wrong quietly produces plausible but false charts:

- **Gaps are drawn as gaps.** A day that failed to fetch breaks the line instead
  of joining its neighbours with a straight segment.
- **Partial days are not extrapolated.** Heating hours are counted from the
  samples actually present, and a local day covered by less than half its
  readings is hidden from the daily chart with a visible note rather than shown
  as a short day.
- **The demand heatmap averages.** Across several zones one hot zone is diluted
  by the quiet ones, so the peak reads lower than any single zone's. Both the
  dashboard and the report let you narrow that chart to one zone on its own —
  in the dashboard without disturbing the other charts — and the caption always
  names what is in the mean.
- **Hot water is not a room.** A `HOT_WATER` zone measures ~45 °C water, so it is
  excluded from "average inside temperature" and left out of the default zone
  selection.
- **Local days, not UTC days.** Everything is grouped by calendar day in the
  home's own timezone, which is not where the UTC day boundary falls.
- **Partial days are not averaged as if whole.** A daily mean built from four
  readings is not comparable to one built from ninety-six, so under-covered
  buckets are dropped rather than plotted as a spike.
- **Synthetic days are excluded.** See the retention section above.
- **One y-axis per unit.** Inside and outside temperature get stacked panels on a
  shared time axis rather than two scales on one plot.

## Where things live

| Path | What |
|---|---|
| `./tado.db` | the database (override with `--db` or `$TADO_EXPORT_DB`) |
| `~/.config/tado-export/token-*.json` | refresh tokens, mode 0600 |
| `./tado-report.html` | the shareable report, if you build one |

Both are in `.gitignore`. The token file is the credential — treat it like a
password. `tado-export logout` deletes it.

## Authentication

tado removed the OAuth password grant in March 2025. This tool uses the
supported device code flow: `login` prints a URL, you approve it in a browser,
and a refresh token is stored locally. Refresh tokens rotate on every use and
expire after 30 days of disuse, so run a sync at least monthly or re-run
`login`.

## Reference

- [tado authentication](https://help.tado.com/en/articles/8565472-how-do-i-authenticate-to-access-the-rest-api)
- [tado REST API rate limits](https://help.tado.com/en/articles/12165739-limitation-for-rest-api-usage)
- [Community OpenAPI spec for the v2 API](https://github.com/kritsel/tado-openapispec-v2) — the endpoint shapes here follow it

Applies to tado° V3+ (`my.tado.com/api/v2`). tado° X homes use a different API
host and are not supported yet; `tado-export homes` prints the home generation.
