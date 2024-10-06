"""Support cookie-encrypted sessions for ASGI applications."""

from __future__ import annotations

import sys
from base64 import urlsafe_b64decode, urlsafe_b64encode
from http import cookies
from typing import TYPE_CHECKING, Any, Dict, Optional, Union, cast

from db.database import dataBase
import asyncio

from asgi_tools import Request, Response
from asgi_tools._compat import json_dumps, json_loads
from asgi_tools.middleware import BaseMiddeware

if TYPE_CHECKING:
    from asgi_tools.types import TJSON, TASGIApp, TASGIReceive, TASGIScope, TASGISend

Fernet: Any
InvalidToken: Any

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    Fernet, InvalidToken = None, None


jwt: Any

try:
    import jwt
except ImportError:
    jwt = None


__all__ = "SessionMiddleware", "Session", "SessionJWT", "SessionFernet"


class SessionMiddleware(BaseMiddeware):
    """Support sessions."""

    def __init__(
        self,
        app: TASGIApp,
        secret_key: Optional[str] = None,
        *,
        session_type: str = "base64",
        cookie_name: str = "session",
        max_age: int = 14 * 24 * 3600,
        samesite: str = "lax",
        secure: bool = False,
    ):
        """Init the middleware."""
        super(SessionMiddleware, self).__init__(app)
        assert secret_key, "secret_key is required"
        self.secret_key = secret_key
        self.session_name = cookie_name
        self.session_type = session_type

        self.cookie_params: Dict[str, Any] = {"path": "/"}
        if max_age:
            self.cookie_params["max-age"] = max_age
        if secure:
            self.cookie_params["secure"] = secure
        if sys.version_info >= (3, 8) and samesite:  # XXX: Python 3.7
            self.cookie_params["samesite"] = samesite

    async def __process__(
        self, scope: Union[TASGIScope, Request], receive: TASGIReceive, send: TASGISend,
    ):
        """Load/save the sessions."""
        # Support asgi_tools.RequestMiddleware
        if isinstance(scope, Request):
            request = scope
        else:
            request = scope.get("request") or Request(scope, receive, send)

        session = self.init_session(request.session.get(self.session_name))
        scope["session"] = session

        # Common ASGI Applications
        def send_wrapper(message):
            """Inject sessions cookie."""
            if session.modified and message["type"] == "http.response.start":
                message["headers"].append(
                    (
                        b"Set-Cookie",
                        session.cookie(self.cookie_name, self.cookie_params).encode(),
                    ),
                )

            return send(message)

        # Support ASGI-Tools Responses
        response = await self.app(scope, receive, send_wrapper)
        if response and isinstance(response, Response) and session.modified:
            response.headers["Set-Cookie"] = session.cookie(
                self.cookie_name, self.cookie_params,
            )

        return response

    def init_session(self, token: Optional[str] = None) -> Session:
        if self.session_type == "jwt":
            return SessionJWT(token, secret=self.secret_key)

        if self.session_type == "fernet":
            return SessionFernet(token, secret=self.secret_key)

        if self.session_type == "mongodb":
            return SessionMongoDBStore 

        return Session(token)


class Session(dict):
    """Base4 session (not encrypted!)."""

    modified = False

    def __init__(self, value: Optional[str] = None, **payload):
        """Initialize the container."""
        if value:
            self.update(self.decode(value))

        if payload:
            self.update(payload)

    def __setitem__(self, name: str, value: TJSON):
        """Store the value and check that the session is pure."""
        self.modified = self.get(name) != value
        dict.__setitem__(self, name, value)

    def __delitem__(self, name: str):
        """Delete the value and check that the session is pure."""
        self.modified = name in self
        dict.__delitem__(self, name)

    def cookie(self, cookie_name: str, cookie_params: Dict) -> str:
        """Render the data as a cookie string."""
        morsel: cookies.Morsel = cookies.Morsel()
        value = self.encode()
        morsel.set(cookie_name, value, value)
        for k in cookie_params:
            morsel[k] = cookie_params[k]
        return morsel.OutputString()

    def clear(self) -> None:
        self.modified = bool(self)
        return dict.clear(self)

    def pop(self, name: str, default=None) -> TJSON:
        self.modified = bool(self)
        return dict.pop(self, name, default)

    def update(self, value):
        self.modifield = bool(value)
        return dict.update(self, value)

    def encode(self) -> str:
        payload = json_dumps(self)
        return urlsafe_b64encode(payload).decode()

    def decode(self, token: str, *, silent: bool = True) -> Dict:
        try:
            payload = urlsafe_b64decode(token)
        except ValueError:
            if silent:
                return {}
            raise
        else:
            return json_loads(payload)


class SessionJWT(Session):
    """Keep/update sessions data."""

    def __init__(self, *args, secret=None, **kwargs):
        if jwt is None:
            raise RuntimeError("Install jwt package to use JWT sessions.")

        if not secret:
            raise ValueError("SessionJWT.secret is required.")

        self.secret = secret
        super(SessionJWT, self).__init__(*args, **kwargs)

    def encode(self) -> str:
        """Encode the session's data."""
        token = jwt.encode(self, key=self.secret, algorithm="HS256")
        # Support JWT<2 (Remove me after 2022-01-01)
        if isinstance(token, bytes):
            return token.decode()
        return token

    def decode(self, token, *, silent=True) -> Dict:
        try:
            payload = jwt.decode(token, key=self.secret, algorithms=["HS256"])
            return cast(Dict, payload)
        except jwt.DecodeError:
            if not silent:
                raise

        return {}


class SessionFernet(Session):
    """Keep/update sessions data."""

    def __init__(self, *args, secret=None, **kwargs):
        if Fernet is None:
            raise RuntimeError("Install cryptography package to use fernet sessions.")

        if not secret:
            raise ValueError("SessionFernet.secret is required.")

        if len(secret) != 32:
            secret = secret[:32]
            secret += "=" * (32 - len(secret) % 32)

        self.secret = urlsafe_b64encode(secret.encode())
        self.f = Fernet(self.secret)
        super(SessionFernet, self).__init__(*args, **kwargs)

    def encode(self) -> str:
        """Encode the session's data."""
        payload = json_dumps(self)
        return self.f.encrypt(payload).decode()

    def decode(self, token, *, silent=True) -> Dict:
        try:
            payload = self.f.decrypt(token.encode())
            return json_loads(payload)
        except InvalidToken:
            if not silent:
                raise

        return {}


class SessionMongoDB(Session):

    def __init__(self, *args, **kwargs):
        self.method = self.kwargs.pop('method')
        self.db = self.kwargs.pop('db')
        self.col = self.kwargs.pop('col')
        self.session_key = self.kwargs.pop('session_id')
        
        client = dataBase.motorConf("client", self.method)
        col = client[self.db][self.col]

    async def get(self, key: str, **kwargs: dict) -> Optional[dict]:  # pragma: no cover
        value = await self.mongodb.get(key.encode(), **kwargs)
        return _loads(value) if value else None

    async def set(
        self, key: str, value: dict, exp: Optional[int] = None, **kwargs: dict
    ) -> Optional[str]:  # pragma: no cover
        return await self.memcache.set(key.encode(), _dumps(value), exptime=exp, **kwargs)

    async def delete(self, key: str, **kwargs: dict) -> Any:  # pragma: no cover
        return await self.memcache.delete(key.encode(), **kwargs)

    async def update(self, key: str, value: dict)
                  
        