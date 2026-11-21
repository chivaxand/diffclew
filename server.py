#!/usr/bin/env python3
"""Small threaded HTTP(S) server for local tools and test stubs. Stdlib only, Python 3.8+.

    server = Server('0.0.0.0', 8111, root='.')

    @server.route('GET', '/hello')
    def hello(req, resp):
        return Response.withJson({'ip': req.ip})

    server.start_and_wait()

Routes run in registration order: every route whose method and path match gets the request and the response
so far; returning a Response replaces it, None keeps it. No match -> 404. A handler may raise HttpError(code, text);
any other exception -> 500 with the traceback in the log only (never sent to the client).

Files (Response.withFile, server.render) come only from `root`, only with an extension from mime_types
(.py, .pem, .env... are never served), never from hidden parts (.git) or through symlinks out of root.
Range requests are supported: video seek and Safari / iOS playback need them.
Response.withStream(generator) sends live output (NDJSON, logs) chunk by chunk.
"""
from __future__ import annotations

import html
import json
import os
import re
import socket
import ssl
import sys
import traceback
import zlib
from dataclasses import dataclass, field
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Iterable, Optional
from urllib.parse import parse_qsl, unquote, urlsplit

# Extension -> Content-Type. Also the allow list of files that may be served.
MIME_TYPES = {
    'html': 'text/html; charset=utf-8', 'htm': 'text/html; charset=utf-8', 'css': 'text/css; charset=utf-8',
    'js': 'text/javascript; charset=utf-8', 'json': 'application/json', 'txt': 'text/plain; charset=utf-8',
    'svg': 'image/svg+xml', 'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'gif': 'image/gif',
    'webp': 'image/webp', 'ico': 'image/x-icon', 'woff2': 'font/woff2',
    'mp3': 'audio/mpeg', 'wav': 'audio/wav', 'mp4': 'video/mp4', 'm4v': 'video/mp4', 'webm': 'video/webm',
    'mkv': 'video/x-matroska', 'vtt': 'text/vtt',
}


class HttpError(Exception):
    """Raise in a handler to answer with this status and plain text."""
    def __init__(self, code: int, text: str = ''):
        super().__init__(text)
        self.code, self.text = code, text


@dataclass
class Request:
    method: str
    url: str                  # decoded path without query: "/state/true"
    query: dict               # "?a=1&b=2" -> {"a": "1", "b": "2"}
    headers: Message          # case-insensitive: req.headers.get("content-type")
    body: bytes
    ip: str
    port: int
    match: Optional[re.Match] = None  # match of the route: req.match.group(1)

    @property
    def text(self) -> str:
        return self.body.decode('utf-8', 'replace')

    def json(self):
        """Parsed JSON body (None if empty); invalid JSON -> 400."""
        try:
            return json.loads(self.body) if self.body else None
        except ValueError:
            raise HttpError(400, 'Invalid JSON body') from None


@dataclass
class Response:
    code: int = 200
    body: bytes | str = b''
    headers: dict = field(default_factory=lambda: {'Content-Type': MIME_TYPES['html'], 'Cache-Control': 'no-cache'})
    file: Optional[str] = None  # URL path under the server root; the server sends it (Range, 404)
    stream: Optional[Iterable] = None  # chunks (bytes / str) sent as they come; see withStream

    @staticmethod
    def withText(text, code=200, type='text/plain; charset=utf-8'):
        response = Response(code, str(text))
        response.headers['Content-Type'] = type
        return response

    @staticmethod
    def withHtml(text, code=200):
        return Response.withText(text, code, MIME_TYPES['html'])

    @staticmethod
    def withJson(data, code=200):
        body = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        return Response.withText(body, code, 'application/json; charset=utf-8')

    @staticmethod
    def withFile(url_path, type=None):
        """File under the server root; Content-Type from its extension unless `type` is given."""
        response = Response(file=url_path, headers={'Cache-Control': 'no-cache'})
        if type:
            response.headers['Content-Type'] = type
        return response

    @staticmethod
    def withStream(chunks, type='application/x-ndjson', code=200):
        """Sends each chunk at once (live output: logs). No Content-Length: the connection ends the body.
        A generator is closed when the client disconnects: its `finally` / `with` blocks release upstream resources."""
        response = Response(code, stream=chunks)
        response.headers['Content-Type'] = type
        return response

    @staticmethod
    def with404():
        return Response.withText('Not found', 404)

    @staticmethod
    def withError(text='Internal error', code=500):
        return Response.withText(text, code)


@dataclass
class Route:
    func: Callable[[Request, Optional[Response]], Optional[Response]]
    method: Optional[str]  # None or "ANY" = every method
    regex: re.Pattern      # full match against Request.url


class Server:
    def __init__(self, host='0.0.0.0', port=8080, root='.', certfile=None, keyfile=None,
                 timeout=60, max_body=10 * 1024 * 1024, mime_types=None):
        self.host, self.port = host, port         # '0.0.0.0' = reachable from the LAN, '127.0.0.1' = this machine
        self.root = os.path.realpath(root)        # files are served only from here
        self.certfile, self.keyfile = certfile, keyfile  # certfile set -> HTTPS (keyfile None: key in certfile)
        self.timeout = timeout                    # seconds per socket read / write: drops stalled clients
        self.max_body = max_body                  # request body limit, also after gzip: bigger -> 413
        self.mime_types = {**MIME_TYPES, **(mime_types or {})}
        self.routes: list = []

    def route(self, method=None, url=None, pattern=None):
        """Decorator. url: exact path, '*' = any characters ('/api/*'); pattern: regex, full match;
        neither: every path. method: 'GET', 'POST'...; None or 'ANY': every method."""
        if pattern is None:
            pattern = '.*' if url is None else '.*'.join(map(re.escape, url.split('*')))

        def add(func):
            self.routes.append(Route(func, method.upper() if method else None, re.compile(pattern)))
            return func
        return add

    def dispatch(self, req: Request) -> Response:
        resp = None
        try:
            for route in self.routes:
                if route.method in (None, 'ANY', req.method) and (match := route.regex.fullmatch(req.url)):
                    req.match = match
                    resp = route.func(req, resp) or resp
        except HttpError as e:
            return Response.withText(e.text, e.code)
        except Exception:
            traceback.print_exc()
            return Response.withError()
        return resp or Response.with404()

    def resolve(self, url_path):
        """URL path -> (real path, content type) of a servable file, or None.
        Refused: anything out of root ('..', absolute paths, symlinks), hidden parts (.git, .env),
        extensions not in mime_types (.py, .pem), directories."""
        try:
            path = os.path.realpath(os.path.join(self.root, url_path.lstrip('/\\')))
            rel = os.path.relpath(path, self.root)
        except ValueError:  # NUL byte, another drive on Windows
            return None
        if os.path.isabs(rel) or any(part.startswith('.') for part in re.split(r'[\\/]', rel)):
            return None  # also catches '..' (out of root) and '.' (the root itself)
        content_type = self.mime_types.get(os.path.splitext(path)[1][1:].lower())
        return (path, content_type) if content_type and os.path.isfile(path) else None

    def render(self, url_path, **values):
        """HTML file from root with {{NAME}} placeholders; values are HTML-escaped."""
        found = self.resolve(url_path)
        if not found:
            return Response.with404()
        with open(found[0], encoding='utf-8') as f:
            text = f.read()
        for name, value in values.items():
            text = text.replace('{{%s}}' % name, html.escape(str(value)))
        return Response.withHtml(text)

    def start_and_wait(self):
        httpd = _HTTPServer((self.host, self.port), _Handler, self)
        host = local_ip() if self.host in ('', '0.0.0.0') else self.host
        print(f"Server started: {'https' if self.certfile else 'http'}://{host}:{self.port}/  root: {self.root}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print('Server stopped')
        finally:
            httpd.server_close()


# **************************************** Internals ****************************************

UNSATISFIABLE = 'unsatisfiable'


def parse_range(header, size):
    """Range header -> (start, end) inclusive, end clamped to the file; None = no / invalid header (send all, 200);
    UNSATISFIABLE = start beyond the file (416). Forms: bytes=a-b, bytes=a-, bytes=-n; of several ranges the first."""
    m = re.match(r'\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)', header or '')
    if not m or not (m.group(1) or m.group(2)):
        return None
    if not m.group(1):  # suffix: the last n bytes
        n = int(m.group(2))
        return (max(size - n, 0), size - 1) if n and size else UNSATISFIABLE
    start = int(m.group(1))
    if m.group(2) and int(m.group(2)) < start:
        return None
    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
    return (start, end) if start < size else UNSATISFIABLE


def local_ip():
    """LAN address for the start message (UDP connect sends no packets)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(('8.8.8.8', 80))
            return s.getsockname()[0]
        except OSError:
            return 'localhost'


class _HTTPServer(ThreadingHTTPServer):
    """One thread per connection (daemon threads: Ctrl+C does not wait for open video streams)."""

    def __init__(self, address, handler, app: Server):
        self.app = app
        super().__init__(address, handler)
        if app.certfile:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(app.certfile, app.keyfile)
            # Handshake in the connection thread, not in accept(): a stalled client must not block the server
            self.socket = context.wrap_socket(self.socket, server_side=True, do_handshake_on_connect=False)

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], (ConnectionError, socket.timeout, ssl.SSLError)):
            super().handle_error(request, client_address)  # client gone / bad TLS: no traceback


class _Handler(BaseHTTPRequestHandler):
    server: _HTTPServer

    def setup(self):
        self.timeout = self.server.app.timeout
        super().setup()

    def do_GET(self):
        self._handle(self.command)

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_GET

    def do_HEAD(self):
        self._handle('GET', send_body=False)

    def _handle(self, method, send_body=True):
        try:
            response = self.server.app.dispatch(self._read_request(method))
        except HttpError as e:
            response = Response.withText(e.text, e.code)
        try:
            if response.file is not None:
                self._send_file(response, send_body)
            elif response.stream is not None:
                self._send_stream(response, send_body)
            else:
                self._send(response, send_body)
        except (ConnectionError, socket.timeout):
            self.close_connection = True  # client went away: normal on video seek / stop

    def _read_request(self, method):
        parts = urlsplit(self.path)
        return Request(method=method, url=unquote(parts.path), query=dict(parse_qsl(parts.query, keep_blank_values=True)),
                       headers=self.headers, body=self._read_body(), ip=self.client_address[0], port=self.client_address[1])

    def _read_body(self):
        max_body = self.server.app.max_body
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            raise HttpError(400, 'Bad Content-Length')
        if length < 0:
            raise HttpError(400, 'Bad Content-Length')
        if length > max_body:
            raise HttpError(413, 'Body too large')
        data = self.rfile.read(length)
        if self.headers.get('Content-Encoding', '').lower() == 'gzip':
            gzip = zlib.decompressobj(16 + zlib.MAX_WBITS)
            try:
                data = gzip.decompress(data, max_body)  # bounded: a small gzip bomb cannot fill the memory
            except zlib.error:
                raise HttpError(400, 'Bad gzip body')
            if gzip.unconsumed_tail:
                raise HttpError(413, 'Body too large')
        return data

    def _start(self, code, headers, length):
        self.send_response(code)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header('X-Content-Type-Options', 'nosniff')
        if length is not None:
            self.send_header('Content-Length', str(length))
        self.end_headers()

    def _send_stream(self, response, send_body):
        self.close_connection = True  # the end of the body is the end of the connection
        self._start(response.code, response.headers, None)
        try:
            for chunk in response.stream if send_body else ():
                self.wfile.write(chunk.encode('utf-8') if isinstance(chunk, str) else chunk)
        except (ConnectionError, socket.timeout):
            pass  # client went away (stop button, closed tab)
        except Exception:
            traceback.print_exc()  # headers are sent: the client sees a cut body
        finally:
            if hasattr(response.stream, 'close'):
                response.stream.close()  # runs the generator's finally: closes upstream connections

    def _send(self, response, send_body):
        body = response.body.encode('utf-8') if isinstance(response.body, str) else response.body
        self._start(response.code, response.headers, len(body))
        if send_body:
            self.wfile.write(body)

    def _send_file(self, response, send_body):
        """Sends exactly the requested bytes (Range); socket.sendfile = zero-copy on plain HTTP."""
        found = self.server.app.resolve(response.file)
        if not found:
            return self._send(Response.with404(), send_body)
        path, content_type = found
        with open(path, 'rb') as f:
            size = os.fstat(f.fileno()).st_size
            byte_range = parse_range(self.headers.get('Range'), size)
            if byte_range == UNSATISFIABLE:
                return self._start(416, {'Content-Range': f'bytes */{size}'}, 0)
            start, end = byte_range or (0, size - 1)
            headers = {'Content-Type': content_type, **response.headers, 'Accept-Ranges': 'bytes'}
            if byte_range:
                headers['Content-Range'] = f'bytes {start}-{end}/{size}'
            self._start(206 if byte_range else response.code, headers, end - start + 1)
            if send_body and end >= start:
                self.connection.sendfile(f, start, end - start + 1)
