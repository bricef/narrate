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
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import os
import ssl
import socket
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass

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


connect = asyncio.open_connection  # the proxy's way out to an address it has checked


@dataclass(frozen=True)
class Failure:
    """A connection the proxy didn't make: `target` is host:port, with the address it resolved to when the
    host is a name. `local` when it was refused as a local address; otherwise it found no address, or none
    answered."""

    target: str
    local: bool


class Unreachable(Exception):
    """A page that couldn't be reached itself, as the proxy saw it."""

    def __init__(self, failure: Failure):
        super().__init__(f"{failure.target} is a local address ({ALLOW})" if failure.local
                         else f"couldn't reach {failure.target}")
        self.failure = failure


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
        """A connection to a public address of `host`, or the SOCKS reply that says why there isn't one."""
        target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        try:
            addresses = await asyncio.get_running_loop().run_in_executor(None, resolve, host, port)
        except (OSError, UnicodeError):
            addresses = []
        reachable = [a for a in addresses if public(a)]
        if addresses and not reachable:
            shown = target if addresses[0] == host else f"{target} ({addresses[0]})"
            self._fail(Failure(shown, True))
            return REFUSED
        for address in reachable:  # the very addresses checked: no second lookup
            try:
                proxy = self._proxy(host, port)
                if proxy:
                    return await asyncio.wait_for(_through_proxy(proxy, address, port), CONNECT_TIMEOUT)
                return await asyncio.wait_for(connect(address, port), CONNECT_TIMEOUT)
            except (OSError, asyncio.TimeoutError):
                if proxy:
                    break
                continue
        self._fail(Failure(target, False))
        return UNREACHABLE

    @staticmethod
    def _proxy(host: str, port: int) -> str | None:
        """The configured HTTP proxy for this destination, unless NO_PROXY excludes it."""
        host = host.strip("[]")
        proxies = urllib.request.getproxies()
        if urllib.request.proxy_bypass_environment(host, proxies):
            return None
        if not any(name in os.environ for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                                                    "http_proxy", "https_proxy", "all_proxy")) and \
                urllib.request.proxy_bypass(host):
            return None
        return proxies.get("https" if port == 443 else "http") or proxies.get("all")

    def _fail(self, failure: Failure) -> None:
        with self._lock:
            self._failures.append(failure)


def _reply(writer: asyncio.StreamWriter, code: int) -> None:
    writer.write(bytes([5, code, 0, IPV4]) + bytes(6))  # bound address 0.0.0.0:0: Chromium doesn't use it


async def _through_proxy(proxy: str, address: str, port: int):
    """Tunnel to an already checked address through an HTTP(S) proxy."""
    if "://" not in proxy:
        proxy = "http://" + proxy
    parsed = urllib.parse.urlsplit(proxy)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise OSError("unsupported proxy URL")
    proxy_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    tls = ssl.create_default_context() if parsed.scheme == "https" else None
    reader, writer = await connect(parsed.hostname, proxy_port, ssl=tls,
                                  server_hostname=parsed.hostname if tls else None)
    authority = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
    headers = [f"CONNECT {authority} HTTP/1.1", f"Host: {authority}", "Proxy-Connection: Keep-Alive"]
    if parsed.username is not None:
        credentials = urllib.parse.unquote(parsed.username) + ":" + urllib.parse.unquote(parsed.password or "")
        token = base64.b64encode(credentials.encode()).decode("ascii")
        headers.append(f"Proxy-Authorization: Basic {token}")
    writer.write(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
    await writer.drain()
    response = await reader.readuntil(b"\r\n\r\n")
    status = response.split(b"\r\n", 1)[0].split()
    if len(status) < 2 or status[1] != b"200":
        writer.close()
        raise OSError("proxy refused CONNECT")
    return reader, writer


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
