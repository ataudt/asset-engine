"""Authenticated HTTP against the Google Play Developer API (Android Publisher v3).

Layer: asset engine, store publishing. Needs the ``store`` extra (google-auth, requests).
Rules:
  - The engine holds no key. The app builds the client with ``key_getter``, a callable returning the
    service account's JSON as a dict, so an unset key raises in the app with its name, and the key
    rotates in the app's one place (``../STORE_ACCESS.md``: ``fastlane-supply@app-werkstatt``).
  - Every path is relative to the app: ``client.get("edits/…")`` means
    ``applications/<package>/edits/…``. The client is bound to one package, so a run cannot touch
    another app by a typo.
  - Google's errors carry the reason in ``error.message``; the raise always quotes it, and the
    status is an attribute (``PlayApiError.status``) so a 404 is told from a failure without parsing
    text.
  - A dropped connection, a 429 or a 5xx is retried, but only for a request that is safe to send
    twice: a POST that Google did receive before the connection broke would create twice.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
import logging
import time
from typing import Any

from google.auth.exceptions import TransportError
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
import requests


logger = logging.getLogger(__name__)

API_ROOT = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications"
UPLOAD_ROOT = "https://androidpublisher.googleapis.com/upload/androidpublisher/v3/applications"
SCOPE = "https://www.googleapis.com/auth/androidpublisher"

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 4
NETWORK_ERRORS = (requests.ConnectionError, requests.Timeout, TransportError)
IDEMPOTENT_METHODS = frozenset({"GET", "PUT", "PATCH", "DELETE"})


class PlayApiError(RuntimeError):
    """A request Google rejected, with its ``error.message`` quoted and the HTTP status kept."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def _describe(response: requests.Response) -> str:
    try:
        error = response.json().get("error") or {}
    except ValueError:
        error = {}
    message = error.get("message") if isinstance(error, dict) else None
    return f"HTTP {response.status_code}: {message or response.text[:600]}"


@dataclass
class PlayClient:
    """A thin JSON client for one app's package. One instance per run; the session is built lazily."""

    package_name: str
    ## The service account's JSON as a dict: the app's config getter. Required — the engine reads no config.
    key_getter: Callable[[], dict]
    timeout: int = 120
    _session: AuthorizedSession | None = field(default=None, init=False, repr=False)

    def _http(self) -> AuthorizedSession:
        if self._session is None:
            credentials = service_account.Credentials.from_service_account_info(
                self.key_getter(), scopes=[SCOPE]
            )
            self._session = AuthorizedSession(credentials)
        return self._session

    def url(self, path: str, *, upload: bool = False) -> str:
        root = UPLOAD_ROOT if upload else API_ROOT
        return f"{root}/{self.package_name}/{path.lstrip('/')}" if path else f"{root}/{self.package_name}"

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        params: dict | None = None,
        data: bytes | None = None,
        content_type: str | None = None,
        upload: bool = False,
    ) -> dict:
        """One API call; ``data`` with ``content_type`` sends a media upload instead of JSON."""
        url = self.url(path, upload=upload)
        retryable = method.upper() in IDEMPOTENT_METHODS
        headers = {"Content-Type": content_type} if content_type else None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._http().request(
                    method,
                    url,
                    data=data,
                    headers=headers,
                    json=payload,
                    params=params,
                    timeout=self.timeout,
                )
            except NETWORK_ERRORS:
                if not retryable or attempt == MAX_ATTEMPTS:
                    raise
                logger.warning("Play %s %s -> network error, retrying", method, path, exc_info=True)
                time.sleep(2**attempt)
                continue
            if response.status_code in RETRY_STATUSES and retryable and attempt < MAX_ATTEMPTS:
                logger.warning("Play %s %s -> %s, retrying", method, path, response.status_code)
                time.sleep(2**attempt)
                continue
            if not response.ok:
                raise PlayApiError(f"{method} {path or '/'} — {_describe(response)}", response.status_code)
            return response.json() if response.content else {}
        raise AssertionError("unreachable")

    def get(self, path: str, **params: Any) -> dict:
        return self.request("GET", path, params=params or None)

    def get_or_none(self, path: str, **params: Any) -> dict | None:
        """``get``, with ``None`` for a resource that does not exist (404)."""
        try:
            return self.get(path, **params)
        except PlayApiError as error:
            if error.status == 404:
                return None
            raise

    def post(self, path: str, payload: dict | None = None, **params: Any) -> dict:
        return self.request("POST", path, payload=payload if payload is not None else {}, params=params or None)

    def put(self, path: str, payload: dict, **params: Any) -> dict:
        return self.request("PUT", path, payload=payload, params=params or None)

    def patch(self, path: str, payload: dict, **params: Any) -> dict:
        return self.request("PATCH", path, payload=payload, params=params or None)

    def delete(self, path: str) -> dict:
        return self.request("DELETE", path)

    def list_all(self, path: str, key: str, **params: Any) -> Iterator[dict]:
        """Every row under ``key`` of a paged collection, following ``nextPageToken``."""
        page = self.get(path, **params)
        while True:
            yield from page.get(key, [])
            token = page.get("nextPageToken")
            if not token:
                return
            page = self.get(path, **{**params, "pageToken": token})


__all__ = ["API_ROOT", "NETWORK_ERRORS", "SCOPE", "PlayApiError", "PlayClient"]
