"""Failures with a name, a status and something a person can act on.

Every endpoint here used to fail the same way: a traceback in the terminal
and `{"error": "internal"}` on the wire. That is the right answer for a bug
and the wrong answer for everything else, and access control is almost
entirely everything else. "You are not signed in", "your session expired",
"you can read this but not change it" and "GitHub refused the login" are
four different things, and a reader who sees one message for all four has
been told nothing.

So: a code the page can branch on, a sentence for the person, and an
optional hint saying what to do about it.
"""


class ProphecyError(Exception):
    """Something went wrong that we understand well enough to explain."""

    status = 400
    code = "error"

    def __init__(self, message, hint="", status=None, code=None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code

    def payload(self):
        body = {"error": self.message, "code": self.code}
        if self.hint:
            body["hint"] = self.hint
        return body


class NotSignedIn(ProphecyError):
    """No session at all. The page should offer the way in."""

    status = 401
    code = "not_signed_in"

    def __init__(self, message="You are not signed in.", hint="", **kw):
        super().__init__(message, hint or "Sign in with GitHub to continue.", **kw)


class SessionExpired(NotSignedIn):
    """A session that was real and is not any more, which is worth saying:
    it means sign in again, not ask for access."""

    code = "session_expired"

    def __init__(self, **kw):
        super().__init__("Your session has expired.",
                         "Sign in again to pick up where you were.", **kw)


class NotAllowed(ProphecyError):
    """Signed in, and still not permitted. Never says what is behind it."""

    status = 403
    code = "not_allowed"

    def __init__(self, message="You do not have access to this instance.",
                 hint="", **kw):
        super().__init__(message, hint, **kw)


class ReadOnly(NotAllowed):
    code = "read_only"

    def __init__(self, what="change anything here", **kw):
        super().__init__(
            f"Your access here is read-only, so you cannot {what}.",
            "An owner of this instance can change that.", **kw)


class BadRequest(ProphecyError):
    status = 400
    code = "bad_request"


class NotFound(ProphecyError):
    status = 404
    code = "not_found"


class UpstreamFailed(ProphecyError):
    """Something outside this process said no: GitHub, or git itself."""

    status = 502
    code = "upstream_failed"


def internal(detail=""):
    """The one case where the message really is "a bug happened here"."""
    error = ProphecyError(
        "Prophecy hit an error handling that request.",
        "The traceback is in the terminal running it.",
        status=500, code="internal")
    if detail:
        error.hint = detail
    return error
