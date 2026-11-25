"""The Play client against the contracts it cannot renegotiate: google-auth's session and Google's Money.

Nothing in the suite talks to Play, so a keyword the session does not take, or a price written in the
wrong shape, would first show as a failed publish. The stub's ``request`` has the real method's
signature, so a call the real session would refuse fails here as a ``TypeError``
(``../CLAUDE.md``: a strict stub at an untested SDK boundary).
"""

from __future__ import annotations

from decimal import Decimal
import inspect

from google.auth.transport.requests import AuthorizedSession
import pytest

from asset_engine.store.play.client import API_ROOT, UPLOAD_ROOT, PlayClient
from asset_engine.store.play.subscriptions import Price


class _Response:
    status_code = 200
    ok = True
    content = b"{}"

    def json(self) -> dict:
        return {}


class _StrictSession:
    """Records each call after binding it to ``AuthorizedSession.request``'s own signature."""

    def __init__(self) -> None:
        self.calls: list[inspect.BoundArguments] = []
        self._signature = inspect.signature(AuthorizedSession.request)

    def request(self, *args, **kwargs):
        bound = self._signature.bind(self, *args, **kwargs)
        self.calls.append(bound)
        return _Response()


@pytest.fixture
def client() -> tuple[PlayClient, _StrictSession]:
    play = PlayClient(package_name="de.example.app", key_getter=lambda: {})
    session = _StrictSession()
    play._session = session  # type: ignore[assignment]
    return play, session


def test_json_and_media_requests_use_arguments_the_session_accepts(client) -> None:
    play, session = client
    play.get("edits/1/listings")
    play.put("edits/1/listings/de-DE", {"title": "x"})
    play.request(
        "POST",
        "edits/1/listings/de-DE/phoneScreenshots",
        params={"uploadType": "media"},
        data=b"\xff\xd8",
        content_type="image/jpeg",
        upload=True,
    )
    urls = [call.arguments["url"] for call in session.calls]
    assert urls[0] == f"{API_ROOT}/de.example.app/edits/1/listings"
    ## Media goes to the upload host; the JSON API host refuses a raw body.
    assert urls[2] == f"{UPLOAD_ROOT}/de.example.app/edits/1/listings/de-DE/phoneScreenshots"


@pytest.mark.parametrize(
    ("raw", "units", "nanos"),
    [("5.99 EUR", "5", 990_000_000), ("6.00 CHF", "6", 0), ("0.99 EUR", "0", 990_000_000)],
)
def test_a_price_is_written_as_googles_money_units_and_nanos(raw: str, units: str, nanos: int) -> None:
    """Google's Money is whole units as a string plus billionths as an int; 5.99 is 5 and 990000000."""
    price = Price.parse(raw, where="test")
    money = price.money()
    assert (money["units"], money["nanos"]) == (units, nanos)
    assert Price.from_money(money) == price
    assert Price.from_money(money).amount == Decimal(raw.split()[0])
