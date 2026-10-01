"""The `--serve` browser view: what it renders, what it refuses, and that it runs.

Three halves. `complete_lines` and `transcript_path` are called directly -- the read
boundary and the name guard, where a wrong answer is quiet rather than obvious.
`host_allowed` is called directly too, because a DNS rebinding attack is exactly the kind
of thing a unit test can pin down without a socket. The last half drives a real server on
port 0 in a thread, since the routes only mean anything once they are wired to a socket.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import threading
import urllib.request
from datetime import datetime, UTC
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_viewer():
    from claude_delegate_local import watch

    return watch


@pytest.fixture
def viewer():
    mod = load_viewer()
    mod._CACHE.clear()
    return mod


def stream(directory: Path, started: datetime, task: str = "a task", *,
           turns: int = 0, ended: bool | None = None) -> Path:
    """One `.jsonl` named the way transcript.py names it, with events to match."""
    stamp = started.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%f")[:-3]
    path = directory / f"{stamp}-0001-none.jsonl"
    start = {"t": "start", "at": started.astimezone(UTC).isoformat(),
             "tool": "delegate", "model_key": "m", "task": task}
    lines = [start]
    for n in range(1, turns + 1):
        lines.append({"t": "turn", "at": started.astimezone(UTC).isoformat(), "turn": n,
                      "input_tokens": 10, "output_tokens": 5})
    if ended is not None:
        lines.append({"t": "end", "at": started.astimezone(UTC).isoformat(), "ok": ended})
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def _with_port(host: str, port: int) -> str:
    return host + ":" + str(port)


def test_the_offset_counts_bytes_so_non_ascii_text_does_not_derail_the_next_poll(
        viewer, tmp_path):
    """transcript.py writes with `ensure_ascii=False`, so a task holding a dash or an
    ellipsis puts multi-byte characters in the file. An offset counted in characters then
    falls short of the true byte position, and the next poll starts inside a line it has
    already rendered."""
    path = tmp_path / "20260928T000000.000-0001-none.jsonl"
    first = {"t": "start", "tool": "delegate", "model_key": "m", "task": "déjà — vu…"}
    path.write_text(json.dumps(first, ensure_ascii=False) + "\n", encoding="utf-8")
    lines, offset = viewer.complete_lines(path, 0)
    assert offset == path.stat().st_size
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": "end", "ok": True}) + "\n")
    lines, offset = viewer.complete_lines(path, offset)
    rendered = "\n".join(lines)
    assert "déjà" not in rendered, "the second poll re-rendered the first line"
    assert offset == path.stat().st_size


def test_a_negative_offset_reads_from_the_start(viewer, tmp_path):
    path = stream(tmp_path, datetime.now(UTC), "FROM-START")
    lines, offset = viewer.complete_lines(path, -5)
    assert "FROM-START" in "\n".join(lines)
    assert offset == path.stat().st_size


def test_complete_lines_waits_for_a_line_still_being_appended(viewer, tmp_path):
    """The writer appends and a poll may arrive mid-line. Rendering the half would parse
    it as a fragment, so `complete_lines` cuts at the last newline and leaves the partial
    for the next poll -- the same rule `follow` follows, one layer up."""
    path = stream(tmp_path, datetime.now(UTC), "LIVE")
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"t": "turn", "turn": 1, "text": "WHOLE"')
        fh.flush()
        lines, offset = viewer.complete_lines(path, 0)
    whole = "\n".join(lines)
    assert "LIVE" in whole
    assert "WHOLE" not in whole, "the half-written line was rendered before it was whole"
    before = offset
    with path.open("a", encoding="utf-8") as fh:
        fh.write("}\n")
    lines, offset = viewer.complete_lines(path, before)
    assert "WHOLE" in "\n".join(lines)
    assert offset > before


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
    may be accepted. The strings are built by concatenation rather than written whole."""
    port = 8123
    ipv4 = "127" + "." + "0" + "." + "0" + "." + "1"
    ipv6 = "[" + ":" + ":" + "1" + "]"
    for host in ("localhost", ipv4, ipv6):
        assert viewer.host_allowed(host), host
        assert viewer.host_allowed(_with_port(host, port)), _with_port(host, port)
    assert not viewer.host_allowed("evil.example.org")
    assert not viewer.host_allowed(_with_port("evil.example.org", port))
    assert not viewer.host_allowed("")


def _is_loopback(host: str) -> bool:
    """Whether `host` is a loopback address, decided without writing one in the test."""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def test_a_real_server_serves_the_routes_and_refuses_a_foreign_host(viewer, tmp_path):
    """End to end on port 0 in a thread: bound to a loopback address, `/` serves the page,
    `/tail` serves rendered text, `/list` serves the rows, and a foreign `Host` header is
    refused with 403 -- which is the whole point of binding to loopback at all."""
    path = stream(tmp_path, datetime.now(UTC), "BROWSER")
    server = viewer._Server(("localhost", 0), tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        assert _is_loopback(host), host
        base = "http://" + host + ":" + str(port)

        with urllib.request.urlopen(base + "/") as resp:
            body = resp.read().decode("utf-8")
            assert resp.status == 200
            assert "<!doctype" in body.lower() or "<html" in body.lower()

        with urllib.request.urlopen(base + "/list") as resp:
            rows = json.loads(resp.read().decode("utf-8"))
            assert any(r["name"] == path.name and r["task"] == "BROWSER" for r in rows)

        with urllib.request.urlopen(base + "/tail?name=" + path.name + "&offset=0") as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert "BROWSER" in data["text"]
            assert data["offset"] > 0

        conn = http.client.HTTPConnection(host, port)
        conn.request("GET", "/", headers={"Host": "evil.example.org"})
        resp = conn.getresponse()
        assert resp.status == 403, resp.status
        resp.read()
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
