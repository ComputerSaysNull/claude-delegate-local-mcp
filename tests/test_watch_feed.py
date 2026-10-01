"""The `--feed`: raw transcript events for a reader on another machine, and what it refuses.

Three halves. `complete_events` and `transcript_path` are called directly -- the read
boundary and the name guard, where a wrong answer is quiet rather than obvious.
`host_allowed` is called directly too, because a DNS rebinding attack is exactly the kind
of thing a unit test can pin down without a socket. The last half drives a real server on
port 0 in a thread, since the routes only mean anything once they are wired to a socket.

The feed replaced a browser page (ADR-0112): the web viewer lives in its own repository and
reads events, so the feed hands over events and renders nothing.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, UTC
from pathlib import Path

import jsonschema
import pytest

from claude_delegate_local import transcript

SCHEMA = json.loads(Path(transcript.__file__).with_name("transcript.schema.json")
                    .read_text(encoding="utf-8"))


@pytest.fixture
def viewer():
    from claude_delegate_local import watch

    watch._CACHE.clear()
    return watch


def stream(directory: Path, started: datetime, task: str = "a task", *,
           turns: int = 0, ended: bool | None = None) -> Path:
    """One `.jsonl` named the way transcript.py names it, with events to match."""
    stamp = started.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%f")[:-3]
    path = directory / f"{stamp}-0001-none.jsonl"
    at = started.astimezone(UTC).isoformat()
    start = {"t": "start", "at": at, "format": transcript.FORMAT, "tool": "delegate",
             "task": task, "agent": None, "model_key": "m", "effort": None,
             "max_turns": None, "tools": []}
    lines = [start]
    for n in range(1, turns + 1):
        lines.append({"t": "turn", "at": at, "turn": n, "tool_calls": [], "text": "",
                      "input_tokens": 10, "output_tokens": 5})
    if ended is not None:
        lines.append({"t": "end", "at": at, "ok": ended, "turns": turns,
                      "elapsed_seconds": 1.0})
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def _with_port(host: str, port: int) -> str:
    return host + ":" + str(port)


def test_the_offset_counts_bytes_so_non_ascii_text_does_not_derail_the_next_poll(
        viewer, tmp_path):
    """transcript.py writes with `ensure_ascii=False`, so a task holding a dash or an
    ellipsis puts multi-byte characters in the file. An offset counted in characters then
    falls short of the true byte position, and the next poll starts inside a line it has
    already returned."""
    path = tmp_path / "20260928T000000.000-0001-none.jsonl"
    first = {"t": "start", "tool": "delegate", "model_key": "m", "task": "déjà — vu…"}
    path.write_text(json.dumps(first, ensure_ascii=False) + "\n", encoding="utf-8")
    events, offset, _ = viewer.complete_events(path, 0)
    assert events == [first]
    assert offset == path.stat().st_size
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": "end", "ok": True}) + "\n")
    events, offset, _ = viewer.complete_events(path, offset)
    assert events == [{"t": "end", "ok": True}], "the second poll returned the first line again"
    assert offset == path.stat().st_size


def test_a_negative_offset_reads_from_the_start(viewer, tmp_path):
    path = stream(tmp_path, datetime.now(UTC), "FROM-START")
    events, offset, _ = viewer.complete_events(path, -5)
    assert events[0]["task"] == "FROM-START"
    assert offset == path.stat().st_size


def test_complete_events_waits_for_a_line_still_being_appended(viewer, tmp_path):
    """The writer appends and a poll may arrive mid-line. Parsing the half would hand over
    a fragment, so the read cuts at the last newline and leaves the partial for the next
    poll -- the same rule `follow` follows."""
    path = stream(tmp_path, datetime.now(UTC), "LIVE")
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"t": "turn", "turn": 1, "text": "WHOLE"')
        fh.flush()
        events, offset, _ = viewer.complete_events(path, 0)
    assert [e["t"] for e in events] == ["start"], "the half-written line was handed over"
    before = offset
    with path.open("a", encoding="utf-8") as fh:
        fh.write("}\n")
    events, offset, _ = viewer.complete_events(path, before)
    assert events == [{"t": "turn", "turn": 1, "text": "WHOLE"}]
    assert offset > before


def test_one_poll_is_capped_and_the_next_carries_on(viewer, tmp_path, monkeypatch):
    """A long delegation's stream runs to tens of megabytes, and reading the remainder in
    one go held all of it in memory per request, on a threaded server. A poll now returns
    at most a chunk of whole lines and says there is more."""
    monkeypatch.setattr(viewer, "FEED_CHUNK_BYTES", 200)
    path = tmp_path / "20260928T000000.000-0001-none.jsonl"
    lines = [{"t": "turn", "turn": n, "text": "x" * 40} for n in range(10)]
    path.write_text("".join(json.dumps(e) + "\n" for e in lines), encoding="utf-8")
    got, offset, more = [], 0, True
    polls = 0
    while more:
        events, offset, more = viewer.complete_events(path, offset)
        got.extend(events)
        polls += 1
        assert polls < 50, "the feed stopped making progress"
    assert got == lines
    assert polls > 1, "nothing was capped"
    assert offset == path.stat().st_size


def test_a_line_longer_than_the_cap_is_still_delivered_whole(viewer, tmp_path, monkeypatch):
    """Cutting a line would hand over a fragment, and stopping before it would never get
    past it: one line over the cap is returned whole, on its own."""
    monkeypatch.setattr(viewer, "FEED_CHUNK_BYTES", 50)
    path = tmp_path / "20260928T000000.000-0001-none.jsonl"
    big = {"t": "turn", "turn": 1, "text": "y" * 500}
    path.write_text(json.dumps(big) + "\n" + json.dumps({"t": "end", "ok": True}) + "\n",
                    encoding="utf-8")
    events, offset, more = viewer.complete_events(path, 0)
    assert events == [big]
    assert more
    events, offset, more = viewer.complete_events(path, offset)
    assert events == [{"t": "end", "ok": True}]
    assert not more


def test_a_name_holding_a_null_byte_is_refused_rather_than_raising(viewer, tmp_path):
    assert viewer.transcript_path(tmp_path, "x\x00.jsonl") is None


def test_an_empty_allowed_name_does_not_admit_an_empty_host(viewer):
    """`--allow-host ""` would otherwise match the empty header a request without `Host`
    is read as."""
    assert not viewer.host_allowed("", ("",))
    assert not viewer.host_allowed("", (" ",))


def test_a_line_that_is_not_an_event_is_passed_over(viewer, tmp_path):
    path = tmp_path / "20260928T000000.000-0001-none.jsonl"
    path.write_text('[1, 2]\nnot json\n{"t": "end", "ok": true}\n', encoding="utf-8")
    events, _, _ = viewer.complete_events(path, 0)
    assert events == [{"t": "end", "ok": True}]


def test_a_bad_name_is_refused_and_a_real_one_served(viewer, tmp_path):
    """`name` arrives from a URL, and this server exists to read transcripts a caller is
    not meant to reach. Separators, traversal and a wrong extension all refuse; a bare
    file name inside the directory is served."""
    path = stream(tmp_path, datetime.now(UTC), "SERVED")
    assert viewer.transcript_path(tmp_path, path.name) == path.resolve()
    for bad in ("../x.jsonl", "sub/x.jsonl", "x.txt", str(path)):
        assert viewer.transcript_path(tmp_path, bad) is None, bad


def test_host_allowed_accepts_the_loopback_names_and_nothing_else(viewer):
    """The `Host` header is what a rebinding attack rewrites, so only the loopback names
    may be accepted by default. The strings are built by concatenation rather than written
    whole."""
    port = 8123
    ipv4 = "127" + "." + "0" + "." + "0" + "." + "1"
    ipv6 = "[" + ":" + ":" + "1" + "]"
    for host in ("localhost", ipv4, ipv6):
        assert viewer.host_allowed(host), host
        assert viewer.host_allowed(_with_port(host, port)), _with_port(host, port)
    assert not viewer.host_allowed("evil.example.org")
    assert not viewer.host_allowed(_with_port("evil.example.org", port))
    assert not viewer.host_allowed("")


def test_a_name_the_operator_allows_is_accepted_and_only_that_name(viewer):
    """What the overlay VPN's own proxy sends as `Host` is the machine's name on it, so
    the operator names it; naming one must not open any other."""
    allowed = ("viewer-host.example.org",)
    assert viewer.host_allowed("viewer-host.example.org", allowed)
    assert viewer.host_allowed(_with_port("VIEWER-HOST.example.org", 443), allowed)
    assert not viewer.host_allowed("other.example.org", allowed)
    assert not viewer.host_allowed("viewer-host.example.org"), "allowed without being named"


def _is_loopback(host: str) -> bool:
    """Whether `host` is a loopback address, decided without writing one in the test."""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _get(host: str, port: int, path: str, header_host: str | None = None):
    conn = http.client.HTTPConnection(host, port)
    conn.request("GET", path, headers={"Host": header_host} if header_host else {})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, body


@pytest.fixture
def running(viewer, tmp_path):
    servers = []

    def start(allowed: tuple[str, ...] = ()):
        server = viewer._Server(("localhost", 0), tmp_path, allowed)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return server.server_address

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join()


def test_a_real_feed_lists_and_serves_raw_events(tmp_path, running):
    """End to end on port 0: bound to loopback, `/list` names each transcript and its
    format, and `/events` hands over the events themselves, which pass the schema."""
    path = stream(tmp_path, datetime.now(UTC), "FEED", turns=1, ended=True)
    host, port = running()
    assert _is_loopback(host), host
    base = "http://" + host + ":" + str(port)

    with urllib.request.urlopen(base + "/list") as resp:
        listing = json.loads(resp.read().decode("utf-8"))
    assert listing["format"] == transcript.FORMAT
    row = next(r for r in listing["transcripts"] if r["name"] == path.name)
    assert row["task"] == "FEED"
    assert row["format"] == transcript.FORMAT

    with urllib.request.urlopen(base + "/events?name=" + path.name + "&offset=0") as resp:
        data = json.loads(resp.read().decode("utf-8"))
    assert [e["t"] for e in data["events"]] == ["start", "turn", "end"]
    assert data["offset"] == path.stat().st_size
    assert data["more"] is False
    validator = jsonschema.Draft202012Validator(SCHEMA)
    assert [list(validator.iter_errors(e)) for e in data["events"]] == [[], [], []]


def test_the_page_and_the_rendered_tail_are_gone(tmp_path, running):
    host, port = running()
    for gone in ("/", "/tail?name=x.jsonl&offset=0"):
        status, _ = _get(host, port, gone)
        assert status == 404, gone


def test_a_foreign_host_is_refused_and_an_allowed_one_served(tmp_path, running):
    """The negative control is the same request, to a feed that was not told the name."""
    stream(tmp_path, datetime.now(UTC), "X")
    host, port = running(("viewer-host.example.org",))
    assert _get(host, port, "/list", "viewer-host.example.org")[0] == 200
    assert _get(host, port, "/list", "evil.example.org")[0] == 403

    host, port = running()
    assert _get(host, port, "/list", "viewer-host.example.org")[0] == 403


def test_the_server_header_names_no_interpreter_version(tmp_path, running):
    host, port = running()
    conn = http.client.HTTPConnection(host, port)
    conn.request("GET", "/list")
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert "Python" not in (resp.getheader("Server") or "")


def test_the_feed_answers_get_only(tmp_path, running):
    host, port = running()
    conn = http.client.HTTPConnection(host, port)
    conn.request("POST", "/list", body=b"{}")
    status = conn.getresponse().status
    conn.close()
    assert status == 501


def test_an_unknown_transcript_is_a_404(tmp_path, running):
    host, port = running()
    base = "http://" + host + ":" + str(port)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(base + "/events?name=missing.jsonl&offset=0")
    assert e.value.code == 404
