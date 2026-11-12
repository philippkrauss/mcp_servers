#!/usr/bin/env python3
"""
influx-query - an MCP server that runs read-only Flux queries against an
InfluxDB 2.x instance, so an assistant can analyse collected data.

Design notes:

  * All configuration comes from a .env file next to this script. URL, org,
    token, export directory and the pointers to schema documentation are
    settings; nothing about a specific installation is hard-coded.
  * The read-only token is the actual safety boundary. On top of it, queries
    that call write functions or import network-capable packages are refused
    before they are sent, so a mistake is caught early and explained.
  * Two output paths with two different limits: flux_query returns a compact
    rendering into the conversation and is capped to protect the model's
    context; flux_export streams into a CSV file and is capped generously to
    protect the database host and the local disk.
  * Results are parsed from the annotated CSV that /api/v2/query returns.
    Only the standard library is used for HTTP and CSV.
  * The server writes nothing except files in EXPORT_DIR whose names match its
    own pattern, and it deletes nothing else either.
  * Schema documentation (collector code, field references) is not served by
    this server. Its location is announced in the server instructions, so the
    assistant reads it at the source with its own file tools and always sees
    the current state.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

# The SDK renamed its high-level server class in 2.0: mcp.server.fastmcp.FastMCP
# became mcp.server.mcpserver.MCPServer. The decorator, run() and the
# instructions argument are the same in both, so support either.
try:
    from mcp.server.mcpserver import MCPServer as _Server  # mcp >= 2.0
except ModuleNotFoundError:
    try:
        from mcp.server.fastmcp import FastMCP as _Server  # mcp 1.2 .. 1.x
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Could not import the MCP server class from the 'mcp' package.\n"
            "Install it into this interpreter's environment:\n"
            "    python -m pip install -r requirements.txt"
        ) from exc

USER_AGENT = "influx-query"

# Packages a query may import. Everything that can reach the network, read
# secrets or files on the database host, or send notifications is absent.
# EXTRA_ALLOWED_IMPORTS in .env extends this list deliberately.
DEFAULT_ALLOWED_IMPORTS = frozenset({
    "array", "date", "dict", "generate", "interpolate", "join", "json",
    "math", "regexp", "runtime", "sampledata", "strings", "system",
    "timezone", "types", "universe",
    "experimental", "experimental/aggregate", "experimental/array",
    "experimental/date/boundaries", "experimental/table",
    "influxdata/influxdb", "influxdata/influxdb/schema",
    "influxdata/influxdb/v1",
    "contrib/tomhollingworth/events",
})

# Calls refused regardless of imports. Matched against the query with string
# literals and comments removed, so a tag value like "to(" cannot trigger it.
BLOCKED_CALLS = (
    (re.compile(r"\bto\s*\("), "to() writes data"),
    (re.compile(r"\bwideTo\s*\("), "wideTo() writes data"),
    (re.compile(r"\bhost\s*:"),
     "a host: parameter makes the database host connect to another server"),
)

# File names this server creates in EXPORT_DIR, and the only ones it deletes:
#   20261020-071512_strom-letztes-jahr.csv
#   20261020-071512_strom-letztes-jahr_2.csv    (further result or schema)
#   20261020-071512_strom-letztes-jahr.flux     (the query, for reproducibility)
#   ....partial                                 (left behind by a crash)
EXPORT_NAME_RE = re.compile(
    r"^\d{8}-\d{6}_[a-z0-9-]{1,48}(?:_\d{1,3})?\.(?:csv|flux)(?:\.partial)?$"
)

DURATION_RE = re.compile(r"^-?(?:\d+(?:ns|us|µs|ms|s|mo|m|h|d|w|y))+$")


class ConfigError(RuntimeError):
    """The .env file is missing or unusable."""


class QueryError(RuntimeError):
    """Something went wrong; the message is meant for a human to read."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal .env reader.

    Deliberately does no escape processing: a Windows path such as
    C:\\Users\\me\\exports survives unquoted, which a general-purpose dotenv
    parser cannot promise. Supported syntax is KEY=VALUE, optional 'export '
    prefix, '#' comments on their own line, and optional surrounding quotes.
    """
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


class Config:
    """Everything the server needs to know, read once at import time."""

    def __init__(self) -> None:
        env_path = Path(
            os.environ.get("INFLUX_QUERY_ENV")
            or Path(__file__).resolve().parent / ".env"
        )
        if not env_path.is_file():
            raise ConfigError(
                f"No configuration file at {env_path}.\n"
                "Copy .env.example to .env and fill it in."
            )
        self.env_path = env_path
        values = _parse_env_file(env_path)

        def required(key: str) -> str:
            value = values.get(key, "").strip()
            if not value:
                raise ConfigError(f"{key} is missing or empty in {env_path}.")
            return value

        def number(key: str, default: float, minimum: float = 0) -> float:
            raw = values.get(key, "").strip()
            if not raw:
                return default
            try:
                result = float(raw)
            except ValueError as exc:
                raise ConfigError(f"{key} in {env_path} is not a number: {raw!r}") from exc
            if result < minimum:
                raise ConfigError(f"{key} in {env_path} must be at least {minimum}.")
            return result

        self.url = required("INFLUX_URL").rstrip("/")
        if not re.match(r"^https?://[^/\s]+", self.url):
            raise ConfigError("INFLUX_URL must start with http:// or https://.")
        self.org = required("INFLUX_ORG")
        self.token = required("INFLUX_TOKEN")

        raw_export = values.get("EXPORT_DIR", "").strip()
        self.export_dir: Path | None = Path(raw_export) if raw_export else None

        # "label=path;label=path". The paths are only announced, never read.
        self.schema_sources: list[tuple[str, str]] = []
        for item in values.get("SCHEMA_SOURCES", "").split(";"):
            item = item.strip()
            if not item:
                continue
            label, sep, path = item.partition("=")
            if not sep:
                label, path = "", item
            self.schema_sources.append((label.strip(), path.strip()))

        self.local_timezone = values.get("LOCAL_TIMEZONE", "").strip()

        self.query_timeout = number("QUERY_TIMEOUT", 60, 1)
        self.export_timeout = number("EXPORT_TIMEOUT", 900, 1)
        self.chat_max_rows = int(number("CHAT_MAX_ROWS", 1000, 1))
        self.chat_max_chars = int(number("CHAT_MAX_CHARS", 60000, 1000))
        self.chat_max_scan_bytes = int(number("CHAT_MAX_SCAN_MB", 50, 1) * 1024 * 1024)
        self.export_max_rows = int(number("EXPORT_MAX_ROWS", 2_000_000, 1))
        self.export_max_bytes = int(number("EXPORT_MAX_MB", 200, 1) * 1024 * 1024)

        extra = values.get("EXTRA_ALLOWED_IMPORTS", "")
        self.allowed_imports = DEFAULT_ALLOWED_IMPORTS | {
            item.strip() for item in extra.split(",") if item.strip()
        }


try:
    CFG: Config | None = Config()
    CFG_ERROR: str | None = None
except ConfigError as exc:  # keep the server startable so the error is visible
    CFG = None
    CFG_ERROR = str(exc)


def _config() -> Config:
    if CFG is None:
        raise QueryError(f"Configuration problem.\n\n{CFG_ERROR}")
    return CFG


# ---------------------------------------------------------------------------
# Server instructions
# ---------------------------------------------------------------------------


def _instructions() -> str:
    """The text every MCP client hands to the model when the server starts.

    This is where the assistant learns where to look up what the data means,
    because it never sees this server's README.
    """
    if CFG is None:
        return (
            "Read-only Flux queries against InfluxDB. The server is not configured "
            "yet; influx_status explains what is missing."
        )
    cfg = CFG
    lines = [
        f"Read-only access to an InfluxDB 2.x instance (org '{cfg.org}') via Flux. "
        "Use it to analyse collected measurements together with the user.",
        "",
        "Finding your way:",
        "- list_buckets and describe_bucket show what exists right now "
        "(measurements, fields with type and latest value, tags with values).",
    ]
    if cfg.schema_sources:
        lines.append(
            "- What the data means - units, conversions, how and how often it is "
            "collected - is documented at the sources below. They are paths on the "
            "computer that runs this server. Read them with your own file tools "
            "when that folder is available in the session; this server does not "
            "serve them. Check them before interpreting a field you have not seen "
            "before, and prefer them over memory: collectors change."
        )
        for label, path in cfg.schema_sources:
            lines.append(f"    - {label + ': ' if label else ''}{path}")
    lines += [
        "",
        "Querying:",
        f"- flux_query returns at most {cfg.chat_max_rows} rows / about "
        f"{cfg.chat_max_chars // 1000}k characters into the conversation. Group-key "
        "columns are printed once per table instead of on every row. Aggregate in "
        "Flux (aggregateWindow, sum, mean, last, count) instead of pulling raw points.",
        "- flux_export writes larger results to CSV for analysis with code. "
        "Add |> drop(columns: [\"_start\", \"_stop\"]) to keep files small, and "
        "pivot(rowKey: [\"_time\"], columnKey: [\"_field\"], valueColumn: \"_value\") "
        "for one column per field.",
    ]
    if cfg.export_dir is not None:
        lines.append(f"- Exports land in {cfg.export_dir} (on the same computer).")
    zone = cfg.local_timezone or "<IANA zone>"
    lines.append(
        "- Timestamps are UTC. For windows aligned to local calendar days: "
        "import \"timezone\" and option location = timezone.location(name: "
        f"\"{zone}\")."
    )
    lines += [
        "- Write functions (to, wideTo), host: parameters and network-capable "
        "packages are refused before sending. The token is read-only regardless.",
        "- export_list shows existing exports; run export_cleanup with "
        "dry_run=True before deleting anything.",
    ]
    return "\n".join(lines)


mcp = _Server("influx-query", instructions=_instructions())


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _http_error_text(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read(65536).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - best effort only
        body = ""
    message = body.strip()
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict) and parsed.get("message"):
            message = str(parsed["message"])
    except ValueError:
        pass
    if exc.code == 401:
        hint = "The token was rejected. Check INFLUX_TOKEN."
    elif exc.code == 403:
        hint = "The token lacks permission for this."
    elif exc.code == 404:
        hint = "Not found - check INFLUX_URL, INFLUX_ORG and the bucket name."
    else:
        hint = ""
    parts = [f"InfluxDB answered HTTP {exc.code} {exc.reason}."]
    if message:
        parts.append(message[:2000])
    if hint:
        parts.append(hint)
    return "\n".join(parts)


def _open(method: str, path: str, *, params: dict[str, str] | None = None,
          body: dict | None = None, accept: str = "application/json",
          timeout: float | None = None, auth: bool = True):
    cfg = _config()
    url = cfg.url + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if auth:
        headers["Authorization"] = f"Token {cfg.token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        return urllib.request.urlopen(request, timeout=timeout or cfg.query_timeout)
    except urllib.error.HTTPError as exc:
        raise QueryError(_http_error_text(exc)) from None
    except urllib.error.URLError as exc:
        raise QueryError(f"InfluxDB at {cfg.url} is not reachable: {exc.reason}") from None
    except (socket.timeout, TimeoutError):
        raise QueryError(f"No answer from {cfg.url} within the timeout.") from None


def _get_json(path: str, params: dict[str, str] | None = None,
              auth: bool = True) -> dict:
    with _open("GET", path, params=params, auth=auth) as response:
        raw = response.read(10 * 1024 * 1024)
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise QueryError(f"Unexpected answer from {path}: not JSON.") from exc


# ---------------------------------------------------------------------------
# Query checks
# ---------------------------------------------------------------------------


def _strip_flux(query: str) -> tuple[str, str]:
    """Return (query without comments, query without comments and strings).

    The first keeps string literals so import paths can be read; the second
    blanks them so call patterns are only matched in code.
    """
    no_comments: list[str] = []
    code_only: list[str] = []
    i, n = 0, len(query)
    while i < n:
        ch = query[i]
        if ch == "/" and query.startswith("//", i):
            end = query.find("\n", i)
            i = n if end == -1 else end
            continue
        if ch == '"':
            j = i + 1
            while j < n and query[j] != '"':
                j += 2 if query[j] == "\\" else 1
            literal = query[i:j + 1]
            no_comments.append(literal)
            code_only.append('""')
            i = j + 1
            continue
        no_comments.append(ch)
        code_only.append(ch)
        i += 1
    return "".join(no_comments), "".join(code_only)


def _check_query(query: str) -> None:
    cfg = _config()
    if not query or not query.strip():
        raise QueryError("Refused: the query is empty.")
    no_comments, code_only = _strip_flux(query)
    imports = re.findall(r'^\s*import\s+(?:\w+\s+)?"([^"]*)"', no_comments, re.MULTILINE)
    forbidden = sorted({name for name in imports if name not in cfg.allowed_imports})
    if forbidden:
        raise QueryError(
            "Refused: this query imports packages that are not allowed: "
            + ", ".join(f'"{name}"' for name in forbidden)
            + ".\nAllowed: " + ", ".join(sorted(cfg.allowed_imports))
            + ".\nPackages that can reach the network, read secrets or write are "
            "blocked. A package can be allowed via EXTRA_ALLOWED_IMPORTS in .env."
        )
    for pattern, reason in BLOCKED_CALLS:
        if pattern.search(code_only):
            raise QueryError(f"Refused: {reason}. This server only reads.")


def _flux_str(value: str) -> str:
    """A Flux string literal for an arbitrary value."""
    escaped = (value.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n").replace("${", "\\${"))
    return f'"{escaped}"'


def _flux_start(range_: str) -> str:
    value = (range_ or "").strip()
    if not DURATION_RE.match(value):
        raise QueryError(
            f"Refused: {range_!r} is not a Flux duration such as 30d, 12h or 1y."
        )
    return value if value.startswith("-") else "-" + value


# ---------------------------------------------------------------------------
# Annotated CSV
# ---------------------------------------------------------------------------


@dataclass
class Schema:
    """One block of annotated CSV: column names, types, group flags, defaults."""

    columns: list[str]
    datatypes: list[str]
    group: list[bool]
    defaults: list[str]
    index: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.index = {name: i for i, name in enumerate(self.columns)}

    @property
    def is_error(self) -> bool:
        return bool(self.columns) and self.columns[0] == "error"


class _Counter:
    """Wraps a line iterator, counting characters and enforcing a deadline."""

    def __init__(self, lines: Iterable[str], deadline: float) -> None:
        self._lines = iter(lines)
        self._deadline = deadline
        self.chars = 0

    def __iter__(self) -> "_Counter":
        return self

    def __next__(self) -> str:
        if time.monotonic() > self._deadline:
            raise QueryError(
                "Aborted: the query ran longer than the configured timeout. "
                "Narrow the range or aggregate in Flux."
            )
        line = next(self._lines)
        self.chars += len(line)
        return line


def _parse_annotated_csv(lines: Iterable[str]) -> Iterator[tuple[str, object]]:
    """Yield ("schema", Schema) and ("row", list[str]) events.

    Annotated CSV comes in blocks separated by blank lines. Each block starts
    with #datatype/#group/#default rows, then a header whose first cell is
    empty, then data rows. Several Flux tables can share one block; they are
    told apart by the "table" column. An error during execution arrives as a
    block with the columns error,reference.
    """
    annotations: dict[str, list[str]] = {}
    schema: Schema | None = None
    for row in csv.reader(lines):
        if not row or all(cell == "" for cell in row):
            schema, annotations = None, {}
            continue
        if row[0].startswith("#"):
            annotations[row[0][1:]] = row[1:]
            schema = None
            continue
        if schema is None:
            columns = row[1:]
            width = len(columns)

            def annotation(name: str, fill: str) -> list[str]:
                values = annotations.get(name, [])
                return (values + [fill] * width)[:width]

            schema = Schema(
                columns=columns,
                datatypes=annotation("datatype", "string"),
                group=[flag == "true" for flag in annotation("group", "false")],
                defaults=annotation("default", ""),
            )
            if not schema.is_error:
                yield "schema", schema
            continue
        values = (row[1:] + [""] * len(schema.columns))[:len(schema.columns)]
        values = [v if v != "" else d for v, d in zip(values, schema.defaults)]
        if schema.is_error:
            message = values[0] or "unknown error"
            reference = values[1] if len(values) > 1 else ""
            raise QueryError(
                "InfluxDB reported an error while running the query:\n"
                f"{message}" + (f" (reference {reference})" if reference else "")
            )
        yield "row", values


def _run_flux(query: str, timeout: float) -> tuple[Iterator[tuple[str, object]], _Counter, object]:
    cfg = _config()
    response = _open(
        "POST", "/api/v2/query", params={"org": cfg.org},
        body={
            "query": query,
            "type": "flux",
            "dialect": {
                "header": True,
                "annotations": ["datatype", "group", "default"],
                "delimiter": ",",
                "dateTimeFormat": "RFC3339",
            },
        },
        accept="application/csv", timeout=timeout,
    )
    text = io.TextIOWrapper(response, encoding="utf-8", newline="")
    counter = _Counter(text, time.monotonic() + timeout)
    return _parse_annotated_csv(counter), counter, response


def _csv_line(values: Iterable[str]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="").writerow(list(values))
    return buffer.getvalue()


def _pair(name: str, value: str) -> str:
    if value == "" or re.search(r'[\s"=]', value):
        value = json.dumps(value, ensure_ascii=False)
    return f"{name}={value}"


def _time_key(value: str) -> str:
    """Sortable key for RFC3339 UTC timestamps with optional fractions."""
    if value.endswith("Z"):
        main, _, frac = value[:-1].partition(".")
        return f"{main}.{frac.ljust(9, '0')}"
    return value


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------


@dataclass
class _ChatTable:
    result: str
    table: str
    header: list[tuple[str, str]]
    columns: list[str]
    lines: list[str] = field(default_factory=list)


def _query_for_chat(query: str) -> str:
    cfg = _config()
    _check_query(query)
    events, counter, response = _run_flux(query, cfg.query_timeout)

    tables: list[_ChatTable] = []
    results: set[str] = set()
    total_rows = total_tables = shown_rows = chars = 0
    truncated = scan_capped = False
    schema: Schema | None = None
    current: tuple[str, str] | None = None
    keep: list[int] = []

    try:
        with response:
            for kind, payload in events:
                if kind == "schema":
                    schema = payload  # type: ignore[assignment]
                    current = None
                    keep = [
                        i for i, name in enumerate(schema.columns)
                        if not schema.group[i] and name not in ("result", "table")
                    ]
                    continue
                assert schema is not None
                values: list[str] = payload  # type: ignore[assignment]
                ri, ti = schema.index.get("result"), schema.index.get("table")
                result = values[ri] if ri is not None else ""
                table = values[ti] if ti is not None else str(total_tables)
                results.add(result)
                total_rows += 1
                if (result, table) != current:
                    current = (result, table)
                    total_tables += 1
                    if not truncated:
                        header = [
                            (name, values[i]) for i, name in enumerate(schema.columns)
                            if schema.group[i] and name not in ("result", "table")
                        ]
                        tables.append(_ChatTable(
                            result, table, header, [schema.columns[i] for i in keep]
                        ))
                if not truncated:
                    line = _csv_line(values[i] for i in keep)
                    if shown_rows >= cfg.chat_max_rows or chars + len(line) > cfg.chat_max_chars:
                        truncated = True
                    else:
                        tables[-1].lines.append(line)
                        shown_rows += 1
                        chars += len(line) + 1
                if truncated and counter.chars > cfg.chat_max_scan_bytes:
                    scan_capped = True
                    break
    except (socket.timeout, TimeoutError):
        raise QueryError("Aborted: InfluxDB stopped sending data (read timeout).") from None

    if total_rows == 0:
        return "No rows. The query ran, but returned nothing for this range and filter."

    tables = [t for t in tables if t.lines]

    # _start/_stop are usually identical for every table; say it once.
    common: list[tuple[str, str]] = []
    for name in ("_start", "_stop"):
        found = {dict(t.header).get(name) for t in tables}
        if len(found) == 1 and None not in found:
            common.append((name, found.pop()))
    if common:
        names = {name for name, _ in common}
        for t in tables:
            t.header = [(k, v) for k, v in t.header if k not in names]

    at_least = "at least " if scan_capped else ""
    summary = f"{at_least}{_n(total_tables, 'table')}, {at_least}{_n(total_rows, 'row')}"
    if common:
        summary += "  |  " + " ".join(_pair(k, v) for k, v in common)
    out = [summary]
    if truncated:
        out.append(
            f"TRUNCATED: showing {_n(shown_rows, 'row')} in {_n(len(tables), 'table')}. "
            "Aggregate in Flux (aggregateWindow, sum, mean), narrow the range, "
            "or use flux_export for the full result."
        )
    multiple_results = len(results) > 1
    previous_columns: list[str] | None = None
    for t in tables:
        label = f"{t.result} · table {t.table}" if multiple_results else f"table {t.table}"
        head = f"[{label}]"
        if t.header:
            head += " " + " ".join(_pair(k, v) for k, v in t.header)
        out.append("")
        out.append(head)
        if t.columns != previous_columns:
            out.append(",".join(t.columns))
            previous_columns = t.columns
        out.extend(t.lines)
    return "\n".join(out)


def _slug(name: str) -> str:
    value = (name or "").strip().lower()
    for src, dst in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        value = value.replace(src, dst)
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:40].strip("-") or "export"


def _export_dir(create: bool) -> Path:
    cfg = _config()
    if cfg.export_dir is None:
        raise QueryError("EXPORT_DIR is not set in .env, so exports are disabled.")
    directory = cfg.export_dir
    if not directory.is_dir():
        if not create:
            raise QueryError(f"The export directory {directory} does not exist (yet).")
        if not directory.parent.is_dir():
            # A missing parent usually means a detached drive or a sync client
            # that is not running - do not invent a directory tree there.
            raise QueryError(
                f"Refused: the parent of EXPORT_DIR ({directory.parent}) does not exist."
            )
        directory.mkdir()
    return directory


@dataclass
class _ExportFile:
    path: Path
    partial: Path
    handle: io.TextIOBase
    writer: object
    result: str
    columns: list[str]
    rows: int = 0
    first_time: str | None = None
    last_time: str | None = None
    preview: list[str] = field(default_factory=list)


def _export(query: str, name: str) -> str:
    cfg = _config()
    _check_query(query)
    directory = _export_dir(create=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = _slug(name)
    base = f"{stamp}_{slug}"
    counter_suffix = 2
    while any(directory.glob(base + ".*")) or any(directory.glob(base + "_*")):
        base = f"{stamp}_{slug[:44]}-{counter_suffix}"
        counter_suffix += 1

    events, counter, response = _run_flux(query, cfg.export_timeout)
    files: dict[tuple[str, tuple[str, ...]], _ExportFile] = {}
    schema: Schema | None = None
    keep: list[int] = []
    total_rows = 0
    written = 0

    def open_file(result: str, columns: list[str]) -> _ExportFile:
        number = len(files) + 1
        final = directory / (f"{base}.csv" if number == 1 else f"{base}_{number}.csv")
        partial = final.with_name(final.name + ".partial")
        handle = open(partial, "w", encoding="utf-8", newline="")
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(columns)
        return _ExportFile(final, partial, handle, writer, result, columns)

    def discard() -> None:
        for f in files.values():
            try:
                f.handle.close()
            finally:
                f.partial.unlink(missing_ok=True)

    try:
        with response:
            for kind, payload in events:
                if kind == "schema":
                    schema = payload  # type: ignore[assignment]
                    keep = [i for i, col in enumerate(schema.columns) if col != "result"]
                    continue
                assert schema is not None
                values: list[str] = payload  # type: ignore[assignment]
                ri = schema.index.get("result")
                result = values[ri] if ri is not None else ""
                columns = [schema.columns[i] for i in keep]
                key = (result, tuple(columns))
                target = files.get(key)
                if target is None:
                    if len(files) >= 100:
                        raise QueryError(
                            "Aborted: the result has more than 100 different column "
                            "sets. Use keep(), drop() or pivot() to give it one shape."
                        )
                    target = files[key] = open_file(result, columns)
                row = [values[i] for i in keep]
                target.writer.writerow(row)  # type: ignore[attr-defined]
                target.rows += 1
                total_rows += 1
                written += sum(len(v) for v in row) + len(row)
                if len(target.preview) < 5:
                    target.preview.append(_csv_line(row))
                ti = schema.index.get("_time")
                if ti is not None and values[ti]:
                    stamp_value = values[ti]
                    if target.first_time is None or _time_key(stamp_value) < _time_key(target.first_time):
                        target.first_time = stamp_value
                    if target.last_time is None or _time_key(stamp_value) > _time_key(target.last_time):
                        target.last_time = stamp_value
                if total_rows > cfg.export_max_rows:
                    raise QueryError(
                        f"Aborted: more than {cfg.export_max_rows} rows (EXPORT_MAX_ROWS). "
                        "Nothing was kept. Aggregate or narrow the range."
                    )
                if written > cfg.export_max_bytes:
                    raise QueryError(
                        f"Aborted: more than {cfg.export_max_bytes // (1024 * 1024)} MB "
                        "(EXPORT_MAX_MB). Nothing was kept. Drop columns, aggregate or "
                        "narrow the range."
                    )
    except QueryError:
        discard()
        raise
    except (socket.timeout, TimeoutError):
        discard()
        raise QueryError("Aborted: InfluxDB stopped sending data (read timeout). "
                         "Nothing was kept.") from None
    except BaseException:
        discard()
        raise

    if total_rows == 0:
        discard()
        return "No rows. The query ran, but returned nothing - no file was written."

    for f in files.values():
        f.handle.close()
        f.partial.replace(f.path)

    flux_path = directory / f"{base}.flux"
    header = [
        "// influx-query export",
        f"// created: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"// org:     {cfg.org}",
    ]
    for f in files.values():
        header.append(f"// file:    {f.path.name}  ({f.rows} rows"
                      + (f", result {f.result}" if f.result else "") + ")")
    flux_path.write_text("\n".join(header) + "\n\n" + query.rstrip() + "\n", encoding="utf-8")

    out = [f"Exported {_n(total_rows, 'row')} into {_n(len(files), 'file')} in {directory}:"]
    for f in files.values():
        size = f.path.stat().st_size
        line = f"- {f.path.name}: {f.rows} rows, {_human_size(size)}"
        if f.result and len(files) > 1:
            line += f", result {f.result}"
        if f.first_time:
            line += f", _time {f.first_time} .. {f.last_time}"
        out.append(line)
        out.append(f"  columns: {', '.join(f.columns)}")
    out.append(f"- {flux_path.name}: the query")
    first = next(iter(files.values()))
    out.append("")
    out.append(f"First rows of {first.path.name}:")
    out.append(",".join(first.columns))
    out.extend(first.preview)
    return "\n".join(out)


def _n(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _managed_exports(directory: Path) -> tuple[list[Path], int]:
    managed, other = [], 0
    for entry in directory.iterdir():
        if entry.is_file() and EXPORT_NAME_RE.match(entry.name):
            managed.append(entry)
        else:
            other += 1
    managed.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return managed, other


def _export_base(name: str) -> str:
    """20261020-071512_strom_2.csv.partial -> 20261020-071512_strom"""
    stem = name.split(".", 1)[0]
    return re.sub(r"_\d{1,3}$", "", stem)


def _age(seconds: float) -> str:
    days = seconds / 86400
    if days >= 1:
        return f"{days:.0f} d"
    hours = seconds / 3600
    return f"{hours:.0f} h" if hours >= 1 else f"{seconds / 60:.0f} min"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def influx_status() -> str:
    """Report connection, server version, token scope and local settings.

    Read-only. Run it first when something does not work.
    """
    try:
        cfg = _config()
    except QueryError as exc:
        return str(exc)

    report = ["influx-query - status", ""]
    report.append(f"Config:       {cfg.env_path}")
    report.append(f"InfluxDB:     {cfg.url}  (org {cfg.org})")

    try:
        health = _get_json("/health", auth=False)
        report.append(f"Health:       {health.get('status', '?')}, version "
                      f"{health.get('version', '?')}")
    except QueryError as exc:
        report.append(f"Health:       unreachable - {exc}")
        return "\n".join(report)

    try:
        listing = _get_json("/api/v2/buckets", {"org": cfg.org, "limit": "100"})
        count = len(listing.get("buckets", []))
        report.append(f"Token:        accepted, sees {_n(count, 'bucket')}")
    except QueryError as exc:
        report.append(f"Token:        {exc}")

    # A token that can list authorizations is not a read-only token. The API
    # filters the listing by permission, so a read-only token gets [] or 401.
    try:
        auths = _get_json("/api/v2/authorizations", {"org": cfg.org})
        if auths.get("authorizations"):
            report.append(
                "              WARNING: this token can read authorizations, so it is "
                "broader than read-only. Create one with:\n"
                f"              influx auth create --org {cfg.org} --read-buckets "
                "--description \"mcp influx-query (read-only)\""
            )
        else:
            report.append("              cannot read authorizations - as it should be")
    except QueryError:
        report.append("              cannot read authorizations - as it should be")

    if cfg.export_dir is None:
        report.append("Exports:      disabled (EXPORT_DIR not set)")
    elif cfg.export_dir.is_dir():
        managed, _ = _managed_exports(cfg.export_dir)
        total = sum(p.stat().st_size for p in managed)
        report.append(f"Exports:      {cfg.export_dir}  ({_n(len(managed), 'file')}, "
                      f"{_human_size(total)})")
    else:
        state = "will be created" if cfg.export_dir.parent.is_dir() else "PARENT MISSING"
        report.append(f"Exports:      {cfg.export_dir}  (does not exist - {state})")

    if cfg.schema_sources:
        report.append("Schema docs:")
        for label, path in cfg.schema_sources:
            state = "ok" if Path(path).exists() else "NOT FOUND"
            report.append(f"  {state:9} {label + ': ' if label else ''}{path}")
    else:
        report.append("Schema docs:  none configured (SCHEMA_SOURCES)")

    report.append(
        f"Limits:       chat {cfg.chat_max_rows} rows / {cfg.chat_max_chars} chars, "
        f"timeout {cfg.query_timeout:.0f} s; export {cfg.export_max_rows} rows / "
        f"{cfg.export_max_bytes // (1024 * 1024)} MB, timeout {cfg.export_timeout:.0f} s"
    )
    return "\n".join(report)


@mcp.tool()
def list_buckets() -> str:
    """List all buckets the token can read, with retention and description."""
    try:
        cfg = _config()
        listing = _get_json("/api/v2/buckets", {"org": cfg.org, "limit": "100"})
    except QueryError as exc:
        return str(exc)
    buckets = listing.get("buckets", [])
    if not buckets:
        return "The token sees no buckets."

    def retention(bucket: dict) -> str:
        rules = bucket.get("retentionRules") or []
        seconds = rules[0].get("everySeconds", 0) if rules else 0
        if not seconds:
            return "forever"
        days = seconds / 86400
        return f"{days:.0f}d" if days >= 1 else f"{seconds / 3600:.0f}h"

    buckets.sort(key=lambda b: (b.get("type") == "system", b.get("name", "")))
    width = max(len(b.get("name", "")) for b in buckets)
    out = [f"{len(buckets)} buckets in org {cfg.org}:"]
    for b in buckets:
        line = f"  {b.get('name', ''):<{width}}  {retention(b):>8}"
        if b.get("type") == "system":
            line += "  (system)"
        if b.get("description"):
            line += f"  {b['description']}"
        out.append(line)
    return "\n".join(out)


@mcp.tool()
def describe_bucket(bucket: str, range: str = "30d") -> str:  # noqa: A002 - tool argument name
    """Show the live schema of one bucket within a time range.

    Lists measurements, their fields with data type and latest value, and
    their tags with the values seen in the range. Use this before writing a
    query against a bucket. `range` is a Flux duration such as 30d, 12h, 1y.
    """
    try:
        cfg = _config()
        start = _flux_start(range)
        b = _flux_str(bucket)
        measurements = _collect_values(
            'import "influxdata/influxdb/schema"\n'
            f"schema.measurements(bucket: {b}, start: {start})"
        )
        if not measurements:
            return (f"No data in bucket {bucket!r} within {start}. "
                    "Check the name with list_buckets or widen the range.")
        out = [f"Bucket {bucket}, range {start}: {_n(len(measurements), 'measurement')}"]
        for measurement in measurements[:30]:
            out.append("")
            out.append(measurement)
            out.extend(_describe_measurement(b, _flux_str(measurement), start))
        if len(measurements) > 30:
            out.append(f"\n... and {len(measurements) - 30} more measurements.")
        return "\n".join(out)
    except QueryError as exc:
        return str(exc)
    except (socket.timeout, TimeoutError):
        return "Aborted: InfluxDB stopped sending data (read timeout)."


def _collect_values(query: str) -> list[str]:
    """Run a query and return the _value column of all rows."""
    events, _, response = _run_flux(query, _config().query_timeout)
    found: list[str] = []
    schema: Schema | None = None
    with response:
        for kind, payload in events:
            if kind == "schema":
                schema = payload  # type: ignore[assignment]
            elif schema is not None and "_value" in schema.index:
                found.append(payload[schema.index["_value"]])  # type: ignore[index]
    return found


def _describe_measurement(b: str, m: str, start: str) -> list[str]:
    tag_keys = [
        key for key in _collect_values(
            'import "influxdata/influxdb/schema"\n'
            f"schema.measurementTagKeys(bucket: {b}, measurement: {m}, start: {start})"
        )
        if key not in ("_start", "_stop", "_measurement", "_field")
    ]

    script = ['import "influxdata/influxdb/schema"']
    for n, key in enumerate(tag_keys):
        script.append(
            f"schema.measurementTagValues(bucket: {b}, measurement: {m}, "
            f"tag: {_flux_str(key)}, start: {start}) |> yield(name: \"tag{n}\")"
        )
    script.append(
        f"from(bucket: {b}) |> range(start: {start}) "
        f"|> filter(fn: (r) => r._measurement == {m}) |> last() "
        '|> group(columns: ["_field"]) |> sort(columns: ["_time"], desc: true) '
        '|> limit(n: 1) |> yield(name: "fields")'
    )

    tag_values: dict[str, list[str]] = {key: [] for key in tag_keys}
    fields: list[str] = []
    events, _, response = _run_flux("\n".join(script), _config().query_timeout)
    schema: Schema | None = None
    with response:
        for kind, payload in events:
            if kind == "schema":
                schema = payload  # type: ignore[assignment]
                continue
            assert schema is not None
            row: list[str] = payload  # type: ignore[assignment]
            result = row[schema.index["result"]] if "result" in schema.index else ""
            if result.startswith("tag") and result[3:].isdigit():
                n = int(result[3:])
                if n < len(tag_keys):
                    tag_values[tag_keys[n]].append(row[schema.index["_value"]])
            elif result == "fields":
                vi = schema.index.get("_value")
                fi = schema.index.get("_field")
                ti = schema.index.get("_time")
                dtype = schema.datatypes[vi] if vi is not None else "?"
                value = row[vi] if vi is not None else ""
                if len(value) > 60:
                    value = value[:57] + "..."
                fields.append(
                    f"    {row[fi] if fi is not None else '?'} ({dtype}) - last "
                    f"{row[ti] if ti is not None else '?'} = {value}"
                )

    lines = ["  fields:"] + (sorted(fields) or ["    (none in range)"])
    if tag_keys:
        lines.append("  tags:")
        for key in tag_keys:
            values = sorted(tag_values[key])
            shown = ", ".join(values[:25])
            more = f" ... ({len(values)} values)" if len(values) > 25 else ""
            lines.append(f"    {key}: {shown}{more}")
    else:
        lines.append("  tags: none")
    return lines


@mcp.tool()
def flux_query(query: str) -> str:
    """Run a Flux query and return the result into the conversation.

    Read-only. Output is capped (CHAT_MAX_ROWS / CHAT_MAX_CHARS). Group-key
    columns are printed once per table; the column header is repeated only
    when it changes. Aggregate in Flux rather than pulling raw points, and
    use flux_export for large results.
    """
    try:
        return _query_for_chat(query)
    except QueryError as exc:
        return str(exc)


@mcp.tool()
def flux_export(query: str, name: str) -> str:
    """Run a Flux query and write the full result to CSV in EXPORT_DIR.

    `name` is a short label for the file name (it is sanitised and prefixed
    with a timestamp; existing files are never overwritten). Next to the CSV
    the query is saved as .flux. Returns paths, row counts, columns and the
    first rows - not the data. If the result has several yields or column
    sets, each gets its own CSV. Exceeding EXPORT_MAX_ROWS / EXPORT_MAX_MB
    aborts and keeps nothing.
    """
    try:
        return _export(query, name)
    except QueryError as exc:
        return str(exc)
    except OSError as exc:
        return f"Export failed while writing the file: {exc}"


@mcp.tool()
def export_list() -> str:
    """List the export files in EXPORT_DIR, newest first, with size and age."""
    try:
        directory = _export_dir(create=False)
        managed, other = _managed_exports(directory)
    except QueryError as exc:
        return str(exc)
    if not managed:
        return f"No exports in {directory}." + (
            f" ({other} other entries there are not managed by this server.)" if other else "")
    now = time.time()
    total = 0
    out = [f"{len(managed)} export files in {directory}:"]
    for p in managed[:200]:
        stat = p.stat()
        total += stat.st_size
        out.append(f"  {p.name}  {_human_size(stat.st_size):>9}  {_age(now - stat.st_mtime)} old")
    if len(managed) > 200:
        total += sum(p.stat().st_size for p in managed[200:])
        out.append(f"  ... and {len(managed) - 200} more")
    out.append(f"Total {_human_size(total)}.")
    if other:
        out.append(f"{other} other entries are not managed by this server and are left alone.")
    return "\n".join(out)


@mcp.tool()
def export_cleanup(older_than_days: int = 30, dry_run: bool = True) -> str:
    """Delete exports older than `older_than_days` from EXPORT_DIR.

    An export (its CSVs and its .flux file) is deleted as a whole. Only files
    whose names match this server's export pattern are touched, and only
    directly in EXPORT_DIR. With dry_run=True (the default) nothing is
    deleted; the files that would go are listed. 0 means all exports.
    """
    try:
        if older_than_days < 0:
            raise QueryError("Refused: older_than_days must be 0 or more.")
        directory = _export_dir(create=False)
        managed, _ = _managed_exports(directory)
    except QueryError as exc:
        return str(exc)
    # An export is one group of files (CSVs plus the .flux query) sharing a
    # base name. It goes as a whole once its newest file is old enough, so a
    # cleanup never leaves a CSV without its query or the other way round.
    groups: dict[str, list[Path]] = {}
    for p in managed:
        groups.setdefault(_export_base(p.name), []).append(p)
    cutoff = time.time() - older_than_days * 86400
    victims = sorted(
        (p for files in groups.values()
         if max(f.stat().st_mtime for f in files) < cutoff for p in files),
        key=lambda p: p.name,
    )
    if not victims:
        return f"Nothing to delete: no exports older than {older_than_days} days."
    size = sum(p.stat().st_size for p in victims)
    exports = len({_export_base(p.name) for p in victims})
    if dry_run:
        out = [f"Dry run - would delete {_n(exports, 'export')}, "
               f"{_n(len(victims), 'file')} ({_human_size(size)}):"]
        out += [f"  {p.name}" for p in victims]
        out.append("Call again with dry_run=False to delete them.")
        return "\n".join(out)
    deleted, failed = [], []
    for p in victims:
        try:
            p.unlink()
            deleted.append(p.name)
        except OSError as exc:
            failed.append(f"{p.name}: {exc}")
    out = [f"Deleted {_n(len(deleted), 'file')} ({_human_size(size)}) from {directory}."]
    if failed:
        out.append("Could not delete:")
        out += [f"  {line}" for line in failed]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
#
# Without arguments this file is an MCP server, which is how MCP clients start
# it. With a subcommand it runs the same tools for a human at a terminal - for
# checking the setup and for queries without an assistant in the loop.

_FAILURE_PREFIXES = (
    "Configuration problem", "Refused", "Aborted", "InfluxDB answered",
    "InfluxDB at", "InfluxDB reported", "No answer", "Export failed",
    "EXPORT_DIR is not set", "The export directory", "Unexpected answer",
)


def _failed(text: str) -> bool:
    return text.startswith(_FAILURE_PREFIXES)


def _read_query(args) -> str:
    import sys

    if args.file:
        return Path(args.file).read_text(encoding="utf-8-sig")
    if args.flux and args.flux != "-":
        return args.flux
    return sys.stdin.read()


def _confirm(question: str) -> bool:
    try:
        answer = input(f"{question} [y/N] ")
    except EOFError:  # no terminal attached - never assume yes
        print()
        return False
    return answer.strip().lower() in ("y", "yes", "j", "ja")


def cli(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="server.py",
        description="Run the influx-query tools from a terminal. "
                    "Without a subcommand, the MCP server starts instead.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="connection, version, token scope, settings")
    sub.add_parser("buckets", help="list buckets")
    describe = sub.add_parser("describe", help="live schema of one bucket")
    describe.add_argument("bucket")
    describe.add_argument("--range", default="30d", help="Flux duration, default 30d")
    query = sub.add_parser("query", help="run Flux, print the compact result")
    query.add_argument("flux", nargs="?", help="the query; '-' or omitted reads stdin")
    query.add_argument("-f", "--file", help="read the query from a file")
    export = sub.add_parser("export", help="run Flux, write CSV to EXPORT_DIR")
    export.add_argument("name", help="short label for the file name")
    export.add_argument("flux", nargs="?", help="the query; '-' or omitted reads stdin")
    export.add_argument("-f", "--file", help="read the query from a file")
    sub.add_parser("exports", help="list export files")
    cleanup = sub.add_parser("cleanup", help="delete old export files")
    cleanup.add_argument("--days", type=int, default=30, help="older than N days (default 30)")
    cleanup.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    sub.add_parser("instructions", help="print the text the server sends to the model")
    args = parser.parse_args(argv)

    if args.command == "status":
        result = influx_status()
    elif args.command == "buckets":
        result = list_buckets()
    elif args.command == "describe":
        result = describe_bucket(args.bucket, args.range)
    elif args.command == "query":
        result = flux_query(_read_query(args))
    elif args.command == "export":
        result = flux_export(_read_query(args), args.name)
    elif args.command == "exports":
        result = export_list()
    elif args.command == "instructions":
        result = _instructions()
    else:  # cleanup
        preview = export_cleanup(args.days, dry_run=True)
        print(preview)
        if not preview.startswith("Dry run"):
            return 1 if _failed(preview) else 0
        if not args.yes and not _confirm("Delete these files?"):
            print("Cancelled - nothing was deleted.")
            return 1
        result = export_cleanup(args.days, dry_run=False)

    print(result)
    return 1 if _failed(result) else 0


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except AttributeError:
                pass
        raise SystemExit(cli(sys.argv[1:]))
    mcp.run()
