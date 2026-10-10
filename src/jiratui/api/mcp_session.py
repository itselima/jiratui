"""A long-lived connection to the Atlassian Remote MCP server.

This allows JiraTUI to talk to Jira Cloud without an API token. The first time the app connects it registers itself as
an OAuth client of the MCP server (dynamic client registration), opens the browser so the user can approve access with
their Atlassian account and stores the resulting tokens in the JiraTUI config directory. Subsequent runs reuse (and
refresh) the stored tokens.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit
import webbrowser

import httpx2
from mcp import Client
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from jiratui.constants import LOGGER_NAME
from jiratui.exceptions import (
    AuthorizationException,
    PermissionException,
    ResourceNotFoundException,
    ServiceInvalidRequestException,
    ServiceInvalidResponseException,
    ServiceUnavailableException,
)
from jiratui.files import get_config_directory

DEFAULT_MCP_SERVER_URL = 'https://mcp.atlassian.com/v1/mcp'
DEFAULT_OAUTH_CALLBACK_PORT = 33418
OAUTH_LOGIN_TIMEOUT_SECONDS = 300

logger = logging.getLogger(LOGGER_NAME)


class FileTokenStorage:
    """Persists the OAuth client registration and tokens in a JSON file readable only by the current user."""

    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)

    async def get_tokens(self) -> OAuthToken | None:
        if tokens := self._read().get('tokens'):
            return OAuthToken.model_validate(tokens)
        return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        data['tokens'] = tokens.model_dump(mode='json', exclude_none=True)
        # remember the absolute expiry so that a restarted app refreshes the token instead of starting a new login
        data['tokens_expire_at'] = time.time() + tokens.expires_in if tokens.expires_in else None
        self._write(data)

    def get_tokens_expiry(self) -> float | None:
        return self._read().get('tokens_expire_at')

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        if client_info := self._read().get('client_info'):
            return OAuthClientInformationFull.model_validate(client_info)
        return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read()
        data['client_info'] = client_info.model_dump(mode='json', exclude_none=True)
        self._write(data)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class _PersistentExpiryOAuthProvider(OAuthClientProvider):
    """Restores the expiry time of stored tokens so expired tokens are refreshed rather than triggering a new login."""

    async def _initialize(self) -> None:
        await super()._initialize()
        storage = self.context.storage
        if isinstance(storage, FileTokenStorage):
            self.context.token_expiry_time = storage.get_tokens_expiry()


class _OAuthCallbackServer:
    """A one-shot HTTP server on localhost that receives the OAuth authorization code."""

    def __init__(self, port: int):
        self.port = port
        self._result: asyncio.Future[AuthorizationCodeResult] | None = None
        self._server: asyncio.Server | None = None

    @property
    def redirect_uri(self) -> str:
        return f'http://localhost:{self.port}/callback'

    async def start(self) -> None:
        self._result = asyncio.get_running_loop().create_future()
        self._server = await asyncio.start_server(self._handle, '127.0.0.1', self.port)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_line = (await reader.readline()).decode(errors='replace')
        parts = request_line.split(' ')
        query = parse_qs(urlsplit(parts[1]).query) if len(parts) > 1 else {}
        if code := query.get('code', [None])[0]:
            body = 'JiraTUI is now connected to Jira. You can close this tab.'
            if self._result and not self._result.done():
                self._result.set_result(
                    AuthorizationCodeResult(
                        code=code,
                        state=query.get('state', [None])[0],
                        iss=query.get('iss', [None])[0],
                    )
                )
        else:
            error = query.get('error_description', query.get('error', ['unknown error']))[0]
            body = f'JiraTUI could not connect to Jira: {error}'
            if self._result and not self._result.done() and 'error' in query:
                self._result.set_exception(AuthorizationException(body))
        payload = body.encode()
        writer.write(
            b'HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\n'
            + f'Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n'.encode()
            + payload
        )
        await writer.drain()
        writer.close()

    async def wait_for_code(self) -> AuthorizationCodeResult:
        assert self._result is not None
        try:
            return await asyncio.wait_for(self._result, timeout=OAUTH_LOGIN_TIMEOUT_SECONDS)
        finally:
            await self.stop()

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            self._server = None


class AtlassianMcpSession:
    """Owns the connection to the Atlassian MCP server and exposes `call_tool`.

    The connection is opened lazily on the first call and kept open by a dedicated task, so it can be shared by all
    the workers of the Textual application (the MCP client uses task groups that must be entered and exited from the
    same task).
    """

    def __init__(
        self,
        server_url: str = DEFAULT_MCP_SERVER_URL,
        callback_port: int = DEFAULT_OAUTH_CALLBACK_PORT,
        token_file: Path | None = None,
        open_browser: bool = True,
    ):
        self.server_url = server_url
        self.callback_port = callback_port
        self.storage = FileTokenStorage(token_file or get_config_directory() / 'mcp-oauth.json')
        self.open_browser = open_browser
        self._client: Client | None = None
        self._owner_task: asyncio.Task | None = None
        self._ready: asyncio.Event | None = None
        self._closing: asyncio.Event | None = None
        self._connect_error: BaseException | None = None
        self._connect_lock: asyncio.Lock | None = None

    def _build_auth(self) -> OAuthClientProvider:
        callback_server = _OAuthCallbackServer(self.callback_port)

        async def redirect_handler(authorization_url: str) -> None:
            await callback_server.start()
            logger.info(
                'Opening the browser to authorize JiraTUI', extra={'url': authorization_url}
            )
            print(  # noqa: T201 - shown by `jiratui mcp login` when the browser can not be opened
                f'\nAuthorize JiraTUI in your browser. If it does not open, visit:\n{authorization_url}\n',
                file=sys.stderr,
            )
            if self.open_browser:
                webbrowser.open(authorization_url)

        async def callback_handler() -> AuthorizationCodeResult:
            return await callback_server.wait_for_code()

        return _PersistentExpiryOAuthProvider(
            server_url=self.server_url,
            client_metadata=OAuthClientMetadata(
                client_name='JiraTUI',
                redirect_uris=[callback_server.redirect_uri],
                grant_types=['authorization_code', 'refresh_token'],
                response_types=['code'],
                token_endpoint_auth_method='none',
            ),
            storage=self.storage,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )

    async def _own_connection(self) -> None:
        assert self._ready is not None and self._closing is not None
        try:
            async with httpx2.AsyncClient(
                auth=self._build_auth(), timeout=httpx2.Timeout(30, read=300)
            ) as http_client:
                async with Client(
                    streamable_http_client(self.server_url, http_client=http_client)
                ) as client:
                    self._client = client
                    self._ready.set()
                    await self._closing.wait()
        except BaseException as e:  # noqa: BLE001 - surfaced to the caller waiting on the connection
            self._connect_error = e
        finally:
            self._client = None
            self._ready.set()

    async def connect(self) -> Client:
        if self._connect_lock is None:
            self._connect_lock = asyncio.Lock()
        async with self._connect_lock:
            if self._client is not None:
                return self._client
            self._ready = asyncio.Event()
            self._closing = asyncio.Event()
            self._connect_error = None
            self._owner_task = asyncio.create_task(self._own_connection())
            await self._ready.wait()
            if self._client is None:
                error = self._connect_error
                self._owner_task = None
                raise ServiceUnavailableException(
                    f'Unable to connect to the Atlassian MCP server: {error}',
                    extra={'url': self.server_url},
                ) from error
            return self._client

    async def close(self) -> None:
        if self._closing is not None:
            self._closing.set()
        if self._owner_task is not None:
            if self._client is None:
                # still connecting (e.g. waiting for the user to authorize in the browser)
                self._owner_task.cancel()
            await asyncio.gather(self._owner_task, return_exceptions=True)
            self._owner_task = None

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Calls a tool and returns its JSON-decoded result.

        Raises:
            ServiceUnavailableException: if the MCP server can not be reached.
            ResourceNotFoundException, AuthorizationException, PermissionException, ServiceInvalidRequestException:
            if the tool reports an error.
            ServiceInvalidResponseException: if the result is not JSON.
        """
        arguments = {k: v for k, v in arguments.items() if v is not None}
        client = await self.connect()
        try:
            result = await client.call_tool(name, arguments)
        except Exception as e:
            # the session may have expired server-side; reconnect once and retry
            logger.warning(
                'MCP tool call failed; reconnecting', extra={'tool': name, 'error': str(e)}
            )
            await self.close()
            client = await self.connect()
            result = await client.call_tool(name, arguments)

        text = ''.join(getattr(block, 'text', '') for block in result.content)
        extra = {'tool': name, 'arguments': arguments}
        if result.is_error:
            raise _exception_for_tool_error(text, extra)
        if not text:
            return result.structured_content or {}
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise ServiceInvalidResponseException(text, extra=extra) from e


def _exception_for_tool_error(message: str, extra: dict) -> Exception:
    lowered = message.lower()
    extra = {**extra, 'errorMessages': [message]}
    if '404' in lowered or 'not found' in lowered or 'does not exist' in lowered:
        return ResourceNotFoundException(message, extra=extra)
    if '401' in lowered or 'unauthorized' in lowered:
        return AuthorizationException(message, extra=extra)
    if '403' in lowered or 'permission' in lowered or 'forbidden' in lowered:
        return PermissionException(message, extra=extra)
    return ServiceInvalidRequestException(message, extra=extra)
