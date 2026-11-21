#!/usr/bin/env python3
"""Open one HTTP connection to addresses that the caller already checked.

The hostname is kept for the Host header and for TLS. The TCP connection does
not call getaddrinfo, so a later answer for that name cannot move the request.
"""

import errno
import http.client
import ipaddress
import socket
import sys
from urllib.request import HTTPHandler, HTTPSHandler, ProxyHandler, build_opener


def addresses(values):
    if isinstance(values, (str, bytes)) or not values:
        raise ValueError("The connection address is not an IP address")
    pinned = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("The connection address is not an IP address")
        try:
            pinned.append(str(ipaddress.ip_address(value)))
        except ValueError:
            raise ValueError("The connection address is not an IP address") from None
    return tuple(pinned)


def _dial(address, port, timeout, source_address):
    parsed = ipaddress.ip_address(address)
    family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
            sock.settimeout(timeout)
        if source_address:
            sock.bind(source_address)
        destination = (str(parsed), port, 0, 0) if parsed.version == 6 else (str(parsed), port)
        sock.connect(destination)
        return sock
    except BaseException:
        sock.close()
        raise


def _connection(kind, pinned):
    base = http.client.HTTPSConnection if kind == "https" else http.client.HTTPConnection

    class Pinned(base):
        def connect(self):
            if self._tunnel_host:
                raise OSError("The pinned connection does not use a proxy tunnel")
            sys.audit("http.client.connect", self, self.host, self.port)
            last = None
            for address in pinned:
                try:
                    sock = _dial(address, self.port, self.timeout, self.source_address)
                except OSError as error:
                    last = error
                    continue
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError as error:
                    if error.errno != errno.ENOPROTOOPT:
                        sock.close()
                        raise
                try:
                    if kind == "https":
                        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                    else:
                        self.sock = sock
                except BaseException:
                    sock.close()
                    raise
                return
            raise last if last is not None else OSError("The connection address is not an IP address")

    return Pinned


class _PinnedHTTP(HTTPHandler):
    def __init__(self, connection):
        super().__init__()
        self._connection = connection

    def http_open(self, req):
        return self.do_open(self._connection, req)


class _PinnedHTTPS(HTTPSHandler):
    def __init__(self, connection):
        super().__init__()
        self._connection = connection

    def https_open(self, req):
        return self.do_open(self._connection, req, context=self._context)


def handler(scheme, values):
    pinned = addresses(values)
    if scheme == "https":
        return _PinnedHTTPS(_connection("https", pinned))
    if scheme == "http":
        return _PinnedHTTP(_connection("http", pinned))
    raise ValueError("The connection scheme is not http or https")


def opener(scheme, values, redirect):
    return build_opener(ProxyHandler({}), redirect(), handler(scheme, values))
