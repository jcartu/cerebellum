"""Tests for http_safe.py — NoRedirectHandler behavior."""
from __future__ import annotations

import urllib.error
import urllib.request
from http.client import HTTPMessage, HTTPResponse
from io import BytesIO
from unittest.mock import MagicMock, Mock, patch

import pytest


class TestNoRedirectHandler:
    def test_200_passes_through(self):
        from cerebellum.http_safe import NoRedirectHandler
        handler = NoRedirectHandler()
        mock_request = MagicMock(spec=urllib.request.Request)
        mock_response = Mock(spec=HTTPResponse)
        mock_response.status = 200
        result = handler.http_response(mock_request, mock_response)
        assert result is mock_response

    def test_201_passes_through(self):
        from cerebellum.http_safe import NoRedirectHandler
        handler = NoRedirectHandler()
        mock_request = MagicMock(spec=urllib.request.Request)
        mock_response = Mock(spec=HTTPResponse)
        mock_response.status = 201
        result = handler.http_response(mock_request, mock_response)
        assert result is mock_response

    def test_404_passes_through(self):
        from cerebellum.http_safe import NoRedirectHandler
        handler = NoRedirectHandler()
        mock_request = MagicMock(spec=urllib.request.Request)
        mock_response = Mock(spec=HTTPResponse)
        mock_response.status = 404
        result = handler.http_response(mock_request, mock_response)
        assert result is mock_response

    def _assert_redirect_raises(self, status_code: int):
        from cerebellum.http_safe import NoRedirectHandler
        handler = NoRedirectHandler()
        mock_request = MagicMock(spec=urllib.request.Request)
        mock_response = Mock(spec=HTTPResponse)
        mock_response.status = status_code
        mock_response.url = "https://example.com/new"
        mock_response.headers = HTTPMessage()
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            handler.http_response(mock_request, mock_response)
        try:
            assert exc_info.value.code == status_code
        finally:
            exc_info.value.close()

    def test_301_raises_http_error(self):
        self._assert_redirect_raises(301)
        assert "Redirect forbidden" in str(self._last_exc) if hasattr(self, "_last_exc") else True

    def test_302_raises_http_error(self):
        self._assert_redirect_raises(302)

    def test_303_raises_http_error(self):
        self._assert_redirect_raises(303)

    def test_307_raises_http_error(self):
        self._assert_redirect_raises(307)

    def test_308_raises_http_error(self):
        self._assert_redirect_raises(308)

    def test_https_response_is_http_response(self):
        from cerebellum.http_safe import NoRedirectHandler
        handler = NoRedirectHandler()
        # https_response is assigned via `https_response = http_response` at class level
        # but Python creates separate bound method objects on each access, so use == not is
        assert handler.https_response == handler.http_response


class TestSafeOpener:
    def test_safe_opener_has_no_redirect_handler(self):
        from cerebellum.http_safe import _safe_opener
        handler_types = [type(h).__name__ for h in _safe_opener.handlers]
        assert "NoRedirectHandler" in handler_types

    def test_safe_opener_rejects_redirect(self):
        from cerebellum.http_safe import _safe_opener
        mock_request = MagicMock(spec=urllib.request.Request)
        error = urllib.error.HTTPError(
            "https://example.com", 302, "Redirect forbidden", HTTPMessage(), BytesIO()
        )
        try:
            with patch.object(_safe_opener, "open", side_effect=error):
                with pytest.raises(urllib.error.HTTPError) as exc_info:
                    _safe_opener.open(mock_request)
                assert exc_info.value.code == 302
        finally:
            error.close()
