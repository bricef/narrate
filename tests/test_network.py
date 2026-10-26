"""Captured pages reach only public addresses, unless the build allows local ones (#93).

Each test's target is a web server on this machine, which records what it's
asked for: refused, it must hear nothing at all.
"""

import asyncio
import os
import threading
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


@pytest.fixture(autouse=True)
def own_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("NARRATE_CACHE", str(tmp_path / "cache"))
    monkeypatch.delenv(network.ENV, raising=False)


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


def test_http_proxy_connects_to_the_checked_address(monkeypatch):
    class Writer:
        request = b""

        def write(self, data):
            self.request += data

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    writer = Writer()
    connection = None

    async def open_proxy(host, port, **kwargs):
        nonlocal connection
        connection = (host, port)
        reader = asyncio.StreamReader()
        reader.feed_data(b"HTTP/1.1 200 Connection established\r\n\r\n")
        reader.feed_eof()
        return reader, writer

    monkeypatch.setattr(network, "connect", open_proxy)

    async def connect():
        result_reader, result_writer = await network._through_proxy("http://proxy.example:8080", PUBLIC, 443)
        assert isinstance(result_reader, asyncio.StreamReader) and result_writer is writer

    asyncio.run(connect())
    assert connection == ("proxy.example", 8080)
    assert writer.request.startswith(f"CONNECT {PUBLIC}:443 HTTP/1.1\r\n".encode())


def test_proxy_selection_honours_http_https_and_no_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://http-proxy.example:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://https-proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", ".example.test")
    if os.name != "nt":
        monkeypatch.delenv("http_proxy", raising=False)
        monkeypatch.delenv("https_proxy", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)

    assert network.Guard._proxy("example.org", 80) == "http://http-proxy.example:8080"
    assert network.Guard._proxy("example.org", 443) == "http://https-proxy.example:8080"
    assert network.Guard._proxy("api.example.test", 443) is None


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


def frame(video: Path) -> np.ndarray:
    png = video.with_suffix(".png")
    media.extract_frame(video, 0.5, png)
    return np.asarray(Image.open(png).convert("RGB"), dtype=float)
