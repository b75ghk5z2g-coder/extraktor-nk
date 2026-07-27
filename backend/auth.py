"""
Optional HTTP Basic Auth gate for the whole appka.

Only active when the NKU_PIN environment variable is set to a non-empty
value. This is meant for the hosted (Render) deployment, where the appka
sits at a public URL and needs a simple gate so a random visitor with the
link can't use it. On a normal desktop run (Windows .exe / mac .command),
NKU_PIN is never set, so this middleware is a complete no-op -- nothing
changes for local use, no login prompt appears.

Deliberately simple: one shared PIN for anyone who needs access (Samuel and
whoever else he shares the hosted link with), not per-person accounts. The
username field in the browser's Basic Auth prompt is ignored entirely --
only the password is checked against NKU_PIN. Typing anything (or nothing)
in the username field is fine.
"""

import base64
import os
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Render's own health checks hit this path without any Authorization header --
# exempting it keeps the service from being marked unhealthy/restarted just
# because the prober can't supply the PIN.
_UNPROTECTED_PATHS = {"/api/health"}


class PinAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.pin = os.environ.get("NKU_PIN", "").strip()

    async def dispatch(self, request: Request, call_next):
        if not self.pin or request.url.path in _UNPROTECTED_PATHS:
            return await call_next(request)

        provided_password = ""
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                _, _, provided_password = decoded.partition(":")
            except Exception:
                provided_password = ""

        if provided_password and secrets.compare_digest(provided_password, self.pin):
            return await call_next(request)

        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="NKU Extraktor"'},
        )
