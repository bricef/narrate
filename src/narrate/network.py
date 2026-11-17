"""What a build's Chromium may reach (#93): public addresses only, unless the build allows local ones.

Otherwise a captured page (a render:web page, an HTML scene, an overlay, a
slide's HTML) could load a studio or service on this machine, a router or a
cloud metadata address, and show what it answers in the video.

Chromium sends every connection through Guard, a SOCKS5 proxy in this process:
- the proxy resolves each host name itself and connects only to a public
  address it resolved, so there's no second lookup that DNS rebinding could
  answer differently. Every connection Chromium opens, for a page, a
  redirect, a frame, a fetch or a socket, is checked anew;
- Chromium resolves no names itself (--host-resolver-rules), and sends
  loopback and link-local addresses to the proxy too (<-loopback>), rather
  than straight there.
Files (file://) don't go through it, so pages load their project files as before.

Behind an HTTP proxy (#113), Guard goes out through it (proxy_for): HTTPS_PROXY
for port 443, HTTP_PROXY for the others, and NO_PROXY, as urllib reads them.
It still resolves and checks every address itself, then asks the proxy for an
HTTP CONNECT tunnel to the address it checked, never the name, so the proxy
can't resolve the name differently. A proxy hears nothing of a local address.
Only http:// proxies are used: ALL_PROXY, and other schemes such as socks5://,
are ignored, and an https:// one is refused. Names must resolve on this
machine: letting the proxy resolve them is #132.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import os
import socket
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http import HTTPStatus

FLAG = "--allow-local-network"
ENV = "NARRATE_ALLOW_LOCAL_NETWORK"
ALLOW = f"builds reach only public addresses, unless {FLAG} allows local ones"
CONNECT_TIMEOUT = 30  # seconds

NAT64 = ipaddress.ip_network("64:ff9b::/96")
# SOCKS5 (RFC 1928): the CONNECT command, address types, and replies
CONNECT, IPV4, NAME, IPV6 = 1, 1, 3, 4
GRANTED, REFUSED, UNREACHABLE, UNSUPPORTED = 0, 2, 4, 7


def allowed_by_environment() -> bool:
    return os.environ.get(ENV, "").strip().lower() in ("1", "true", "yes")


def public(address: str) -> bool:
    """Whether an address is on the public internet: not loopback, private, link-local, shared (CGNAT),
    reserved or multicast. An IPv6 address that carries an IPv4 one is judged by that."""
    ip = ipaddress.ip_address(address.split("%")[0])
    if ip.version == 6:
        if ip.ipv4_mapped:
            return public(str(ip.ipv4_mapped))
        if ip in NAT64 or int(ip) >> 32 == 0:  # NAT64, and the old IPv4-compatible form (::1 too)
            return public(str(ipaddress.IPv4Address(int(ip) & 0xFFFF_FFFF)))
    return ip.is_global and not ip.is_multicast


def resolve(host: str, port: int) -> list[str]:
    """The addresses `host` resolves to, in the system's order (an address resolves to itself)."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


connect = asyncio.open_connection  # the proxy's way out to an address it has checked, or to an HTTP proxy


@dataclass(frozen=True)
class Failure:
    """A connection the proxy didn't make: `target` is host:port, with the address it resolved to when the
    host is a name. `local` when it was refused as a local address; otherwise it found no address, or none
    answered. `why` says what an HTTP proxy did instead, when there is one."""

    target: str
    local: bool
    why: str = ""


class Unreachable(Exception):
    """A page that couldn't be reached itself, as the proxy saw it."""

    def __init__(self, failure: Failure):
        super().__init__(f"{failure.target} is a local address ({ALLOW})" if failure.local
                         else f"couldn't reach {failure.target}" + (f" ({failure.why})" if failure.why else ""))
        self.failure = failure


@dataclass(frozen=True)
class Proxy:
    """An HTTP proxy to tunnel through. It shows as host:port: the credentials in its URL go to it, in
    `authorization`, and nowhere else."""

    host: str
    port: int
    authorization: str | None = field(default=None, repr=False)  # Proxy-Authorization's value

    def __str__(self) -> str:
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"


class ProxyError(Exception):
    """The proxy can't be used or reached, so no address will do. Its message never holds credentials."""


class TunnelError(Exception):
    """The proxy didn't open a tunnel to one address. Its message never holds credentials."""


def proxy_for(host: str, port: int) -> Proxy | None:
    """The HTTP proxy to reach `host` through, as urllib finds it: HTTPS_PROXY for port 443, HTTP_PROXY for the
    others, and on macOS and Windows the system's settings when no *_PROXY variable is set. None to connect
    directly, as without a proxy: there's none, NO_PROXY names `host`, or it isn't http:// (ALL_PROXY isn't
    read, and socks5:// and other schemes are ignored). Raises ProxyError for an https:// proxy, which
    narrate doesn't support, and for a URL it can't read. May query the system: call it off the loop."""
    url = urllib.request.getproxies().get("https" if port == 443 else "http")
    if not url or urllib.request.proxy_bypass(host):
        return None
    if "://" not in url:
        url = "http://" + url  # proxy.example:3128, as curl and Chromium read it
    scheme = url.split("://", 1)[0].lower()
    if scheme not in ("http", "https"):
        return None
    unreadable = ProxyError("narrate can't read the proxy's URL")  # its text could hold a password: never shown
    try:
        parts = urllib.parse.urlsplit(url)
        name, number = parts.hostname, parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise unreadable from None
    # user:pa/ss@host would take "user" for the host: an @ must end the host's part of the URL
    if not name or ("@" in url and "@" not in parts.netloc):
        raise unreadable
    if scheme == "https":
        raise ProxyError(f"the proxy at {Proxy(name, number)} is https://: narrate supports only http:// proxies")
    authorization = None
    if parts.username is not None:
        credentials = f"{urllib.parse.unquote(parts.username)}:{urllib.parse.unquote(parts.password or '')}"
        authorization = "Basic " + base64.b64encode(credentials.encode("utf-8", "surrogateescape")).decode("ascii")
    return Proxy(name, number, authorization)


async def tunnel(proxy: Proxy, address: str, port: int):
    """A connection to `address`, already checked, through `proxy`'s HTTP CONNECT tunnel (RFC 9110, 9.3.6).
    The proxy is asked for the address, never a name. Raises ProxyError when the proxy can't be reached,
    and TunnelError when it doesn't open the tunnel: it refuses, closes, or doesn't answer in time."""
    try:
        reader, writer = await asyncio.wait_for(connect(proxy.host, proxy.port), CONNECT_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        raise ProxyError(f"couldn't connect to the proxy at {proxy}") from None
    try:
        status = await asyncio.wait_for(_ask(reader, writer, proxy, address, port), CONNECT_TIMEOUT)
    except BaseException as e:
        writer.close()  # whatever happened, a cancelled build too: no connection to the proxy is left open
        if isinstance(e, asyncio.TimeoutError):
            raise TunnelError(f"the proxy at {proxy} didn't answer in {CONNECT_TIMEOUT} s") from None
        if isinstance(e, (EOFError, OSError)):
            raise TunnelError(f"the proxy at {proxy} closed the connection without answering") from None
        if isinstance(e, asyncio.LimitOverrunError):
            raise TunnelError(f"the proxy at {proxy} answered with something that isn't HTTP") from None
        raise
    if 200 <= status < 300:  # any 2xx opens the tunnel
        return reader, writer
    writer.close()
    try:
        status_text = f"{status} {HTTPStatus(status).phrase}"  # the standard words, not the proxy's own
    except ValueError:
        status_text = str(status)
    raise TunnelError(f"the proxy at {proxy} answered {status_text}")


async def _ask(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, proxy: Proxy, address: str,
               port: int) -> int:
    """Ask `proxy` for a tunnel to `address`, and return its answer's status."""
    authority = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
    request = f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n"
    if proxy.authorization:
        request += f"Proxy-Authorization: {proxy.authorization}\r\n"
    writer.write(f"{request}\r\n".encode("ascii"))
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")  # what follows, already from the far end, stays in `reader`
    line = head.split(b"\r\n", 1)[0].split(b" ", 2)  # HTTP/1.1 200 Connection established
    if len(line) < 2 or not line[0].startswith(b"HTTP/") or len(line[1]) != 3 or not line[1].isdigit():
        raise TunnelError(f"the proxy at {proxy} answered with something that isn't HTTP")
    return int(line[1])


def note(page: str, target: str) -> str:
    """What a build says of a local address a page tried to reach."""
    return f"{page} tried to reach {target}, a local address (refused: {ALLOW})"


class Guard:
    """A SOCKS5 proxy on 127.0.0.1, in a thread of its own, that connects only to public addresses."""

    def __init__(self):
        self.port = 0
        self._failures: list[Failure] = []
        self._lock = threading.Lock()
        self._started = threading.Event()
        self._error: BaseException | None = None

    def __enter__(self) -> Guard:
        self._thread = threading.Thread(target=asyncio.run, args=(self._serve(),), name="narrate-guard", daemon=True)
        self._thread.start()
        self._started.wait()
        if self._error is not None:
            raise self._error
        return self

    def __exit__(self, *exc: object) -> None:
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(5)

    def chromium_args(self) -> list[str]:
        return [f"--proxy-server=socks5://127.0.0.1:{self.port}",
                "--proxy-bypass-list=<-loopback>",
                "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
                # WebRTC would otherwise send UDP around the proxy
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]

    def take(self) -> list[Failure]:
        """The connections refused or failed since the last call."""
        with self._lock:
            taken, self._failures = self._failures, []
        return taken

    async def _serve(self) -> None:
        try:
            self._loop, self._stop = asyncio.get_running_loop(), asyncio.Event()
            server = await asyncio.start_server(self._client, "127.0.0.1", 0)
            self.port = server.sockets[0].getsockname()[1]
        except BaseException as e:  # noqa: BLE001 - raised in the thread that started it
            self._error = e
            return
        finally:
            self._started.set()
        await self._stop.wait()
        server.close()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream = None
        try:
            _, methods = await reader.readexactly(2)
            await reader.readexactly(methods)
            writer.write(b"\x05\x00")  # no authentication: only this machine reaches 127.0.0.1
            _, command, _, kind = await reader.readexactly(4)
            if kind == NAME:
                host = (await reader.readexactly((await reader.readexactly(1))[0])).decode("ascii")
            elif kind in (IPV4, IPV6):
                packed = await reader.readexactly(4 if kind == IPV4 else 16)
                host = socket.inet_ntop(socket.AF_INET if kind == IPV4 else socket.AF_INET6, packed)
            else:
                _reply(writer, UNSUPPORTED)
                return
            port = int.from_bytes(await reader.readexactly(2), "big")
            upstream = await self._open(host.strip("[]"), port) if command == CONNECT else UNSUPPORTED
            if isinstance(upstream, int):
                _reply(writer, upstream)
                return
            _reply(writer, GRANTED)
            await _splice(reader, writer, *upstream)
        except (OSError, EOFError, UnicodeError, ValueError):
            pass
        finally:
            writer.close()
            if upstream is not None and not isinstance(upstream, int):
                upstream[1].close()

    async def _open(self, host: str, port: int):
        """A connection to a public address of `host`, directly or through the HTTP proxy for it (proxy_for),
        or the SOCKS reply that says why there isn't one."""
        target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        loop = asyncio.get_running_loop()
        try:
            addresses = await loop.run_in_executor(None, resolve, host, port)
        except (OSError, UnicodeError):
            addresses = []
        reachable = [a for a in addresses if public(a)]
        if addresses and not reachable:
            shown = target if addresses[0] == host else f"{target} ({addresses[0]})"
            self._fail(Failure(shown, True))
            return REFUSED
        why = ""
        try:
            # Worked out once, after the check: a proxy hears only of public addresses
            proxy = await loop.run_in_executor(None, proxy_for, host, port) if reachable else None
            for address in reachable:  # the very addresses checked: no second lookup
                try:
                    if proxy is not None:
                        return await tunnel(proxy, address, port)
                    return await asyncio.wait_for(connect(address, port), CONNECT_TIMEOUT)
                except TunnelError as e:  # the proxy answered, but not with a tunnel there: try the next
                    why = str(e)
                except (OSError, asyncio.TimeoutError):
                    continue
        except ProxyError as e:  # the proxy itself can't be used or reached: no address will do
            why = str(e)
        self._fail(Failure(target, False, why))
        return UNREACHABLE

    def _fail(self, failure: Failure) -> None:
        with self._lock:
            self._failures.append(failure)


def _reply(writer: asyncio.StreamWriter, code: int) -> None:
    writer.write(bytes([5, code, 0, IPV4]) + bytes(6))  # bound address 0.0.0.0:0: Chromium doesn't use it


async def _pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
    while data := await source.read(65536):
        sink.write(data)
        await sink.drain()


async def _splice(reader, writer, up_reader, up_writer) -> None:
    """Bytes both ways until either side closes."""
    pipes = [asyncio.ensure_future(_pipe(reader, up_writer)), asyncio.ensure_future(_pipe(up_reader, writer))]
    try:
        await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for pipe in pipes:
            pipe.cancel()
        await asyncio.gather(*pipes, return_exceptions=True)
