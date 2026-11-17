"""Captured pages reach only public addresses, unless the build allows local ones (#93), through an HTTP proxy
when one is set (#113).

Each test's target is a web server on this machine, which records what it's
asked for: refused, it must hear nothing at all. The proxies are on this
machine too, and no test reaches the internet.
"""

import asyncio
import base64
import ipaddress
import logging
import os
import socket
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from narrate import media, network
from narrate.build import Builder, BuildError, BuildResult
from narrate.script import parse_file
from narrate.tts import SilentSynth

PUBLIC = "93.184.215.14"  # stands for a site on the internet; the tests send its connections to a local server
PUBLIC6 = "2606:2800:21f:cb07:6820:80da:af6b:8b2c"  # and its IPv6 address, which no test connects to


class Site:
    """A web server on 127.0.0.1: `page` at /page.html, a redirect to `away` at /away, and a small answer
    to anything else. `asked` lists the paths requested."""

    def __init__(self, page: str = "<!doctype html><body style='background: #00f'>", away: str = ""):
        self.asked: list[str] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                site.asked.append(self.path)
                if self.path == "/away" and away:
                    self.send_response(302)
                    self.send_header("Location", away)
                    self.end_headers()
                    return
                body = page.encode() if self.path == "/page.html" else b"local"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def site():
    s = Site()
    yield s
    s.stop()


class ConnectProxy:
    """An HTTP CONNECT proxy on 127.0.0.1. `asked` lists each CONNECT's target, and `requests` each request
    whole; `to` maps a target's address to the one it really connects to, as the tests send PUBLIC to a
    server on this machine, and it answers 502 for any other. `answer` is how it answers, the same for every
    target or by address: "tunnel" (with the status line `opened`), a status such as 407, "close" (it closes
    without a word), "silence" (it never answers) or "nonsense" (not HTTP). `connections` counts the
    connections made to it, and `hung_up` is released for each the other side closed after an answer that
    opened no tunnel. A request for a URL, as a browser sends an HTTP proxy for an http:// page, is listed in
    `asked` too, and answered with a green page."""

    def __init__(self, to: dict[str, str] | None = None, answer: str | int | dict = "tunnel",
                 opened: str = "HTTP/1.1 200 Connection established"):
        self.asked: list[str] = []
        self.requests: list[str] = []
        self.connections = 0
        self.hung_up = threading.Semaphore(0)
        self.to = to or {}
        self.answer = answer
        self.opened = opened
        self.loop = asyncio.new_event_loop()
        started = threading.Event()

        async def start():
            self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
            started.set()

        threading.Thread(target=lambda: (self.loop.run_until_complete(start()), self.loop.run_forever()),
                         daemon=True).start()
        started.wait(5)
        self.port = self.server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"

    async def handle(self, reader, writer):
        self.connections += 1
        try:
            request = (await reader.readuntil(b"\r\n\r\n")).decode()
            self.requests.append(request)
            method, target = request.split()[:2]
            self.asked.append(target)
            if method != "CONNECT":
                body = b"<!doctype html><body style='background: #0f0'>"
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n"
                             b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
                return
            host, port = target.rsplit(":", 1)
            host = host.strip("[]")
            answer = self.answer.get(host, "tunnel") if isinstance(self.answer, dict) else self.answer
            if answer == "tunnel" and host not in self.to:
                answer = 502  # never the internet
            if answer == "tunnel":
                up_reader, up_writer = await asyncio.open_connection(self.to[host], int(port))
                writer.write(f"{self.opened}\r\n\r\n".encode())
                await network._splice(reader, writer, up_reader, up_writer)
                up_writer.close()
                return
            if answer == "close":
                return
            if answer == "nonsense":
                writer.write(b"SSH-2.0-OpenSSH_9.6\r\n\r\n")
            elif answer != "silence":
                writer.write(f"HTTP/1.1 {answer} Refused here\r\n\r\n".encode())
            await reader.read()  # until the other side hangs up
            self.hung_up.release()
        except (OSError, EOFError):
            pass
        finally:
            writer.close()

    def stop(self):
        self.loop.call_soon_threadsafe(self.server.close)
        self.loop.call_soon_threadsafe(self.loop.stop)


@pytest.fixture
def proxies():
    """Makes CONNECT proxies, `proxies(answer=407)`, and stops them after the test."""
    made: list[ConnectProxy] = []

    def make(**options) -> ConnectProxy:
        made.append(ConnectProxy(**options))
        return made[-1]

    yield make
    for proxy in made:
        proxy.stop()


LOOPBACK = ("127.0.0.1", "::1")


@pytest.fixture(autouse=True)
def outbound(monkeypatch):
    """Every connection Guard makes itself, as host:port. Only this machine's loopback addresses are
    connected to: anything else fails before it's looked up, as on a network where only a proxy reaches the
    internet, so no test can reach it, whatever the code it tests does. Names other than localhost aren't
    looked up either: a test gives its own addresses for them."""
    made: list[str] = []
    connect, resolve = network.connect, network.resolve

    async def only_here(host, port):
        made.append(f"[{host}]:{port}" if ":" in host else f"{host}:{port}")
        if host not in LOOPBACK:
            raise OSError("no route from the tests but to this machine")
        return await connect(host, port)

    def only_literals(host, port):
        try:
            ipaddress.ip_address(host.split("%")[0])
        except ValueError:
            if host != "localhost":
                raise OSError(f"the tests look up no names: {host}") from None
        return resolve(host, port)

    monkeypatch.setattr(network, "connect", only_here)
    monkeypatch.setattr(network, "resolve", only_literals)
    return made


def closed_port() -> int:
    """A port on 127.0.0.1 that nothing listens on."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def through_guard(host: str, port: int) -> tuple[int, bytes, list[network.Failure]]:
    """What Guard makes of a connection to host:port: its SOCKS reply, what a GET of /page.html brings back
    when it connected, and the failures it recorded."""
    guard = network.Guard()

    async def go():
        opened = await guard._open(host, port)
        if isinstance(opened, int):
            return opened, b""
        reader, writer = opened
        writer.write(b"GET /page.html HTTP/1.0\r\n\r\n")
        try:
            return network.GRANTED, await reader.read()
        finally:
            writer.close()

    reply, answer = asyncio.run(go())
    return reply, answer, guard.take()


@pytest.fixture(autouse=True)
def own_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("NARRATE_CACHE", str(tmp_path / "cache"))
    monkeypatch.delenv(network.ENV, raising=False)
    # No proxy but a test's own, whatever the shell running the tests has set: no *_PROXY variable, and on
    # macOS and Windows not the system's settings either. With REQUEST_METHOD set, as for a CGI script,
    # urllib ignores HTTP_PROXY
    for name in list(os.environ):
        if name.lower().endswith("_proxy") or name == "REQUEST_METHOD":
            monkeypatch.delenv(name)
    monkeypatch.setattr(urllib.request, "getproxies", urllib.request.getproxies_environment)
    monkeypatch.setattr(urllib.request, "proxy_bypass", urllib.request.proxy_bypass_environment)


def build(tmp_path: Path, body: str, name: str = "s", **options) -> BuildResult:
    script = tmp_path / f"{name}.md"
    script.write_text(f"---\nsize: 320x180\n---\n\n{body}\n\n(duration: 1)\n")
    return Builder(parse_file(script), SilentSynth(), log=lambda _: None, **options).build(tmp_path / f"{name}.mp4")


def web(url: str) -> str:
    return f"```render:web\n{url}\n```"


def refused(result: BuildResult, page: str, target: str) -> bool:
    return network.note(page, target) in result.notes


@pytest.mark.parametrize("address, public", [
    ("127.0.0.1", False), ("::1", False), ("0.0.0.0", False), ("::", False),
    ("10.1.2.3", False), ("172.16.0.1", False), ("192.168.1.1", False), ("fc00::1", False),
    ("169.254.169.254", False), ("fe80::1", False), ("100.64.0.1", False), ("224.0.0.1", False),
    ("::ffff:127.0.0.1", False), ("::127.0.0.1", False), ("64:ff9b::a9fe:a9fe", False),
    ("8.8.8.8", True), ("2001:4860:4860::8888", True), ("::ffff:8.8.8.8", True), ("64:ff9b::808:808", True),
])
def test_only_public_addresses_are_public(address, public):
    assert network.public(address) is public


def test_render_web_of_a_local_address_fails_and_names_it(tmp_path, site, monkeypatch):
    block = web(f"{site.url}/page.html")
    with pytest.raises(BuildError, match=rf"127\.0\.0\.1:{site.port} is a local address \(.*--allow-local-network"):
        build(tmp_path, block)
    assert site.asked == []
    # Allowed by the build, or by its environment, as the studio's builds take it
    build(tmp_path, block, name="allowed", local_network=True)
    assert site.asked == ["/page.html"]
    monkeypatch.setenv(network.ENV, "1")
    build(tmp_path, block, name="from-environment", regenerate=True)
    assert site.asked == ["/page.html"] * 2


def test_a_name_that_resolves_to_this_machine_is_refused(tmp_path, site, monkeypatch):
    # DNS rebinding: a name that looks public, answered with 127.0.0.1 when the page is loaded
    resolve = network.resolve
    monkeypatch.setattr(network, "resolve", lambda host, port: ["127.0.0.1"] if host == "rebind.example"
                        else resolve(host, port))
    with pytest.raises(BuildError, match=rf"rebind\.example:{site.port} \(127\.0\.0\.1\) is a local address"):
        build(tmp_path, web(f"http://rebind.example:{site.port}/page.html"))
    # And with the system's own resolver
    with pytest.raises(BuildError, match=rf"localhost:{site.port} \(.+\) is a local address"):
        build(tmp_path, web(f"http://localhost:{site.port}/page.html"), name="localhost")
    assert site.asked == []


def test_a_public_page_loads_but_not_what_it_points_to_here(tmp_path, site, monkeypatch):
    public = Site(f"<!doctype html><body style='background: #0f0'><img src='{site.url}/picture.png'>",
                  away=f"{site.url}/page.html")
    resolve, connect = network.resolve, network.connect
    monkeypatch.setattr(network, "resolve", lambda host, port: [PUBLIC] if host == "public.example"
                        else resolve(host, port))

    async def to_public(address, port):
        return await connect("127.0.0.1" if address == PUBLIC else address, port)

    monkeypatch.setattr(network, "connect", to_public)
    try:
        url = f"http://public.example:{public.port}/page.html"
        result = build(tmp_path, web(url))
        assert frame(result.video)[90, 160][1] > 200  # the page, through the proxy
        assert refused(result, url, f"127.0.0.1:{site.port}")
        # A redirect is a connection of its own, checked again
        with pytest.raises(BuildError, match=rf"127\.0\.0\.1:{site.port} is a local address"):
            build(tmp_path, web(f"http://public.example:{public.port}/away"), name="away")
        assert public.asked == ["/page.html", "/away"]
    finally:
        public.stop()
    assert site.asked == []


def test_an_html_scene_loads_its_own_files_but_no_local_address(tmp_path, site):
    (tmp_path / "look.css").write_text("html, body { margin: 0; height: 100%; background: #0f0; }")
    (tmp_path / "scene.html").write_text(f"""<!doctype html><link rel="stylesheet" href="look.css">
<img src="{site.url}/picture.png"><iframe src="{site.url}/frame.html"></iframe>
<script src="{site.url}/script.js"></script>
<script>
  fetch("{site.url}/fetch").catch(() => {{}});
  new WebSocket("ws://127.0.0.1:{site.port}/socket");
</script>""")
    result = build(tmp_path, "![](scene.html)")
    assert frame(result.video)[90, 300][1] > 200  # its stylesheet, from the project
    assert refused(result, "scene.html", f"127.0.0.1:{site.port}")
    assert site.asked == []
    build(tmp_path, "![](scene.html)", name="allowed", local_network=True)
    assert {"/picture.png", "/frame.html", "/script.js", "/fetch", "/socket"} <= set(site.asked)


def test_a_slide_with_html_reaches_no_local_address(tmp_path, site):
    slide = f'```md\n# Hello\n\n<img src="{site.url}/slide.png">\n```'
    result = build(tmp_path, slide)
    assert refused(result, "a slide", f"127.0.0.1:{site.port}")
    assert site.asked == []
    build(tmp_path, slide, name="allowed", local_network=True)
    assert site.asked == ["/slide.png"]


def test_a_build_asks_the_proxy_for_the_checked_address_only(tmp_path, site, outbound, monkeypatch):
    public = Site("<!doctype html><body style='background: #0f0'>")
    proxy = ConnectProxy(to={PUBLIC: "127.0.0.1"})
    monkeypatch.setenv("HTTP_PROXY", proxy.url)
    resolve = network.resolve
    monkeypatch.setattr(network, "resolve", lambda host, port: [PUBLIC] if host == "public.example"
                        else resolve(host, port))
    try:
        result = build(tmp_path, web(f"http://public.example:{public.port}/page.html"))
        assert frame(result.video)[90, 160][1] > 200  # the page, through both proxies
        assert proxy.asked == [f"{PUBLIC}:{public.port}"]  # the address narrate checked, not the name
        assert "public.example" not in "".join(proxy.requests)
        with pytest.raises(BuildError, match=rf"127\.0\.0\.1:{site.port} is a local address"):
            build(tmp_path, web(f"{site.url}/page.html"), name="local")
        assert proxy.asked == [f"{PUBLIC}:{public.port}"] and site.asked == []  # refused before the proxy
        assert f"{PUBLIC}:{public.port}" not in outbound  # never around the proxy
    finally:
        public.stop()
        proxy.stop()


@pytest.mark.parametrize("host", ["rebind.example", "127.0.0.1", "10.1.2.3", "169.254.169.254"])
def test_a_local_address_is_refused_before_the_proxy_hears_of_it(host, proxies, outbound, monkeypatch):
    proxy = proxies(to={PUBLIC: "127.0.0.1"})
    monkeypatch.setenv("HTTP_PROXY", proxy.url)
    resolve = network.resolve
    monkeypatch.setattr(network, "resolve", lambda name, port: ["127.0.0.1"] if name == "rebind.example"
                        else resolve(name, port))
    reply, _, failures = through_guard(host, 8080)
    assert reply == network.REFUSED and failures[0].local
    assert proxy.connections == 0 and outbound == []


def test_the_proxy_is_asked_for_each_checked_address_until_one_opens(site, proxies, outbound, monkeypatch):
    # A proxy without IPv6 refuses the IPv6 address, and the IPv4 one is tried next
    proxy = proxies(to={PUBLIC: "127.0.0.1"}, answer={PUBLIC6: 502})
    monkeypatch.setenv("HTTP_PROXY", proxy.url)
    monkeypatch.setattr(network, "resolve", lambda host, port: [PUBLIC6, PUBLIC])
    reply, answer, failures = through_guard("public.example", site.port)
    assert reply == network.GRANTED and b"background: #00f" in answer and failures == []
    assert proxy.asked == [f"[{PUBLIC6}]:{site.port}", f"{PUBLIC}:{site.port}"]
    # The whole request: the address as the Host too, and no headers but those (no Proxy-Connection)
    assert proxy.requests == [f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n"
                              for target in (f"[{PUBLIC6}]:{site.port}", f"{PUBLIC}:{site.port}")]
    assert outbound == [f"127.0.0.1:{proxy.port}"] * 2


@pytest.mark.parametrize("opened", ["HTTP/1.0 200 OK", "HTTP/1.1 204 No Content", "HTTP/1.1 299"])
def test_any_2xx_answer_opens_the_tunnel(opened, site, proxies, outbound, monkeypatch):
    proxy = proxies(to={PUBLIC: "127.0.0.1"}, opened=opened)  # RFC 9110, 9.3.6
    monkeypatch.setenv("HTTP_PROXY", proxy.url)
    reply, answer, failures = through_guard(PUBLIC, site.port)
    assert reply == network.GRANTED and b"background: #00f" in answer and failures == []


@pytest.mark.parametrize("answer, why", [
    (407, "answered 407 Proxy Authentication Required"),
    (403, "answered 403 Forbidden"),
    (502, "answered 502 Bad Gateway"),
    (599, "answered 599"),  # not a status HTTP names: its number alone
    ("nonsense", "answered with something that isn't HTTP"),
    ("close", "closed the connection without answering"),
])
def test_a_tunnel_the_proxy_doesnt_open_says_why_and_the_next_address_is_tried(answer, why, proxies, outbound,
                                                                               monkeypatch):
    proxy = proxies(answer=answer)
    monkeypatch.setenv("HTTP_PROXY", proxy.url)
    monkeypatch.setattr(network, "resolve", lambda host, port: [PUBLIC6, PUBLIC])
    reply, _, failures = through_guard("public.example", 8080)
    assert reply == network.UNREACHABLE
    assert proxy.asked == [f"[{PUBLIC6}]:8080", f"{PUBLIC}:8080"]  # the next address after each
    assert [str(network.Unreachable(f)) for f in failures] == [
        f"couldn't reach public.example:8080 (the proxy at 127.0.0.1:{proxy.port} {why})"]
    assert outbound == [f"127.0.0.1:{proxy.port}"] * 2  # never around the proxy
    if answer != "close":  # the proxy kept its side open: narrate closed each connection
        assert proxy.hung_up.acquire(timeout=5) and proxy.hung_up.acquire(timeout=5)


def test_a_proxy_that_doesnt_answer_in_time_ends_the_attempt(proxies, outbound, monkeypatch):
    # Waiting again for the next address would outlast Chromium, which then can't be told why
    proxy = proxies(answer="silence")
    monkeypatch.setenv("HTTP_PROXY", proxy.url)
    monkeypatch.setattr(network, "PROXY_TIMEOUT", 0.5)
    monkeypatch.setattr(network, "resolve", lambda host, port: [PUBLIC6, PUBLIC])
    reply, _, failures = through_guard("public.example", 8080)
    assert reply == network.UNREACHABLE and proxy.asked == [f"[{PUBLIC6}]:8080"]
    assert [str(network.Unreachable(f)) for f in failures] == [
        f"couldn't reach public.example:8080 (the proxy at 127.0.0.1:{proxy.port} didn't answer in 0.5 s)"]
    assert proxy.hung_up.acquire(timeout=5)  # narrate closed the connection it gave up on


def test_a_build_says_when_its_proxy_doesnt_answer(tmp_path, proxies, outbound, monkeypatch):
    proxy = proxies(answer="silence")
    monkeypatch.setenv("HTTP_PROXY", proxy.url)
    monkeypatch.setattr(network, "PROXY_TIMEOUT", 2)
    monkeypatch.setattr(network, "resolve", lambda host, port: [PUBLIC])
    with pytest.raises(BuildError, match=rf"couldn't reach public\.example:8080 \(the proxy at 127\.0\.0\.1:"
                                         rf"{proxy.port} didn't answer in 2 s\)"):
        build(tmp_path, web("http://public.example:8080/page.html"))


def test_a_proxy_is_given_up_on_well_before_chromium_gives_up_on_the_guard():
    # Chromium gives up on its SOCKS handshake with the guard after 30 s (kSOCKSConnectJobTimeoutInSeconds),
    # counted from before the guard looks the name up. Only a failure the guard records before then reaches
    # the build: after it, Chromium reports net::ERR_TIMED_OUT, with no word of the proxy
    chromium_gives_up = 30
    assert network.PROXY_TIMEOUT <= chromium_gives_up - 10


def test_a_proxy_that_cant_be_reached_ends_the_attempt(outbound, monkeypatch):
    port = closed_port()
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(network, "resolve", lambda host, port: [PUBLIC6, PUBLIC])
    reply, _, failures = through_guard("public.example", 8080)
    assert reply == network.UNREACHABLE
    assert outbound == [f"127.0.0.1:{port}"]  # once: no other address would get further
    assert str(network.Unreachable(failures[0])) == \
        f"couldn't reach public.example:8080 (couldn't connect to the proxy at 127.0.0.1:{port})"


def test_no_proxy_names_connect_directly_and_are_still_checked(site, proxies, monkeypatch):
    proxy = proxies(to={PUBLIC: "127.0.0.1"})
    monkeypatch.setenv("HTTP_PROXY", proxy.url)
    monkeypatch.setenv("NO_PROXY", "public.example, .rebind.example")
    addresses = {"public.example": [PUBLIC], "www.rebind.example": ["127.0.0.1"], "elsewhere.example": [PUBLIC]}
    monkeypatch.setattr(network, "resolve", lambda host, port: addresses[host])
    direct, connect = [], network.connect

    async def to_public(address, port):
        direct.append(f"{address}:{port}")
        return await connect("127.0.0.1" if address == PUBLIC else address, port)

    monkeypatch.setattr(network, "connect", to_public)
    reply, answer, _ = through_guard("public.example", site.port)
    assert reply == network.GRANTED and b"background: #00f" in answer
    assert direct == [f"{PUBLIC}:{site.port}"] and proxy.connections == 0  # the checked address, not the proxy
    reply, _, failures = through_guard("www.rebind.example", site.port)
    assert reply == network.REFUSED and failures == [network.Failure(f"www.rebind.example:{site.port} (127.0.0.1)",
                                                                      True)]
    assert len(direct) == 1 and proxy.connections == 0 and site.asked == ["/page.html"]
    # A name NO_PROXY doesn't list goes through the proxy
    reply, _, _ = through_guard("elsewhere.example", site.port)
    assert reply == network.GRANTED and proxy.asked == [f"{PUBLIC}:{site.port}"]


@pytest.mark.parametrize("variables", [
    {},
    {"ALL_PROXY": "http://127.0.0.1:{port}"},
    {"all_proxy": "http://127.0.0.1:{port}"},
    {"HTTP_PROXY": "socks5://127.0.0.1:{port}"},
    {"http_proxy": "socks5h://127.0.0.1:{port}", "HTTPS_PROXY": "socks4://127.0.0.1:{port}"},
    {"NO_PROXY": "example.com"},
])
def test_without_an_http_proxy_connections_go_direct_as_before(variables, site, proxies, monkeypatch):
    proxy = proxies(to={PUBLIC: "127.0.0.1"})
    for name, value in variables.items():
        monkeypatch.setenv(name, value.format(port=proxy.port))
    calls, connect = [], network.connect

    async def to_public(*args, **kwargs):
        calls.append((args, kwargs))
        return await connect("127.0.0.1" if args[0] == PUBLIC else args[0], *args[1:], **kwargs)

    monkeypatch.setattr(network, "connect", to_public)
    reply, answer, failures = through_guard(PUBLIC, site.port)
    assert reply == network.GRANTED and b"background: #00f" in answer and failures == []
    assert calls == [((PUBLIC, site.port), {})]  # the checked address itself, as before
    assert proxy.connections == 0


def test_https_proxy_serves_port_443_and_http_proxy_the_others(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://secure.example:3129")
    monkeypatch.setenv("HTTP_PROXY", "plain.example:3128")  # no scheme: http://, as curl and Chromium read it
    assert network.proxy_for("example.com", 443) == network.Proxy("secure.example", 3129)
    assert network.proxy_for("example.com", 80) == network.Proxy("plain.example", 3128)
    assert network.proxy_for("example.com", 8443) == network.Proxy("plain.example", 3128)
    monkeypatch.setenv("http_proxy", "http://lower.example")  # lower case wins; port 80 when it names none
    assert network.proxy_for("example.com", 80) == network.Proxy("lower.example", 80)
    monkeypatch.setenv("NO_PROXY", ".internal.example, example.org")
    assert network.proxy_for("www.example.org", 443) is None
    assert network.proxy_for("example.org", 80) is None
    assert network.proxy_for("example.com", 443) == network.Proxy("secure.example", 3129)


def test_an_https_proxy_is_refused_and_says_so(proxies, outbound, monkeypatch):
    # TLS to the proxy itself isn't supported: the build says so, rather than going around it
    proxy = proxies(to={PUBLIC: "127.0.0.1"})
    monkeypatch.setenv("HTTPS_PROXY", f"https://127.0.0.1:{proxy.port}")
    reply, _, failures = through_guard(PUBLIC, 443)
    assert reply == network.UNREACHABLE
    assert str(network.Unreachable(failures[0])) == (
        f"couldn't reach {PUBLIC}:443 (the proxy at 127.0.0.1:{proxy.port} is https://: "
        "narrate supports only http:// proxies)")
    assert proxy.connections == 0 and outbound == []


USER, PASSWORD = "narrator", "Xy7@s3cret/Qk9"  # a password with characters a URL must escape
TOKEN = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()  # as Proxy-Authorization carries them


def told_nothing_of_the_credentials(told: str) -> bool:
    """Whether `told` holds no part of the user name or password, and not their Base64 form either."""
    return not any(part in told for part in (USER, "Xy7", "s3cret", "Qk9", TOKEN))


def test_proxy_credentials_reach_the_proxy_and_nothing_else(tmp_path, proxies, outbound, monkeypatch, caplog):
    proxy = proxies(answer=407)
    monkeypatch.setenv("HTTP_PROXY", f"http://{USER}:Xy7%40s3cret%2FQk9@127.0.0.1:{proxy.port}")
    monkeypatch.setattr(network, "resolve", lambda host, port: [PUBLIC])
    caplog.set_level(logging.DEBUG)
    script = tmp_path / "s.md"
    script.write_text(f"---\nsize: 320x180\n---\n\n{web('http://public.example:8080/page.html')}\n\n(duration: 1)\n")
    lines: list[str] = []
    with pytest.raises(BuildError) as raised:
        Builder(parse_file(script), SilentSynth(), log=lines.append).build(tmp_path / "s.mp4")
    told = "\n".join([str(raised.value), *lines, caplog.text, repr(network.proxy_for("public.example", 8080))])
    assert f"(the proxy at 127.0.0.1:{proxy.port} answered 407 Proxy Authentication Required)" in told
    assert told_nothing_of_the_credentials(told)
    assert proxy.requests and set(proxy.requests) == {
        f"CONNECT {PUBLIC}:8080 HTTP/1.1\r\nHost: {PUBLIC}:8080\r\nProxy-Authorization: Basic {TOKEN}\r\n\r\n"}


@pytest.mark.parametrize("url, why", [
    ("http://narrator:Xy7%40s3cret%2FQk9@127.0.0.1:{port}", "couldn't connect to the proxy at 127.0.0.1:{port}"),
    ("https://narrator:Xy7%40s3cret%2FQk9@127.0.0.1:{port}",
     "the proxy at 127.0.0.1:{port} is https://: narrate supports only http:// proxies"),
    # Characters left unescaped: what follows the first /, ? or # would be taken for the host
    ("http://narrator:Xy7@s3cret/Qk9@127.0.0.1:{port}", "narrate can't read the proxy's URL"),
    ("http://narrator:Xy7@s3cret?Qk9@127.0.0.1:{port}", "narrate can't read the proxy's URL"),
    ("http://narrator:Xy7@s3cret#Qk9@127.0.0.1:{port}", "narrate can't read the proxy's URL"),
    ("http://narrator:Xy7/s3cretQk9@127.0.0.1:{port}", "narrate can't read the proxy's URL"),
    ("http://narrator:Xy7s3cretQk9@127.0.0.1:port", "narrate can't read the proxy's URL"),
])
def test_a_proxy_that_cant_be_used_never_shows_its_credentials(url, why, outbound, monkeypatch, caplog):
    port = closed_port()
    monkeypatch.setenv("HTTP_PROXY", url.format(port=port))
    caplog.set_level(logging.DEBUG)
    reply, _, failures = through_guard(PUBLIC, 8080)
    assert reply == network.UNREACHABLE
    shown = str(network.Unreachable(failures[0]))
    assert shown == f"couldn't reach {PUBLIC}:8080 ({why.format(port=port)})"
    assert told_nothing_of_the_credentials(shown + caplog.text)
    # Nothing looked up or connected to but the proxy, and only when its URL could be read
    assert outbound == ([f"127.0.0.1:{port}"] if why.startswith("couldn't connect") else [])


@pytest.mark.parametrize("trouble, why", [
    ("refuses", "the proxy at 127.0.0.1:{port} answered 407 Proxy Authentication Required"),
    ("is down", "couldn't connect to the proxy at 127.0.0.1:{port}"),
    ("is https", "the proxy at 127.0.0.1:{port} is https://: narrate supports only http:// proxies"),
    ("is unreadable", "narrate can't read the proxy's URL"),
])
def test_what_a_proxy_kept_from_a_page_is_noted_once(trouble, why, tmp_path, proxies, outbound, monkeypatch):
    proxy = proxies(answer=407)
    port = proxy.port if trouble == "refuses" else closed_port()
    scheme, password = ("https" if trouble == "is https" else "http",
                        "Xy7@s3cret/Qk9" if trouble == "is unreadable" else "Xy7%40s3cret%2FQk9")
    monkeypatch.setenv("HTTP_PROXY", f"{scheme}://{USER}:{password}@127.0.0.1:{port}")
    resolve = network.resolve
    monkeypatch.setattr(network, "resolve", lambda host, port: [PUBLIC] if host == "public.example"
                        else resolve(host, port))
    (tmp_path / "scene.html").write_text("""<!doctype html>
<link rel="stylesheet" href="http://public.example:8080/look.css">
<body style="margin: 0; background: #00f"><img src="http://public.example:8080/picture.png">""")
    result = build(tmp_path, "![](scene.html)")  # the scene builds, without what the proxy kept out
    assert [n for n in result.notes if "proxy" in n] == [network.proxy_note(why.format(port=port))]
    assert told_nothing_of_the_credentials("\n".join(result.notes))
    if trouble == "refuses":  # once, though the proxy refused both
        assert set(proxy.asked) == {f"{PUBLIC}:8080"} and len(proxy.asked) >= 2


@pytest.mark.skipif(sys.platform != "linux", reason="Chromium reads the proxy variables on Linux; elsewhere it "
                                                    "reads the system's settings")
@pytest.mark.parametrize("variable", ["HTTP_PROXY", "ALL_PROXY"])
def test_with_local_addresses_allowed_chromium_uses_the_proxy_itself_but_not_for_this_machine(
        variable, tmp_path, site, proxies, monkeypatch):
    # The guard is off, as on main, and Chromium reads the proxy variables itself, ALL_PROXY too. It sends a
    # name to the proxy, and this machine's own address straight there
    from narrate import doctor
    launch, launched = doctor.launch_chromium, []

    def resolving_nothing(playwright, args):
        # Were Chromium not to use the proxy, it couldn't look the name up and go out to the internet either
        launched.append(args := [*args, "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1"])
        return launch(playwright, args)

    monkeypatch.setattr(doctor, "launch_chromium", resolving_nothing)
    for name in ("XDG_CURRENT_DESKTOP", "DESKTOP_SESSION", "KDE_FULL_SESSION", "GNOME_DESKTOP_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)  # a desktop's own proxy settings would come before the variables
    proxy = proxies()
    monkeypatch.setenv(variable, proxy.url)
    build(tmp_path, web(f"{site.url}/page.html"), local_network=True)
    assert launched and site.asked == ["/page.html"] and proxy.asked == []  # this machine: straight there
    result = build(tmp_path, web("http://public.example/page.html"), name="named", local_network=True)
    assert proxy.asked == ["http://public.example/page.html"]  # a name: through the proxy
    assert frame(result.video)[90, 160][1] > 200  # the proxy's green page


def frame(video: Path) -> np.ndarray:
    png = video.with_suffix(".png")
    media.extract_frame(video, 0.5, png)
    return np.asarray(Image.open(png).convert("RGB"), dtype=float)
