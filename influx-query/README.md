# influx-query

An MCP server that runs read-only Flux queries against an InfluxDB 2.x
instance, so that an assistant can analyse collected data together with you.

It answers small questions directly in the conversation and writes large
results to CSV files for analysis with code. It cannot write to the database.

## Tools

| Tool | Effect |
|---|---|
| `influx_status()` | read-only: reachability, server version, whether the token works and is really read-only, export directory, schema sources, limits |
| `list_buckets()` | read-only: buckets with retention and description |
| `describe_bucket(bucket, range="30d")` | read-only: live schema - measurements, fields with type and latest value, tags with their values |
| `flux_query(query)` | read-only: runs Flux, returns a compact rendering into the conversation, capped |
| `flux_export(query, name)` | runs Flux, streams the full result to CSV in `EXPORT_DIR`, returns paths and a preview, not the data |
| `export_list()` | lists export files with size and age |
| `export_cleanup(older_than_days=30, dry_run=True)` | deletes old exports; lists only unless `dry_run=False` |

## Command line

The same tools run from a terminal, for checking the setup and for queries
without an assistant:

```sh
python server.py status
python server.py buckets
python server.py describe strom --range 7d
python server.py query 'from(bucket: "strom") |> range(start: -1h) |> last()'
python server.py query -f query.flux            # or pipe the query via stdin
python server.py export strom-2026 -f query.flux
python server.py exports
python server.py cleanup --days 30              # shows the list, asks, then deletes
python server.py instructions                   # the text the model receives
```

Without a subcommand the file starts as an MCP server. Exit code `0` on
success, `1` when something was refused or failed.

## Safety

**The token is the boundary.** Give the server a read-only token and nothing
else:

```sh
influx auth create --org <org> --read-buckets --description "mcp influx-query (read-only)"
```

Without bucket IDs, `--read-buckets` grants read access to all buckets of the
organisation and nothing more - no writes, no tokens, no settings. When the
CLI runs inside the InfluxDB container, prefix it with
`docker exec -it <container>`. `influx_status` warns if the token can read
authorizations, which a read-only token cannot.

**Checks before sending, as a second line.** Flux is more than a query
language: `to()` writes, `http.post()` and `sql.from()` make the database host
talk to other systems, `secrets.get()` reads stored secrets. Queries are
therefore refused before they leave this machine when they

- import a package that is not on the allowlist (`DEFAULT_ALLOWED_IMPORTS` in
  `server.py`, extendable via `EXTRA_ALLOWED_IMPORTS`) - `http`, `sql`,
  `experimental/http`, `influxdata/influxdb/secrets`, `csv`, notification
  endpoints and the like are not on it,
- call `to()`, `experimental.to()` or `wideTo()`,
- pass a `host:` parameter, which would make `from()` read from another server.

The check ignores comments and string literals, so a tag value such as
`"/to(x)"` does not trip it. It is a pattern check, not a parser, and is not
meant to be a guarantee - the token is.

**Limits.** `flux_query` is capped by rows and characters because its output
lands in the model's context. `flux_export` is capped generously, to protect
the database host and the local disk; exceeding the cap aborts the export and
deletes what was written so far. Both have a timeout.

**Files.** The server writes only into `EXPORT_DIR`, never overwrites a file,
and `export_cleanup` deletes only files whose names match its own export
pattern, directly in that directory. Anything else there is left alone.

### Database-side limits

A 4 GB host can be pushed into swap by a single `range(start: -1y)` over raw
data. The checks above cannot prevent that; `influxd` can. Worth setting in the
InfluxDB container (values are a starting point for a small VPS - check the
documentation of your version for how the options interact):

```yaml
environment:
  INFLUXD_QUERY_CONCURRENCY: "4"
  INFLUXD_QUERY_QUEUE_SIZE: "10"
  INFLUXD_QUERY_INITIAL_MEMORY_BYTES: "10485760"    # 10 MB
  INFLUXD_QUERY_MEMORY_BYTES: "536870912"           # 512 MB per query
  INFLUXD_QUERY_MAX_MEMORY_BYTES: "1610612736"      # 1.5 GB for all queries
```

That protects against every client, Grafana included, not only this one.

## Output

`flux_query` renders the annotated CSV compactly. Group-key columns (tags,
`_field`, `_measurement`) appear once per table instead of on every row,
`_start`/`_stop` once for the whole result when they are the same everywhere,
and the column header only when it changes:

```
3 tables, 72 rows  |  _start=2026-10-19T00:00:00Z _stop=2026-10-20T00:00:00Z

[table 0] _field=power _measurement=easymeter location=franzenstrasse
_time,_value
2026-10-19T00:00:00Z,412.3
...
[table 1] _field=power _measurement=easymeter location=sandstrasse
2026-10-19T00:00:00Z,388.0
```

When the result is cut, the first line still reports the full table and row
count, and a `TRUNCATED` line says what to do instead.

## Exports

`flux_export(query, name)` writes

```
20261020-071512_strom-letztes-jahr.csv     the result, one header row
20261020-071512_strom-letztes-jahr_2.csv   only if there is a second yield or column set
20261020-071512_strom-letztes-jahr.flux    the query, with creation time and row counts
```

The CSV keeps every column except the constant `result`. With several
`yield()`s or differently shaped tables, each result or column set gets its
own file, so every file has a single header. Rows are streamed to disk, not
collected in memory. While writing, files carry a `.partial` suffix; they are
renamed only on success.

`export_cleanup` treats the CSVs and the `.flux` of one export as a unit and
deletes them together once the newest of them is older than
`older_than_days`.

## Schema documentation

The server does not serve any documentation. Instead, `SCHEMA_SOURCES` in
`.env` lists where the meaning of the data is documented - collector code,
field references, dashboards with working queries - and the server announces
these paths in its MCP instructions. The assistant reads them at the source
with its own file tools, so a new collector is visible immediately and no copy
can go stale. If the folder is not available in a session, the assistant
falls back to `describe_bucket`, which reflects what is actually in the
database.

## Requirements

- Python 3.10 or newer
- The `mcp` SDK, 1.x or 2.x - the import handles both, as in `static-site-sync`
- Nothing else: HTTP and CSV use the standard library

## Setup

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt   # Windows: .venv\Scripts\python.exe
cp .env.example .env                                  # then edit it
.venv/bin/python server.py status
```

Register the server with your MCP client, for example in
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "influx-query": {
      "command": "C:\\src\\mcp_servers\\influx-query\\.venv\\Scripts\\python.exe",
      "args": ["C:\\src\\mcp_servers\\influx-query\\server.py"]
    }
  }
}
```

The `.env` file is read from the directory containing `server.py`. Set
`INFLUX_QUERY_ENV` to an absolute path to override that.

A missing or invalid `.env` does not stop the server from starting. The tools
report the problem instead, which is easier to debug than a client that shows
no tools at all.

## Configuration

See [.env.example](.env.example). `INFLUX_URL`, `INFLUX_ORG` and
`INFLUX_TOKEN` are required; everything else has a default or is optional.

## Licence

None yet - all rights reserved by default.
