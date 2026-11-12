# ksuite-calendar

An MCP server that lets an assistant read calendars and task lists
(reminders) on a CalDAV server and, if allowed, create entries and delete the
ones it created itself. Built for Infomaniak kSuite; nothing in it is specific
to Infomaniak except the defaults in `.env.example`.

## Tools

| Tool | Effect |
|---|---|
| `calendar_status()` | read-only: per configured collection whether it is reachable, its name on the server, whether the server grants write, and whether this server may write |
| `list_calendars()` | read-only: configured calendars and task lists with mode |
| `list_events(start?, end?, calendar?, query?)` | read-only: events in a range (default today + 14 days, max 366), recurrences expanded, with UID |
| `get_event(calendar, uid)` | read-only: one event in full - times, location, description, rule, alarms |
| `list_tasks(task_list?, include_completed=False)` | read-only: tasks sorted by due date |
| `get_task(task_list, uid)` | read-only: one task in full |
| `create_event(calendar, title, start, end?, all_day?, location?, description?, alarm_minutes?, repeat?)` | creates an event, UID `claude-...` |
| `create_task(task_list, title, due?, notes?, repeat?)` | creates a task; a due time gets an alarm at that time |
| `delete_event(calendar, uid)` | deletes an event **only if its UID starts with `claude-`** |
| `delete_task(task_list, uid)` | deletes a task **only if its UID starts with `claude-`** |

`repeat` is one of `none`, `daily`, `weekly`, `monthly`, `yearly`, without an
end date. Times are `YYYY-MM-DD` or `YYYY-MM-DD HH:MM` in `TIMEZONE`.

## Command line

```sh
python server.py status
python server.py calendars
python server.py events --days 7
python server.py events --start 2026-12-24 --end 2026-12-26 --query essen
python server.py event Alltag <uid>
python server.py tasks [--list Test] [--all]
python server.py task Test <uid>
python server.py create-event Alltag "Zahnarzt" "2026-11-20 14:00" --end "2026-11-20 14:30" --alarm 60
python server.py create-event Geburtstage "Oma" 2027-03-02 --all-day --repeat yearly
python server.py create-task Test "Müll rausstellen" --due "2026-11-12 07:00" --repeat weekly
python server.py delete-event Alltag claude-...
python server.py delete-task Test claude-...
python server.py raw Test <uid>          # raw iCalendar data, for debugging
python server.py instructions            # the text the model receives
```

Without a subcommand the file starts as an MCP server. Exit code `0` on
success, `1` when something was refused or failed. Write commands obey
`ALLOW_WRITE` like the tools do.

`probe.py` is the one-off connection check used during setup: which
collections the account sees (with paths for `.env`), their privileges,
recurrence expansion, tasks, and with `--write-test` a create/read/delete
round trip in a test collection.

## Safety

**The account is the boundary.** Run the server as a dedicated CalDAV account
that sees nothing but the calendars shared with it. With Infomaniak an
application password of the main account would also open kDrive over WebDAV;
a separate account with per-calendar shares does not. Shares there must be
accepted twice: the invitation, and then each calendar in the calendar web
app of the receiving account.

**On top of that:**

- Only collections listed in `.env` exist for the server. Tools take display
  names, never URLs, so the model cannot point the server elsewhere.
- `READONLY` collections are never written. `ALLOW_WRITE=false` turns every
  write tool off.
- Existing entries are never changed. The server creates new ones and deletes
  only those it created itself, recognised by the fixed UID prefix `claude-`.
- Every write is appended to `WRITE_LOG` as one JSON line.
- The server instructions tell the model that event and task text is data,
  not instructions, and that writes happen only at the user's request in the
  conversation.
- The password never appears in output; error texts are masked.

In the MCP client, set the four write tools to require approval for every
call.

## Setup

```sh
cd ksuite-calendar
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows
cp .env.example .env                                       # then fill in
.venv/Scripts/python probe.py                              # find the paths
.venv/Scripts/python server.py status
```

Claude Desktop (`claude_desktop_config.json`):

```json
"ksuite-calendar": {
  "command": "C:\\src\\mcp_servers\\ksuite-calendar\\.venv\\Scripts\\python.exe",
  "args": ["C:\\src\\mcp_servers\\ksuite-calendar\\server.py"]
}
```
