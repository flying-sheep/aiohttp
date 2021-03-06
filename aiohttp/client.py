"""HTTP Client for asyncio."""

import asyncio
import base64
import hashlib
import os
import sys
import traceback
import warnings
import http.cookies
import urllib.parse

import aiohttp
from .client_reqrep import ClientRequest, ClientResponse
from .errors import WSServerHandshakeError
from .multidict import MultiDictProxy, MultiDict, CIMultiDict, upstr
from .websocket import WS_KEY, WebSocketParser, WebSocketWriter
from .websocket_client import ClientWebSocketResponse
from . import hdrs


__all__ = ('ClientSession', 'request', 'get', 'options', 'head',
           'delete', 'post', 'put', 'patch')

PY_35 = sys.version_info >= (3, 5)


class ClientSession:
    """First-class interface for making HTTP requests."""

    _source_traceback = None
    _connector = None

    def __init__(self, *, connector=None, loop=None, cookies=None,
                 headers=None, skip_auto_headers=None,
                 auth=None, request_class=ClientRequest,
                 response_class=ClientResponse,
                 ws_response_class=ClientWebSocketResponse):

        if connector is None:
            connector = aiohttp.TCPConnector(loop=loop)
            loop = connector._loop  # never None
        else:
            if loop is None:
                loop = connector._loop  # never None
            elif connector._loop is not loop:
                raise ValueError("loop argument must agree with connector")

        self._loop = loop
        if loop.get_debug():
            self._source_traceback = traceback.extract_stack(sys._getframe(1))

        self._cookies = http.cookies.SimpleCookie()

        # For Backward compatability with `share_cookies` connectors
        if connector._share_cookies:
            self._update_cookies(connector.cookies)
        if cookies is not None:
            self._update_cookies(cookies)
        self._connector = connector
        self._default_auth = auth

        # Convert to list of tuples
        if headers:
            headers = CIMultiDict(headers)
        else:
            headers = CIMultiDict()
        self._default_headers = headers
        if skip_auto_headers is not None:
            self._skip_auto_headers = frozenset([upstr(i)
                                                 for i in skip_auto_headers])
        else:
            self._skip_auto_headers = frozenset()

        self._request_class = request_class
        self._response_class = response_class
        self._ws_response_class = ws_response_class

    def __del__(self, _warnings=warnings):
        if not self.closed:
            self.close()

            _warnings.warn("Unclosed client session {!r}".format(self),
                           ResourceWarning)
            context = {'client_session': self,
                       'message': 'Unclosed client session'}
            if self._source_traceback is not None:
                context['source_traceback'] = self._source_traceback
            self._loop.call_exception_handler(context)

    def request(self, method, url, *,
                params=None,
                data=None,
                headers=None,
                skip_auto_headers=None,
                files=None,
                auth=None,
                allow_redirects=True,
                max_redirects=10,
                encoding='utf-8',
                version=aiohttp.HttpVersion11,
                compress=None,
                chunked=None,
                expect100=False,
                read_until_eof=True):
        """Perform HTTP request."""
        return _RequestContextManager(
            self._request(
                method,
                url,
                params=params,
                data=data,
                headers=headers,
                skip_auto_headers=skip_auto_headers,
                files=files,
                auth=auth,
                allow_redirects=allow_redirects,
                max_redirects=max_redirects,
                encoding=encoding,
                version=version,
                compress=compress,
                chunked=chunked,
                expect100=expect100,
                read_until_eof=read_until_eof))

    @asyncio.coroutine
    def _request(self, method, url, *,
                 params=None,
                 data=None,
                 headers=None,
                 skip_auto_headers=None,
                 files=None,
                 auth=None,
                 allow_redirects=True,
                 max_redirects=10,
                 encoding='utf-8',
                 version=aiohttp.HttpVersion11,
                 compress=None,
                 chunked=None,
                 expect100=False,
                 read_until_eof=True):

        if self.closed:
            raise RuntimeError('Session is closed')

        redirects = 0
        if not isinstance(method, upstr):
            method = upstr(method)

        # Merge with default headers and transform to CIMultiDict
        headers = self._prepare_headers(headers)
        if auth is None:
            auth = self._default_auth
        # It would be confusing if we support explicit Authorization header
        # with `auth` argument
        if (headers is not None and
                auth is not None and
                hdrs.AUTHORIZATION in headers):
            raise ValueError("Can't combine `Authorization` header with "
                             "`auth` argument")

        skip_headers = set(self._skip_auto_headers)
        if skip_auto_headers is not None:
            for i in skip_auto_headers:
                skip_headers.add(upstr(i))

        while True:
            req = self._request_class(
                method, url, params=params, headers=headers,
                skip_auto_headers=skip_headers, data=data,
                cookies=self.cookies, files=files, encoding=encoding,
                auth=auth, version=version, compress=compress, chunked=chunked,
                expect100=expect100,
                loop=self._loop, response_class=self._response_class)

            conn = yield from self._connector.connect(req)
            try:
                resp = req.send(conn.writer, conn.reader)
                try:
                    yield from resp.start(conn, read_until_eof)
                except:
                    resp.close(force=True)
                    conn.close()
                    raise
            except (aiohttp.HttpProcessingError,
                    aiohttp.ServerDisconnectedError) as exc:
                raise aiohttp.ClientResponseError() from exc
            except OSError as exc:
                raise aiohttp.ClientOSError(*exc.args) from exc

            self._update_cookies(resp.cookies)
            # For Backward compatability with `share_cookie` connectors
            if self._connector._share_cookies:
                self._connector.update_cookies(resp.cookies)

            # redirects
            if resp.status in (301, 302, 303, 307) and allow_redirects:
                redirects += 1
                if max_redirects and redirects >= max_redirects:
                    resp.close(force=True)
                    break

                # For 301 and 302, mimic IE behaviour, now changed in RFC.
                # Details: https://github.com/kennethreitz/requests/pull/269
                if resp.status != 307:
                    method = hdrs.METH_GET
                    data = None
                    if headers.get(hdrs.CONTENT_LENGTH):
                        headers.pop(hdrs.CONTENT_LENGTH)

                r_url = (resp.headers.get(hdrs.LOCATION) or
                         resp.headers.get(hdrs.URI))

                scheme = urllib.parse.urlsplit(r_url)[0]
                if scheme not in ('http', 'https', ''):
                    resp.close(force=True)
                    raise ValueError('Can redirect only to http or https')
                elif not scheme:
                    r_url = urllib.parse.urljoin(url, r_url)

                url = r_url
                yield from resp.release()
                continue

            break

        return resp

    @asyncio.coroutine
    def ws_connect(self, url, *,
                   protocols=(),
                   timeout=10.0,
                   autoclose=True,
                   autoping=True,
                   auth=None):
        """Initiate websocket connection."""

        sec_key = base64.b64encode(os.urandom(16))

        headers = {
            hdrs.UPGRADE: hdrs.WEBSOCKET,
            hdrs.CONNECTION: hdrs.UPGRADE,
            hdrs.SEC_WEBSOCKET_VERSION: '13',
            hdrs.SEC_WEBSOCKET_KEY: sec_key.decode(),
        }
        if protocols:
            headers[hdrs.SEC_WEBSOCKET_PROTOCOL] = ','.join(protocols)

        # send request
        resp = yield from self.request('get', url, headers=headers,
                                       read_until_eof=False,
                                       auth=auth)

        # check handshake
        if resp.status != 101:
            raise WSServerHandshakeError('Invalid response status')

        if resp.headers.get(hdrs.UPGRADE, '').lower() != 'websocket':
            raise WSServerHandshakeError('Invalid upgrade header')

        if resp.headers.get(hdrs.CONNECTION, '').lower() != 'upgrade':
            raise WSServerHandshakeError('Invalid connection header')

        # key calculation
        key = resp.headers.get(hdrs.SEC_WEBSOCKET_ACCEPT, '')
        match = base64.b64encode(
            hashlib.sha1(sec_key + WS_KEY).digest()).decode()
        if key != match:
            raise WSServerHandshakeError('Invalid challenge response')

        # websocket protocol
        protocol = None
        if protocols and hdrs.SEC_WEBSOCKET_PROTOCOL in resp.headers:
            resp_protocols = [
                proto.strip() for proto in
                resp.headers[hdrs.SEC_WEBSOCKET_PROTOCOL].split(',')]

            for proto in resp_protocols:
                if proto in protocols:
                    protocol = proto
                    break

        reader = resp.connection.reader.set_parser(WebSocketParser)
        writer = WebSocketWriter(resp.connection.writer, use_mask=True)

        return self._ws_response_class(reader,
                                       writer,
                                       protocol,
                                       resp,
                                       timeout,
                                       autoclose,
                                       autoping,
                                       self._loop)

    def _update_cookies(self, cookies):
        """Update shared cookies."""
        if isinstance(cookies, dict):
            cookies = cookies.items()

        for name, value in cookies:
            if isinstance(value, http.cookies.Morsel):
                # use dict method because SimpleCookie class modifies value
                # before Python 3.4
                dict.__setitem__(self.cookies, name, value)
            else:
                self.cookies[name] = value

    def _prepare_headers(self, headers):
        """ Add default headers and transform it to CIMultiDict
        """
        # Convert headers to MultiDict
        result = CIMultiDict(self._default_headers)
        if headers:
            if not isinstance(headers, (MultiDictProxy, MultiDict)):
                headers = CIMultiDict(headers)
            added_names = set()
            for key, value in headers.items():
                if key in added_names:
                    result.add(key, value)
                else:
                    result[key] = value
                    added_names.add(key)
        return result

    def get(self, url, *, allow_redirects=True, **kwargs):
        """Perform HTTP GET request."""
        return _RequestContextManager(
            self._request(hdrs.METH_GET, url,
                          allow_redirects=allow_redirects,
                          **kwargs))

    def options(self, url, *, allow_redirects=True, **kwargs):
        """Perform HTTP OPTIONS request."""
        return _RequestContextManager(
            self._request(hdrs.METH_OPTIONS, url,
                          allow_redirects=allow_redirects,
                          **kwargs))

    def head(self, url, *, allow_redirects=False, **kwargs):
        """Perform HTTP HEAD request."""
        return _RequestContextManager(
            self._request(hdrs.METH_HEAD, url,
                          allow_redirects=allow_redirects,
                          **kwargs))

    def post(self, url, *, data=None, **kwargs):
        """Perform HTTP POST request."""
        return _RequestContextManager(
            self._request(hdrs.METH_POST, url,
                          data=data,
                          **kwargs))

    def put(self, url, *, data=None, **kwargs):
        """Perform HTTP PUT request."""
        return _RequestContextManager(
            self._request(hdrs.METH_PUT, url,
                          data=data,
                          **kwargs))

    def patch(self, url, *, data=None, **kwargs):
        """Perform HTTP PATCH request."""
        return _RequestContextManager(
            self._request(hdrs.METH_PATCH, url,
                          data=data,
                          **kwargs))

    def delete(self, url, **kwargs):
        """Perform HTTP DELETE request."""
        return _RequestContextManager(
            self._request(hdrs.METH_DELETE, url,
                          **kwargs))

    def close(self):
        """Close underlying connector.

        Release all acquired resources.
        """
        if not self.closed:
            self._connector.close()
            self._connector = None

    @property
    def closed(self):
        """Is client session closed.

        A readonly property.
        """
        return self._connector is None or self._connector.closed

    @property
    def connector(self):
        """Connector instance used for the session."""
        return self._connector

    @property
    def cookies(self):
        """The session cookies."""
        return self._cookies

    def detach(self):
        """Detach connector from session without closing the former.

        Session is switched to closed state anyway.
        """
        self._connector = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

if PY_35:
    from collections.abc import Coroutine
    base = Coroutine
else:
    base = object


class _RequestContextManager(base):

    __slots__ = ('_coro', '_resp')

    def __init__(self, coro):
        self._coro = coro
        self._resp = None

    def send(self, value):
        return self._coro.send(value)

    def throw(self, typ, val=None, tb=None):
        if val is None:
            return self._coro.throw(typ)
        elif tb is None:
            return self._coro.throw(typ, val)
        else:
            return self._coro.throw(typ, val, tb)

    def close(self):
        return self._coro.close()

    @asyncio.coroutine
    def __iter__(self):
        resp = yield from self._coro
        return resp

    if PY_35:
        def __await__(self):
            resp = yield from self._coro
            return resp

        @asyncio.coroutine
        def __aenter__(self):
            self._resp = yield from self._coro
            return self._resp

        @asyncio.coroutine
        def __aexit__(self, exc_type, exc, tb):
            if exc_type is not None:
                self._resp.close()
            else:
                yield from self._resp.release()


if not PY_35:
    try:
        from asyncio import coroutines
        coroutines._COROUTINE_TYPES += (_RequestContextManager,)
    except:
        pass


class _DetachedRequestContextManager(_RequestContextManager):

    __slots__ = _RequestContextManager.__slots__ + ('_session', )

    def __init__(self, coro, session):
        super().__init__(coro)
        self._session = session

    def __del__(self):
        self._session.detach()


def request(method, url, *,
            params=None,
            data=None,
            headers=None,
            skip_auto_headers=None,
            cookies=None,
            files=None,
            auth=None,
            allow_redirects=True,
            max_redirects=10,
            encoding='utf-8',
            version=aiohttp.HttpVersion11,
            compress=None,
            chunked=None,
            expect100=False,
            connector=None,
            loop=None,
            read_until_eof=True,
            request_class=None,
            response_class=None):
    """Constructs and sends a request. Returns response object.

    :param str method: http method
    :param str url: request url
    :param params: (optional) Dictionary or bytes to be sent in the query
      string of the new request
    :param data: (optional) Dictionary, bytes, or file-like object to
      send in the body of the request
    :param dict headers: (optional) Dictionary of HTTP Headers to send with
      the request
    :param dict cookies: (optional) Dict object to send with the request
    :param auth: (optional) BasicAuth named tuple represent HTTP Basic Auth
    :type auth: aiohttp.helpers.BasicAuth
    :param bool allow_redirects: (optional) If set to False, do not follow
      redirects
    :param version: Request http version.
    :type version: aiohttp.protocol.HttpVersion
    :param bool compress: Set to True if request has to be compressed
       with deflate encoding.
    :param chunked: Set to chunk size for chunked transfer encoding.
    :type chunked: bool or int
    :param bool expect100: Expect 100-continue response from server.
    :param connector: BaseConnector sub-class instance to support
       connection pooling.
    :type connector: aiohttp.connector.BaseConnector
    :param bool read_until_eof: Read response until eof if response
       does not have Content-Length header.
    :param request_class: (optional) Custom Request class implementation.
    :param response_class: (optional) Custom Response class implementation.
    :param loop: Optional event loop.

    Usage::

      >>> import aiohttp
      >>> resp = yield from aiohttp.request('GET', 'http://python.org/')
      >>> resp
      <ClientResponse(python.org/) [200]>
      >>> data = yield from resp.read()

    """
    if connector is None:
        connector = aiohttp.TCPConnector(loop=loop, force_close=True)

    kwargs = {}

    if request_class is not None:
        kwargs['request_class'] = request_class

    if response_class is not None:
        kwargs['response_class'] = response_class

    session = ClientSession(loop=loop,
                            cookies=cookies,
                            connector=connector,
                            **kwargs)
    return _DetachedRequestContextManager(
        session._request(method, url,
                         params=params,
                         data=data,
                         headers=headers,
                         skip_auto_headers=skip_auto_headers,
                         files=files,
                         auth=auth,
                         allow_redirects=allow_redirects,
                         max_redirects=max_redirects,
                         encoding=encoding,
                         version=version,
                         compress=compress,
                         chunked=chunked,
                         expect100=expect100,
                         read_until_eof=read_until_eof),
        session=session)


def get(url, **kwargs):
    return request(hdrs.METH_GET, url, **kwargs)


def options(url, **kwargs):
    return request(hdrs.METH_OPTIONS, url, **kwargs)


def head(url, **kwargs):
    return request(hdrs.METH_HEAD, url, **kwargs)


def post(url, **kwargs):
    return request(hdrs.METH_POST, url, **kwargs)


def put(url, **kwargs):
    return request(hdrs.METH_PUT, url, **kwargs)


def patch(url, **kwargs):
    return request(hdrs.METH_PATCH, url, **kwargs)


def delete(url, **kwargs):
    return request(hdrs.METH_DELETE, url, **kwargs)
