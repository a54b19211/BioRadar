"""Single-workspace development login; this is not production identity or SSO."""

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from fastapi import Request, Security
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer

from bioradar.api.config import Settings
from bioradar.api.errors import DomainError

COOKIE_NAME = "bioradar_engine_session"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
USER_BEARER = HTTPBearer(
    auto_error=False,
    scheme_name="DevelopmentUserCredential",
    description="Local development-user credential for CLI; never an upstream or shared service API key.",
)
USER_SESSION = APIKeyCookie(
    name=COOKIE_NAME,
    auto_error=False,
    scheme_name="DevelopmentUserSession",
    description="Short-lived HttpOnly local development login session; no SSO or production authorization.",
)


@dataclass(frozen=True)
class EngineActor:
    actor_id: str
    workspace_id: str
    grant_ref: str
    source_allowlist: tuple[str, ...]


class DevelopmentAccess:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return self.settings.engine_dev_credential is not None

    def actor(self) -> EngineActor:
        workspace = self.settings.engine_workspace_id
        return EngineActor(
            actor_id="development-user",
            workspace_id=workspace,
            grant_ref=f"development:{workspace}:sources",
            source_allowlist=tuple(self.settings.engine_source_allowlist),
        )

    def _credential(self) -> str:
        if self.settings.engine_dev_credential is None:
            raise DomainError("engine_not_configured", "开发研究登录尚未配置", 503)
        return self.settings.engine_dev_credential.get_secret_value()

    def check_local(self, request: Request) -> None:
        # The deployment still has to bind loopback. Do not trust forwarded headers.
        if request.url.hostname not in LOCAL_HOSTS:
            raise DomainError("engine_local_only", "研究预览仅允许本机开发访问", 403)

    def check_origin(self, request: Request) -> None:
        if request.headers.get("origin") not in self.settings.engine_allowed_origins:
            raise DomainError("engine_origin_forbidden", "请求来源不在开发许可范围", 403)

    def check_credential(self, credential: str) -> None:
        if not hmac.compare_digest(credential.encode(), self._credential().encode()):
            raise DomainError("authentication_required", "开发登录凭据无效", 401)

    def _sign(self, encoded: str) -> str:
        key = hashlib.sha256(("bioradar-development-session:" + self._credential()).encode()).digest()
        message = (self.settings.engine_workspace_id + ":" + encoded).encode()
        return hmac.new(key, message, hashlib.sha256).hexdigest()

    def create_session(self) -> str:
        payload = {"v": 1, "expires": int(time.time()) + self.settings.engine_session_seconds}
        encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
        return encoded + "." + self._sign(encoded)

    def _session_valid(self, cookie: str) -> bool:
        if not cookie or len(cookie) > 1024:
            return False
        try:
            encoded, signature = cookie.split(".", 1)
            if not hmac.compare_digest(signature, self._sign(encoded)):
                return False
            payload = json.loads(base64.urlsafe_b64decode(encoded))
            return (
                set(payload) == {"v", "expires"}
                and payload["v"] == 1
                and type(payload["expires"]) is int
                and payload["expires"] > int(time.time())
            )
        except (ValueError, TypeError, UnicodeError):
            return False

    def optional_actor(self, request: Request) -> EngineActor | None:
        self.check_local(request)
        if not self.enabled:
            return None
        authorization = request.headers.get("authorization")
        if authorization is not None:
            scheme, _, credential = authorization.partition(" ")
            if scheme.lower() != "bearer" or not credential:
                return None
            self.check_credential(credential)
            # A browser sending Authorization must still be from an allowed Origin.
            if request.headers.get("origin") is not None:
                self.check_origin(request)
            return self.actor()
        if self._session_valid(request.cookies.get(COOKIE_NAME, "")):
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                self.check_origin(request)
            return self.actor()
        return None

    def require_actor(
        self,
        request: Request,
        bearer: HTTPAuthorizationCredentials | None = Security(USER_BEARER),
        session_cookie: str | None = Security(USER_SESSION),
    ) -> EngineActor:
        actor = self.optional_actor(request)
        if actor is None:
            raise DomainError("authentication_required", "请先登录本地研究预览", 401)
        return actor
