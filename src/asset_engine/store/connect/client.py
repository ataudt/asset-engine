"""Authenticated HTTP against the App Store Connect API.

Layer: asset engine, store upload.
Rules:
  - The engine holds no key. The app builds the client with ``key_getter``, a callable returning
    ``{"key_id", "issuer_id", "private_key"}`` from its own config, so an unset key raises there with
    its name rather than as a 401 twenty lines later, and the key rotates in the app's one place.
  - A client can be built to refuse resources (``forbidden_resources``): each request path and
    payload is checked against them first, and a hit raises ``MainListingRefused``. NutriSpy's custom
    product pages use it to keep their client off the app's main listing.
  - Apple's errors carry the reason in ``errors[].detail``. A bare status code is useless for
    debugging a rejected edit, so the raise always quotes it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
import json
import logging
import time
from typing import Any

import jwt
import requests


logger = logging.getLogger(__name__)

API_ROOT = "https://api.appstoreconnect.apple.com/v1"

## Apple rejects a token older than 20 minutes. 15 leaves room for a slow screenshot upload to
## finish on the token it started with.
TOKEN_LIFETIME_SECONDS = 15 * 60
TOKEN_REFRESH_MARGIN_SECONDS = 60

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 4

## A dropped connection or a timed-out write is retried like a 5xx — but only for a request that is
## safe to send twice. A custom-product-page run is ~2 000 uploads of 2–4 MB frames, and one stalled
## write used to end the whole run (NutriSpy, Sep 2026). POST is excluded: a create Apple did receive
## before the connection broke would leave a second, orphaned reservation behind.
NETWORK_ERRORS = (requests.ConnectionError, requests.Timeout)
IDEMPOTENT_METHODS = frozenset({"GET", "PUT", "PATCH", "DELETE"})


class AppStoreConnectError(RuntimeError):
    """A request Apple rejected, with its ``errors[].detail`` quoted."""


class MainListingRefused(RuntimeError):
    """A request named a resource this client was built to refuse (``forbidden_resources``). Always
    a bug in the caller."""


def _describe(response: requests.Response) -> str:
    try:
        errors = response.json().get("errors") or []
    except ValueError:
        errors = []
    if not errors:
        return f"HTTP {response.status_code}: {response.text[:400]}"
    parts = [
        f"{error.get('code', '?')}: {error.get('title', '')} — {error.get('detail', '')}".strip()
        for error in errors
    ]
    return f"HTTP {response.status_code}: " + " | ".join(parts)


@dataclass
class AppStoreConnectClient:
    """A thin JSON:API client. One instance per run; the token is minted lazily and reused."""

    ## Which team key signs: the app's config getter. Required — the engine reads no config.
    key_getter: Callable[[], dict]
    timeout: int = 60
    session: requests.Session = field(default_factory=requests.Session)
    ## Resource stems no request of this client may name, as a path segment, a relationship key or a
    ## payload type. Stems, not full type names: `appStoreVersions`, `appStoreVersionLocalization`
    ## and a `type` value all contain `appStoreVersion`, and the singular is the one a list of
    ## plurals would let slip through.
    forbidden_resources: tuple[str, ...] = ()
    _token: str | None = field(default=None, init=False, repr=False)
    _token_expires: float = field(default=0.0, init=False, repr=False)

    ## -- auth ---------------------------------------------------------------------------------

    def _bearer(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires - TOKEN_REFRESH_MARGIN_SECONDS:
            return self._token
        key = self.key_getter()
        expires = now + TOKEN_LIFETIME_SECONDS
        self._token = jwt.encode(
            {
                ## A *team* key identifies itself by issuer; only an individual key uses `sub`.
                "iss": key["issuer_id"],
                "iat": int(now),
                "exp": int(expires),
                "aud": "appstoreconnect-v1",
            },
            key["private_key"],
            algorithm="ES256",
            headers={"kid": key["key_id"], "typ": "JWT"},
        )
        self._token_expires = expires
        return self._token

    ## -- requests -----------------------------------------------------------------------------

    def _guard(self, path: str, payload: dict | None = None) -> None:
        if not self.forbidden_resources:
            return
        haystack = path if payload is None else f"{path} {json.dumps(payload)}"
        for resource in self.forbidden_resources:
            if resource in haystack:
                raise MainListingRefused(
                    f"{resource} is refused by this client (forbidden_resources) — it belongs to "
                    f"the app's main listing, which this caller must never write (path {path!r})"
                )

    def _send(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """``session.request``, retrying a dropped connection when ``method`` is safe to repeat."""
        retryable = method.upper() in IDEMPOTENT_METHODS
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return self.session.request(method, url, **kwargs)
            except NETWORK_ERRORS:
                if not retryable or attempt == MAX_ATTEMPTS:
                    raise
                delay = 2**attempt
                logger.warning(
                    "App Store Connect %s %s -> network error, retrying in %ss",
                    method,
                    url.split("?")[0],
                    delay,
                    exc_info=True,
                )
                time.sleep(delay)
        raise AssertionError("unreachable")

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        """One API call. ``path`` is relative to ``API_ROOT``, or an absolute link Apple handed us."""
        self._guard(path, payload)
        url = path if path.startswith("http") else f"{API_ROOT}/{path.lstrip('/')}"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            response = self._send(
                method,
                url,
                headers={
                    "Authorization": f"Bearer {self._bearer()}",
                    "Content-Type": "application/json",
                },
                json=payload,
                params=params,
                timeout=self.timeout,
            )
            if response.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                delay = 2**attempt
                logger.warning(
                    "App Store Connect %s %s -> %s, retrying in %ss",
                    method,
                    url,
                    response.status_code,
                    delay,
                )
                time.sleep(delay)
                continue
            if not response.ok:
                raise AppStoreConnectError(f"{method} {url} — {_describe(response)}")
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()
        raise AppStoreConnectError(f"{method} {url} — gave up after {MAX_ATTEMPTS} attempts")

    def get(self, path: str, **params: Any) -> dict:
        return self.request("GET", path, params=params or None)

    def post(self, path: str, payload: dict) -> dict:
        return self.request("POST", path, payload=payload)

    def patch(self, path: str, payload: dict) -> dict:
        return self.request("PATCH", path, payload=payload)

    def delete(self, path: str, payload: dict | None = None) -> dict:
        ## A payload for relationship endpoints, whose DELETE names the linkages to remove.
        return self.request("DELETE", path, payload=payload)

    def list_all(self, path: str, **params: Any) -> Iterator[dict]:
        """Every ``data`` row of a collection, following ``links.next``."""
        page = self.get(path, **params)
        while True:
            yield from page.get("data", [])
            nxt = (page.get("links") or {}).get("next")
            if not nxt:
                return
            page = self.request("GET", nxt)

    def upload_asset_chunk(self, method: str, url: str, headers: dict[str, str], chunk: bytes) -> None:
        """A single ``uploadOperations`` entry.

        Deliberately not routed through ``request``: the URL is Apple's blob storage, not the API,
        and it must **not** carry the bearer token — sending it there would leak the credential to a
        host that has no business with it and, on some regions, gets the upload rejected outright.
        """
        response = self._send(method, url, headers=headers, data=chunk, timeout=self.timeout)
        if not response.ok:
            raise AppStoreConnectError(
                f"{method} {url} — HTTP {response.status_code}: {response.text[:200]}"
            )


__all__ = [
    "API_ROOT",
    "NETWORK_ERRORS",
    "AppStoreConnectClient",
    "AppStoreConnectError",
    "MainListingRefused",
]
