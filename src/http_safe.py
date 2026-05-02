"""Safe urllib helpers that forbid HTTP redirects."""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Any


class NoRedirectHandler(urllib.request.HTTPErrorProcessor):
    """Reject redirect responses so callers never follow them implicitly."""

    def http_response(self, request: urllib.request.Request, response: Any) -> Any:
        if response.status in (301, 302, 303, 307, 308):
            raise urllib.error.HTTPError(response.url, response.status, "Redirect forbidden", response.headers, None)
        return response

    https_response = http_response


_safe_opener = urllib.request.build_opener(NoRedirectHandler())
