#!/usr/bin/env python3
"""
probe.py - one-off check of the CalDAV access before ksuite-calendar is built.

Answers four questions and prints the answers, never the password:

  1. Can the account log in, and which collections does it see?
     (name, component types, the privileges the server grants)
  2. Do events come back for the next days, with recurrences expanded
     (birthdays) and all-day events recognisable?
  3. Do tasks (VTODO) come back from task lists such as "Test"?
  4. Only with --write-test: can an object be created, read back and deleted
     in the test collection? The object carries a fixed UID prefix and is
     removed again at once.

Usage:
    python probe.py
    python probe.py --days 60
    python probe.py --diagnose                 # where are shared calendars?
    python probe.py --write-test               # uses the collection "Test"
    python probe.py --write-test --test-calendar "Andere Liste"

Configuration comes from .env next to this script (see .env.example).
Exit code 0 when every check passed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import caldav
    from icalendar import Calendar as ICalendar, Event as IEvent, Todo as ITodo
except ModuleNotFoundError as exc:
    raise SystemExit(
        f"Missing package: {exc.name}\n"
        "Install the requirements into this interpreter's environment:\n"
        "    python -m pip install -r requirements.txt"
    ) from exc

TZ = ZoneInfo("Europe/Berlin")
WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
PROBE_UID_PREFIX = "ksuite-calendar-probe-"
DAV_NS = "{DAV:}"

failures: list[str] = []


def section(title: str) -> None:
    print()
    print(f"== {title} " + "=" * max(0, 70 - len(title)))


def fail(msg: str) -> None:
    failures.append(msg)
    print(f"  FAIL  {msg}")


def ok(msg: str) -> None:
    print(f"  ok    {msg}")


def note(msg: str) -> None:
    print(f"        {msg}")


def parse_env(path: Path) -> dict[str, str]:
    """Same minimal .env reader as influx-query: KEY=VALUE, '#' comments, optional quotes."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_config() -> dict[str, str]:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        raise SystemExit(f"No configuration file at {env_path}. Copy .env.example to .env and fill it in.")
    values = parse_env(env_path)
    missing = [k for k in ("CALDAV_URL", "CALDAV_USER", "CALDAV_PASSWORD") if not values.get(k)]
    if missing:
        raise SystemExit(f"Missing or empty in {env_path}: {', '.join(missing)}")
    if not values["CALDAV_URL"].startswith("https://"):
        raise SystemExit("CALDAV_URL must start with https://")
    return values


def safe_error(exc: Exception, password: str) -> str:
    """Exception text with the password masked, just in case a library echoes it."""
    text = f"{type(exc).__name__}: {exc}"
    return text.replace(password, "***") if password else text


def privileges(client: "caldav.DAVClient", url: str) -> list[str]:
    """DAV:current-user-privilege-set of a collection, e.g. ['read', 'write', ...]."""
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-privilege-set/></d:prop></d:propfind>'
    )
    response = client.propfind(url, props=body, depth=0)
    tree = response.tree
    if tree is None:
        return []
    names = set()
    for priv in tree.iter(f"{DAV_NS}privilege"):
        for child in priv:
            tag = child.tag
            names.add(tag.split("}", 1)[1] if "}" in tag else tag)
    return sorted(names)


def describe_component(comp) -> str:
    """One line for an expanded VEVENT/VTODO: start, all-day flag, summary."""
    summary = str(comp.get("SUMMARY", "(ohne Titel)"))
    start = comp.get("DTSTART") or comp.get("DUE")
    if start is None:
        when = "ohne Datum"
    else:
        value = start.dt
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                value = value.astimezone(TZ)
            when = f"{WEEKDAYS[value.weekday()]} " + value.strftime("%d.%m.%Y %H:%M")
        else:
            when = f"{WEEKDAYS[value.weekday()]} " + value.strftime("%d.%m.%Y") + " (ganztägig)"
    extra = []
    if comp.get("RRULE") is not None or comp.get("RECURRENCE-ID") is not None:
        extra.append("Serie")
    if comp.get("STATUS"):
        extra.append(str(comp.get("STATUS")).lower())
    return f"{when}  {summary}" + (f"  [{', '.join(extra)}]" if extra else "")


def diagnose(client: "caldav.DAVClient", principal) -> None:
    """Where could shared calendars hide? Lists every calendar-home-set, the raw
    children of each home, and delegation/proxy properties of the principal."""
    section("D  Diagnose: Kalender-Heimatordner und Delegationen")

    def propfind(url: str, prop_xml: str, depth: int = 0):
        body = ('<?xml version="1.0" encoding="utf-8"?>'
                '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
                'xmlns:cs="http://calendarserver.org/ns/">'
                f'<d:prop>{prop_xml}</d:prop></d:propfind>')
        return client.propfind(url, props=body, depth=depth).tree

    def short(tag: str) -> str:
        return tag.split("}", 1)[1] if "}" in tag else tag

    purl = str(principal.url)
    note(f"Principal: {principal.url.path}")
    tree = propfind(purl, "<c:calendar-home-set/><cs:calendar-proxy-read-for/>"
                          "<cs:calendar-proxy-write-for/><d:group-membership/>")
    homes = []
    if tree is not None:
        for el in tree.iter():
            name = short(el.tag)
            if name in ("calendar-home-set", "calendar-proxy-read-for",
                        "calendar-proxy-write-for", "group-membership"):
                hrefs = [h.text for h in el.iter(f"{DAV_NS}href") if h.text]
                note(f"{name}: {', '.join(hrefs) or '-'}")
                if name == "calendar-home-set":
                    homes.extend(hrefs)
    for home in homes:
        url = str(client.url.join(home))
        note(f"Inhalt von {home}:")
        tree = propfind(url, "<d:displayname/><d:resourcetype/><cs:shared-url/><d:owner/>", depth=1)
        if tree is None:
            continue
        for resp in tree.iter(f"{DAV_NS}response"):
            href = next((h.text for h in resp.iter(f"{DAV_NS}href")), "?")
            dn = next((d.text for d in resp.iter(f"{DAV_NS}displayname")), None)
            rtypes = [short(c.tag) for rt in resp.iter(f"{DAV_NS}resourcetype") for c in rt]
            print(f"          {href}  name={dn!r}  typ={','.join(rtypes) or '-'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=30, help="look-ahead for events (default 30)")
    parser.add_argument("--write-test", action="store_true", help="create, read back and delete one probe object")
    parser.add_argument("--diagnose", action="store_true", help="list calendar homes, raw children and delegations")
    parser.add_argument("--test-calendar", default="Test", help='collection for --write-test (default "Test")')
    args = parser.parse_args()

    cfg = load_config()
    password = cfg["CALDAV_PASSWORD"]

    # 1 --- login and collections -------------------------------------------
    section("1  Anmeldung und Sammlungen")
    try:
        client = caldav.DAVClient(
            url=cfg["CALDAV_URL"],
            username=cfg["CALDAV_USER"],
            password=password,
            enable_rfc6764=False,
            timeout=30,
        )
        principal = client.principal()
        collections = principal.calendars()
    except Exception as exc:  # noqa: BLE001 - report anything, never crash with a traceback
        fail(f"Anmeldung/Abruf fehlgeschlagen: {safe_error(exc, password)}")
        return 1
    ok(f"angemeldet als {cfg['CALDAV_USER']}, {len(collections)} Sammlung(en) sichtbar")

    info = []
    for cal in collections:
        try:
            name = cal.get_display_name() or "(ohne Namen)"
        except Exception:  # noqa: BLE001
            name = "(Name nicht lesbar)"
        try:
            comps = sorted(cal.get_supported_components())
        except Exception as exc:  # noqa: BLE001
            comps = [f"? ({type(exc).__name__})"]
        try:
            privs = privileges(client, str(cal.url))
        except Exception as exc:  # noqa: BLE001
            privs = [f"? ({type(exc).__name__})"]
        writable = any(p in privs for p in ("write", "write-content", "all", "bind"))
        info.append((cal, name, comps))
        print(f"  -     {name}")
        note(f"Typen: {', '.join(comps) or '-'}")
        note(f"Rechte: {', '.join(privs) or '-'}  -> {'schreibbar' if writable else 'nur lesen'}")
        note(f"Pfad: {cal.url.path}")

    if args.diagnose:
        try:
            diagnose(client, principal)
        except Exception as exc:  # noqa: BLE001
            fail(f"Diagnose fehlgeschlagen: {safe_error(exc, password)}")

    # 2 --- events -------------------------------------------------------------
    section(f"2  Termine der nächsten {args.days} Tage (Wiederholungen aufgelöst)")
    start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=args.days)
    for cal, name, comps in info:
        if comps and "VEVENT" not in comps:
            continue
        try:
            events = cal.search(start=start, end=end, event=True, expand=True)
        except Exception as exc:  # noqa: BLE001
            fail(f"{name}: Suche fehlgeschlagen: {safe_error(exc, password)}")
            continue
        components = [e.component for e in events]
        components.sort(key=lambda c: str(c.get("DTSTART").dt) if c.get("DTSTART") else "")
        allday = sum(1 for c in components if c.get("DTSTART") and not isinstance(c.get("DTSTART").dt, datetime))
        series = sum(1 for c in components if c.get("RECURRENCE-ID") is not None or c.get("RRULE") is not None)
        ok(f"{name}: {len(components)} Termin(e), davon {allday} ganztägig, {series} aus Serien")
        for comp in components[:3]:
            note(describe_component(comp))

    # birthdays need a longer horizon to show that yearly recurrences expand
    for cal, name, comps in info:
        if "geburt" not in name.lower():
            continue
        try:
            year = cal.search(start=start, end=start + timedelta(days=366), event=True, expand=True)
            masters = cal.search(start=start, end=start + timedelta(days=366), event=True, expand=False)
        except Exception as exc:  # noqa: BLE001
            fail(f"{name}: Jahressuche fehlgeschlagen: {safe_error(exc, password)}")
            continue
        ok(f"{name}: nächste 12 Monate {len(year)} Vorkommen aus {len(masters)} gespeicherten Objekten")
        if masters and len(year) < len(masters):
            fail(f"{name}: weniger Vorkommen als Objekte - Auflösung der Serien prüfen")

    # 3 --- tasks ----------------------------------------------------------------
    section("3  Aufgaben (VTODO)")
    task_lists = [(cal, name) for cal, name, comps in info if "VTODO" in comps]
    if not task_lists:
        fail("keine Sammlung meldet VTODO - Erinnerungslisten nicht sichtbar")
    for cal, name in task_lists:
        try:
            todos = cal.search(todo=True, include_completed=True)
        except Exception as exc:  # noqa: BLE001
            fail(f"{name}: Aufgaben-Suche fehlgeschlagen: {safe_error(exc, password)}")
            continue
        open_count = sum(1 for t in todos if str(t.component.get("STATUS", "")).upper() != "COMPLETED")
        ok(f"{name}: {len(todos)} Aufgabe(n), davon {open_count} offen")
        for t in todos[:3]:
            note(describe_component(t.component))

    # 4 --- write test -------------------------------------------------------------
    section("4  Schreibtest")
    if not args.write_test:
        note("übersprungen (mit --write-test ausführen)")
    else:
        target = next((c for c, n, _ in info if n == args.test_calendar), None)
        comps = next((cs for c, n, cs in info if n == args.test_calendar), [])
        if target is None:
            fail(f'Sammlung "{args.test_calendar}" nicht gefunden')
        else:
            uid = PROBE_UID_PREFIX + uuid.uuid4().hex
            tomorrow = (datetime.now(TZ) + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
            cal_obj = ICalendar()
            cal_obj.add("prodid", "-//ksuite-calendar//probe//DE")
            cal_obj.add("version", "2.0")
            if "VEVENT" in comps or not comps:
                kind, item = "Termin", IEvent()
                item.add("dtstart", tomorrow)
                item.add("dtend", tomorrow + timedelta(minutes=15))
            else:
                kind, item = "Aufgabe", ITodo()
                item.add("due", tomorrow)
            item.add("uid", uid)
            item.add("summary", "ksuite-calendar Probe (wird sofort gelöscht)")
            item.add("dtstamp", datetime.now(TZ))
            cal_obj.add_component(item)
            created = None
            try:
                created = target.save_event(cal_obj.to_ical().decode()) if kind == "Termin" \
                    else target.save_todo(cal_obj.to_ical().decode())
                ok(f'{kind} in "{args.test_calendar}" angelegt (UID {uid[:len(PROBE_UID_PREFIX) + 8]}…)')
                again = target.get_object_by_uid(uid)
                ok(f"zurückgelesen: {again.component.get('SUMMARY')}")
            except Exception as exc:  # noqa: BLE001
                fail(f"Anlegen/Lesen fehlgeschlagen: {safe_error(exc, password)}")
            finally:
                if created is not None:
                    try:
                        created.delete()
                        try:
                            target.get_object_by_uid(uid)
                            fail("nach dem Löschen noch vorhanden")
                        except Exception:  # noqa: BLE001 - not found is the expected outcome
                            ok("wieder gelöscht und nicht mehr auffindbar")
                    except Exception as exc:  # noqa: BLE001
                        fail(f"Löschen fehlgeschlagen - bitte von Hand entfernen: {safe_error(exc, password)}")

    section("Ergebnis")
    if failures:
        print(f"  {len(failures)} Problem(e):")
        for f_ in failures:
            print(f"   - {f_}")
        return 1
    print("  alle Prüfungen bestanden")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    raise SystemExit(main())
