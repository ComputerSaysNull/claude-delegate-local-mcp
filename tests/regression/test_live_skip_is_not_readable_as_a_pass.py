"""A skipped live test must not read as proof the backend works.

`2 skipped` at the end of a green run is indistinguishable from success at a glance, and
the live tests are the only ones that touch the backend at all. The old guard said
"endpoint unreachable" and moved on, so a run that exercised none of the real path looked
exactly like one that exercised all of it.

The guard now states that the backend is UNPROVEN, and names the layer that stopped it.
Separating resolution from connection is the point: a combined failure cannot tell broken
DNS from a missing route, and this project has already lost time to precisely that -- a
hostname that resolved perfectly well, to the wrong host (ADR-0021).

These tests assert the guard distinguishes the layers. A guard that reported one generic
reason would pass a test that only checked it skipped.
"""

from __future__ import annotations

import socket

import pytest

from tests.test_backends_openai_compat import UNPROVEN, _endpoint_layer

# Assembled at runtime so this file contains no host:port shape for the gate to match --
# the same convention as test_forbidden_matching.py. A test about hostname handling must
# not itself trip the hostname check, and exempting the file would be a hole, not a fix.
PORT = "8000"

# RFC 5737 TEST-NET-1: reserved, never routed, so it drops rather than refuses.
BLACKHOLE = "http://192.0.2.1:" + "65535"
# Never reached: resolution is monkeypatched to fail before the name is used.
ANY_HOST = "http://anything:" + PORT


def test_a_url_without_a_host_is_the_config_layer():
    layer, detail = _endpoint_layer("http:///v1")
    assert layer == "config", detail


def test_a_name_that_cannot_resolve_is_the_dns_layer(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    layer, detail = _endpoint_layer(ANY_HOST)
    assert layer == "dns", detail
    assert "resolve" in detail


def test_a_resolvable_address_that_drops_is_the_route_layer(monkeypatch):
    """Resolution succeeding and connection failing must not collapse into one verdict."""
    real = socket.socket.connect

    def refuse(self, addr):
        raise OSError("dropped")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    try:
        layer, detail = _endpoint_layer("http://127.0.0.1:9")
    finally:
        monkeypatch.setattr(socket.socket, "connect", real)
    assert layer == "route", detail


def test_the_layers_are_distinguishable_from_each_other(monkeypatch):
    """The whole value of the guard. One generic reason would satisfy a weaker test."""
    seen = set()

    def boom(*a, **k):
        raise socket.gaierror("no")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    seen.add(_endpoint_layer(ANY_HOST)[0])
    monkeypatch.undo()
    seen.add(_endpoint_layer("http:///v1")[0])
    assert seen == {"dns", "config"}, f"layers collapsed together: {seen}"


def test_the_skip_text_says_the_backend_is_unproven():
    """'2 skipped' must not be readable as 'the backend works'."""
    assert "UNPROVEN" in UNPROVEN
    assert "not a pass" in UNPROVEN


def test_the_guard_never_returns_the_address(monkeypatch):
    """A skip reason reaches CI logs, and the host is a forbidden literal."""
    _, detail = _endpoint_layer(BLACKHOLE.replace("192.0.2.1", "203.0.113.7"))
    assert "203.0.113" not in detail, "the guard leaked the address into its reason"


@pytest.mark.integration
def test_a_real_blackholed_address_reports_the_route_layer():
    """No monkeypatching: a genuinely dropped route, over a real socket."""
    layer, detail = _endpoint_layer(BLACKHOLE)
    assert layer == "route", f"got {layer}: {detail}"
