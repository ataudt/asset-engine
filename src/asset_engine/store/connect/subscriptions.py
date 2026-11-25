"""The app's auto-renewable subscriptions on App Store Connect, from ``appstore/subscriptions.json``.

Layer: asset engine, store publishing. Grown from Wunschkalender's
``marketing/app_store_connect/subscriptions.py`` and NutriSpy's ``marketing/store_prices/apple.py``.
Rules:
  - The file is the source; read, plan, and with ``execute`` write what differs. A missing group,
    product, localization or availability is created. A product id is permanent once created.
  - ``prices`` are the customer prices of the territories the file names, stated rather than
    equalized (Apple's equalization of 5.99 EUR gave 4.00 CHF). Apple wants a price in every
    territory before a subscription can go to review, however few it is sold in, so every other
    territory gets Apple's equalization of the base territory's price, filled where missing and never
    touched after.
  - A stated price that differs is set. Before Apple has approved the product nobody can have bought
    it, so the price is replaced outright. After approval it is scheduled (Apple takes a change from
    the second day on): a **cut** with ``preserveCurrentPrice: false``, so existing subscribers renew
    at the lower price too; a **raise** with ``true``, so they keep theirs and only new subscribers pay
    more — the same rule as on Play (NutriSpy, Aaron, 2026-11-21).
  - Copy (group name, product name and description per language, review note) is updated where it
    differs. What cannot change is reported: a product's period.
  - Availability: the territories of ``store.json`` → ``apple`` (every territory for ``"ALL"``), not
    in territories Apple adds later unless it says so.
  - ``introOffer`` (mode, duration, number of periods, and for a paid offer the price per stated
    territory) is kept in every territory the product is sold in; a paid price is handed over on a
    date like a price change (``ensure_intro_offer``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from asset_engine.store.connect.app import AppleSettings, apple_settings, ref, resolve_app
from asset_engine.store.connect.client import AppStoreConnectClient, AppStoreConnectError
from asset_engine.store.connect.record import all_territories
from asset_engine.store.listing import read_json, store_locales
from asset_engine.store.report import Report


## Apple's caps on a subscription's localized display name and description. App Store Connect takes a
## 31-character name (NutriSpy's "Premium Monatlich (3-Tage Test)"), so the name cap is its 35.
NAME_CAP = 35
DESCRIPTION_CAP = 55
## A subscription Apple has never approved: its price may still be replaced outright.
UNAPPROVED_STATES = frozenset({"MISSING_METADATA", "READY_TO_SUBMIT", "DEVELOPER_ACTION_NEEDED", "REJECTED"})
## The earliest start Apple accepts for a price change of an approved product, in days from today.
CHANGE_LEAD_DAYS = 2


class SubscriptionsFileError(ValueError):
    """``subscriptions.json`` does not describe a valid set of App Store products."""


@dataclass(frozen=True)
class AppleProduct:
    product_id: str
    reference_name: str
    period: str
    group_level: int
    prices: dict[str, Decimal]
    ## Apple locale -> (name, description).
    copy: dict[str, tuple[str, str]]
    review_note: str
    ## ``introOffer``: {"mode", "duration", "periods", "prices"?: {territory: price}} or ``None``.
    intro: dict | None = None


def _localized(value: str | dict, settings: AppleSettings) -> dict[str, str]:
    if isinstance(value, str):
        return {settings.primary_locale: value}
    tags = store_locales("apple")
    return {tags[code]: text for code, text in value.items() if code in tags}


def load_products() -> tuple[dict, list[AppleProduct]]:
    """The group and the products of ``subscriptions.json``; a cap overage raises."""
    settings = apple_settings()
    data = read_json("subscriptions.json")
    products: list[AppleProduct] = []
    for row in data["subscriptions"]:
        if "referenceName" not in row:
            ## A Play-only entry (a base plan with no App Store counterpart).
            continue
        names = _localized(row["name"], settings)
        descriptions = _localized(row["description"], settings)
        copy = {locale: (names[locale], descriptions.get(locale, "")) for locale in names}
        for locale, (name, description) in copy.items():
            if len(name) > NAME_CAP or len(description) > DESCRIPTION_CAP:
                raise SubscriptionsFileError(
                    f"{row['productId']} [{locale}]: name {len(name)}/{NAME_CAP}, description "
                    f"{len(description)}/{DESCRIPTION_CAP} characters"
                )
        prices = {code: Decimal(value) for code, value in row["prices"].items()}
        if settings.base_territory not in prices:
            raise SubscriptionsFileError(f"{row['productId']}: no price in the base territory {settings.base_territory}")
        if settings.territories is not None and not set(settings.territories) <= set(prices):
            raise SubscriptionsFileError(
                f"{row['productId']}: prices for {sorted(prices)}, sold in {sorted(settings.territories)}"
            )
        products.append(
            AppleProduct(
                product_id=row["productId"],
                reference_name=row["referenceName"],
                period=row["subscriptionPeriod"],
                group_level=row["groupLevel"],
                prices=prices,
                copy=copy,
                review_note=row.get("reviewNote", ""),
                intro=row.get("introOffer"),
            )
        )
    return data["group"], products


def _create(client: AppStoreConnectClient, kind: str, attributes: dict, relationships: dict) -> dict:
    return client.post(kind, {"data": {"type": kind, "attributes": attributes, "relationships": relationships}})["data"]


def _patch(client: AppStoreConnectClient, kind: str, ident: str, attributes: dict) -> None:
    client.patch(f"{kind}/{ident}", {"data": {"type": kind, "id": ident, "attributes": attributes}})


## -- group ---------------------------------------------------------------------------------------


def ensure_group(client: AppStoreConnectClient, app_id: str, group: dict, settings: AppleSettings, report: Report) -> str | None:
    """The group's id (``None`` in a plan that would create it), with its display name."""
    groups = {row["attributes"]["referenceName"]: row for row in client.list_all(f"apps/{app_id}/subscriptionGroups", limit=50)}
    row = groups.get(group["referenceName"])
    if row is None:
        report.change(f"group {group['referenceName']!r}")
        if not report.execute:
            return None
        row = _create(client, "subscriptionGroups", {"referenceName": group["referenceName"]}, {"app": ref("apps", app_id)})
    else:
        report.ok(f"group {group['referenceName']!r}")
    names = _localized(group["name"], settings)
    localizations = {
        loc["attributes"]["locale"]: loc
        for loc in client.list_all(f"subscriptionGroups/{row['id']}/subscriptionGroupLocalizations", limit=50)
    }
    for locale, name in names.items():
        current = localizations.get(locale)
        if current is None:
            report.change(f"group name ({locale}): {name!r}")
            if report.execute:
                _create(
                    client,
                    "subscriptionGroupLocalizations",
                    {"name": name, "locale": locale},
                    {"subscriptionGroup": ref("subscriptionGroups", row["id"])},
                )
        elif current["attributes"].get("name") != name:
            report.change(f"group name ({locale}): {current['attributes'].get('name')!r} -> {name!r}")
            if report.execute:
                _patch(client, "subscriptionGroupLocalizations", current["id"], {"name": name})
        else:
            report.ok(f"group name ({locale})")
    return row["id"]


## -- product ---------------------------------------------------------------------------------------


def ensure_product(
    client: AppStoreConnectClient, group_id: str | None, product: AppleProduct, settings: AppleSettings, report: Report
) -> None:
    existing = {}
    if group_id is not None:
        existing = {
            row["attributes"]["productId"]: row
            for row in client.list_all(f"subscriptionGroups/{group_id}/subscriptions", limit=50)
        }
    row = existing.get(product.product_id)
    if row is None:
        prices = ", ".join(f"{code} {value}" for code, value in product.prices.items())
        report.change(f"{product.product_id}: create, {product.period}, level {product.group_level}, {prices}")
        if not report.execute:
            return
        row = _create(
            client,
            "subscriptions",
            {
                "name": product.reference_name,
                "productId": product.product_id,
                "subscriptionPeriod": product.period,
                "familySharable": False,
                "reviewNote": product.review_note,
                "groupLevel": product.group_level,
            },
            {"group": ref("subscriptionGroups", group_id)},
        )
    else:
        attributes = row["attributes"]
        report.ok(f"{product.product_id}: exists ({attributes.get('state')})")
        if attributes.get("subscriptionPeriod") != product.period:
            report.note(f"{product.product_id}: period {attributes.get('subscriptionPeriod')}, file {product.period} — a period never changes")
        if attributes.get("groupLevel") != product.group_level:
            report.note(f"{product.product_id}: level {attributes.get('groupLevel')}, file {product.group_level} — reorder the group in App Store Connect")
        differing = {
            key: value
            for key, value in (("name", product.reference_name), ("reviewNote", product.review_note))
            if (attributes.get(key) or "") != value
        }
        if differing:
            report.change(f"{product.product_id}: {', '.join(differing)}")
            if report.execute:
                _patch(client, "subscriptions", row["id"], differing)
    ensure_localizations(client, row["id"], product, report)
    ensure_availability(client, row["id"], product, settings, report)
    ## The paid first year must stay below its yearly price on every day: it goes first when prices
    ## come down and last when they go up (NutriSpy's first-year discount).
    if _intro_cut(client, row["id"], product):
        ensure_intro_offer(client, row["id"], product, settings, report)
        ensure_prices(client, row["id"], row["attributes"].get("state"), product, settings, report)
    else:
        ensure_prices(client, row["id"], row["attributes"].get("state"), product, settings, report)
        ensure_intro_offer(client, row["id"], product, settings, report)


def _intro_cut(client: AppStoreConnectClient, subscription_id: str, product: AppleProduct) -> bool:
    """Whether a stated paid introductory price is lower than the one running."""
    prices = (product.intro or {}).get("prices") or {}
    if not prices:
        return False
    live = intro_offers(client, subscription_id)
    return any(
        live.get(code, {}).get("price") is not None and Decimal(value) < live[code]["price"]
        for code, value in prices.items()
    )


def ensure_localizations(client: AppStoreConnectClient, subscription_id: str, product: AppleProduct, report: Report) -> None:
    current = {
        loc["attributes"]["locale"]: loc
        for loc in client.list_all(f"subscriptions/{subscription_id}/subscriptionLocalizations", limit=50)
    }
    for locale, (name, description) in product.copy.items():
        row = current.get(locale)
        if row is None:
            report.change(f"{product.product_id} [{locale}]: name and description")
            if report.execute:
                _create(
                    client,
                    "subscriptionLocalizations",
                    {"name": name, "description": description, "locale": locale},
                    {"subscription": ref("subscriptions", subscription_id)},
                )
            continue
        differing = {
            key: value
            for key, value in (("name", name), ("description", description))
            if (row["attributes"].get(key) or "") != value
        }
        if differing:
            report.change(f"{product.product_id} [{locale}]: {', '.join(differing)}")
            if report.execute:
                _patch(client, "subscriptionLocalizations", row["id"], differing)
        else:
            report.ok(f"{product.product_id} [{locale}]: name and description")


def ensure_availability(
    client: AppStoreConnectClient, subscription_id: str, product: AppleProduct, settings: AppleSettings, report: Report
) -> None:
    wanted = set(settings.territories) if settings.territories is not None else set(all_territories(client))
    label = ", ".join(settings.territories) if settings.territories is not None else "every territory"
    availability = client.get_or_none(f"subscriptions/{subscription_id}/subscriptionAvailability")
    if availability is None:
        report.change(f"{product.product_id}: sold in {label}")
        if report.execute:
            client.post(
                "subscriptionAvailabilities",
                {
                    "data": {
                        "type": "subscriptionAvailabilities",
                        "attributes": {"availableInNewTerritories": settings.available_in_new_territories},
                        "relationships": {
                            "subscription": ref("subscriptions", subscription_id),
                            "availableTerritories": {"data": [{"type": "territories", "id": code} for code in sorted(wanted)]},
                        },
                    }
                },
            )
        return
    territories = {
        row["id"]
        for row in client.list_all(f"subscriptionAvailabilities/{availability['data']['id']}/availableTerritories", limit=200)
    }
    if territories == wanted:
        report.ok(f"{product.product_id}: sold in {label}")
    else:
        report.note(
            f"{product.product_id}: sold in {len(territories)} territories on App Store Connect, the file says "
            f"{label} — change it in App Store Connect (taking a territory away touches subscribers)"
        )


## -- prices ----------------------------------------------------------------------------------------


def current_prices(client: AppStoreConnectClient, subscription_id: str) -> tuple[dict[str, Decimal], dict[str, list[tuple[str, Decimal]]]]:
    """Per territory the price new subscribers pay today, and the changes scheduled after today.

    The listing is a history: each change adds a row with its ``startDate`` (none = since the start),
    and today's price is the row with the latest start on or before today.
    """
    today = date.today().isoformat()
    rows, included = client.list_with_included(
        f"subscriptions/{subscription_id}/prices", include="territory,subscriptionPricePoint", limit=200
    )
    history: dict[str, list[tuple[str, Decimal]]] = {}
    for row in rows:
        relationships = row["relationships"]
        territory = relationships["territory"]["data"]["id"]
        point = included[("subscriptionPricePoints", relationships["subscriptionPricePoint"]["data"]["id"])]
        history.setdefault(territory, []).append(
            (row["attributes"].get("startDate") or "", Decimal(point["attributes"]["customerPrice"]))
        )
    current: dict[str, Decimal] = {}
    scheduled: dict[str, list[tuple[str, Decimal]]] = {}
    for territory, entries in history.items():
        entries.sort(key=lambda entry: entry[0])
        started = [entry for entry in entries if entry[0] <= today]
        if started:
            current[territory] = started[-1][1]
        later = [entry for entry in entries if entry[0] > today]
        if later:
            scheduled[territory] = later
    return current, scheduled


def _point_at(client: AppStoreConnectClient, subscription_id: str, code: str, price: Decimal) -> str:
    for row in client.list_all(f"subscriptions/{subscription_id}/pricePoints", **{"filter[territory]": code, "limit": 200}):
        if Decimal(row["attributes"]["customerPrice"]) == price:
            return row["id"]
    raise AppStoreConnectError(f"subscription {subscription_id}: no {code} price point at {price}")


def ensure_prices(
    client: AppStoreConnectClient,
    subscription_id: str,
    state: str | None,
    product: AppleProduct,
    settings: AppleSettings,
    report: Report,
) -> None:
    approved = state is not None and state not in UNAPPROVED_STATES
    current, scheduled = current_prices(client, subscription_id) if state is not None else ({}, {})
    replace: list[str] = []
    schedule: list[tuple[str, bool]] = []
    for code, wanted in product.prices.items():
        if current.get(code) == wanted:
            report.ok(f"{product.product_id}: {code} {wanted}")
            continue
        if any(price == wanted for _, price in scheduled.get(code, [])):
            report.ok(f"{product.product_id}: {code} {wanted} scheduled")
            continue
        if code not in current:
            report.change(f"{product.product_id}: {code} price {wanted}")
            replace.append(code)
        elif not approved:
            report.change(f"{product.product_id}: {code} price {current[code]} -> {wanted} (never approved: replaced)")
            replace.append(code)
        else:
            raise_ = wanted > current[code]
            report.change(
                f"{product.product_id}: {code} price {current[code]} -> {wanted} from "
                f"{(date.today() + timedelta(days=CHANGE_LEAD_DAYS)).isoformat()} "
                f"({'raise, existing subscribers keep theirs' if raise_ else 'cut, existing subscribers too'})"
            )
            schedule.append((code, raise_))

    elsewhere = [code for code in all_territories(client) if code not in product.prices and code not in current]
    base_price = product.prices[settings.base_territory]
    if elsewhere:
        report.change(
            f"{product.product_id}: prices in {len(elsewhere)} more territories "
            f"(Apple's equalization of {settings.base_territory} {base_price})"
        )
    else:
        report.ok(f"{product.product_id}: priced in every territory")
    if not report.execute:
        return

    if replace or elsewhere:
        points = {code: _point_at(client, subscription_id, code, product.prices[code]) for code in replace}
        if elsewhere:
            base_point = _point_at(client, subscription_id, settings.base_territory, base_price)
            equalized = {
                row["relationships"]["territory"]["data"]["id"]: row["id"]
                for row in client.list_all(f"subscriptionPricePoints/{base_point}/equalizations", include="territory", limit=200)
            }
            missing = [code for code in elsewhere if code not in equalized]
            if missing:
                raise AppStoreConnectError(f"{product.product_id}: Apple has no equalized price for {', '.join(missing)}")
            points.update({code: equalized[code] for code in elsewhere})
        ## preserveCurrentPrice false: a replaced price applies to everyone, right only because a
        ## replacement happens solely before approval, when nobody has bought it.
        client.patch(
            f"subscriptions/{subscription_id}",
            {
                "data": {
                    "type": "subscriptions",
                    "id": subscription_id,
                    "relationships": {
                        "prices": {"data": [{"type": "subscriptionPrices", "id": f"${{{code}}}"} for code in points]}
                    },
                },
                "included": [
                    {
                        "type": "subscriptionPrices",
                        "id": f"${{{code}}}",
                        "attributes": {"preserveCurrentPrice": False},
                        "relationships": {
                            "subscription": ref("subscriptions", subscription_id),
                            "subscriptionPricePoint": ref("subscriptionPricePoints", point),
                            "territory": ref("territories", code),
                        },
                    }
                    for code, point in points.items()
                ],
            },
        )
    start = (date.today() + timedelta(days=CHANGE_LEAD_DAYS)).isoformat()
    for code, raise_ in schedule:
        ## A row without startDate is the initial price, which an approved subscription refuses (409).
        client.post(
            "subscriptionPrices",
            {
                "data": {
                    "type": "subscriptionPrices",
                    "attributes": {"preserveCurrentPrice": raise_, "startDate": start},
                    "relationships": {
                        "subscription": ref("subscriptions", subscription_id),
                        "subscriptionPricePoint": ref(
                            "subscriptionPricePoints", _point_at(client, subscription_id, code, product.prices[code])
                        ),
                        "territory": ref("territories", code),
                    },
                }
            },
        )


## -- introductory offers ------------------------------------------------------------------------------


def intro_offers(client: AppStoreConnectClient, subscription_id: str) -> dict[str, dict]:
    """Per territory the introductory offer running today (or the next one), with later ones under
    ``scheduled``: ``{"id", "mode", "duration", "periods", "start", "end", "price"}``."""
    today = date.today().isoformat()
    rows, included = client.list_with_included(
        f"subscriptions/{subscription_id}/introductoryOffers", include="territory,subscriptionPricePoint", limit=200
    )
    by_territory: dict[str, list[dict]] = {}
    for row in rows:
        attributes = row["attributes"]
        point_ref = (row["relationships"].get("subscriptionPricePoint") or {}).get("data")
        point = included.get(("subscriptionPricePoints", point_ref["id"])) if point_ref else None
        by_territory.setdefault(row["relationships"]["territory"]["data"]["id"], []).append(
            {
                "id": row["id"],
                "mode": attributes["offerMode"],
                "duration": attributes["duration"],
                "periods": attributes["numberOfPeriods"],
                "start": attributes.get("startDate") or "",
                "end": attributes.get("endDate"),
                "price": Decimal(point["attributes"]["customerPrice"]) if point else None,
            }
        )
    result: dict[str, dict] = {}
    for territory, offers in by_territory.items():
        running = [o for o in offers if o["start"] <= today and (o["end"] is None or o["end"] > today)]
        later = sorted((o for o in offers if o["start"] > today), key=lambda o: o["start"])
        first = (running or later or [None])[0]
        if first is not None:
            result[territory] = {**first, "scheduled": later if running else later[1:]}
    return result


def _create_intro(
    client: AppStoreConnectClient, subscription_id: str, territory: str, intro: dict, point_id: str | None, start: str | None
) -> None:
    relationships = {
        "subscription": ref("subscriptions", subscription_id),
        "territory": ref("territories", territory),
    }
    if point_id:
        relationships["subscriptionPricePoint"] = ref("subscriptionPricePoints", point_id)
    client.post(
        "subscriptionIntroductoryOffers",
        {
            "data": {
                "type": "subscriptionIntroductoryOffers",
                "attributes": {
                    "duration": intro["duration"],
                    "offerMode": intro["mode"],
                    "numberOfPeriods": intro["periods"],
                    "startDate": start,
                },
                "relationships": relationships,
            }
        },
    )


def ensure_intro_offer(
    client: AppStoreConnectClient, subscription_id: str, product: AppleProduct, settings: AppleSettings, report: Report
) -> None:
    """The product's introductory offer: present in every territory it is sold in, its paid price
    where the file states one.

    A paid offer's price cannot change in place (Apple's ``PATCH`` takes only ``endDate``) and two
    offers of a territory may not overlap, so a new price is handed over on one date: the running
    offer ends on it and a new one starts on it (NutriSpy's first-year discount, from
    ``marketing/store_prices``). A different mode, duration or number of periods is reported: that is
    a different offer, decided in App Store Connect.
    """
    live = intro_offers(client, subscription_id)
    if product.intro is None:
        if live:
            report.note(f"{product.product_id}: an introductory offer in {len(live)} territories, none in the file")
        return
    intro = product.intro
    shape = (intro["mode"], intro["duration"], intro["periods"])
    label = f"{product.product_id} intro {intro['mode']} {intro['duration']}×{intro['periods']}"
    prices = {code: Decimal(value) for code, value in (intro.get("prices") or {}).items()}
    territories = set(settings.territories) if settings.territories is not None else set(all_territories(client))
    missing = sorted(territories - set(live))
    other = sorted(code for code, offer in live.items() if (offer["mode"], offer["duration"], offer["periods"]) != shape)
    if other:
        report.note(f"{label}: a different offer runs in {', '.join(other[:8])}{'…' if len(other) > 8 else ''} — left as it is")
    start = (date.today() + timedelta(days=CHANGE_LEAD_DAYS)).isoformat()
    reprice: list[tuple[str, Decimal]] = []
    for code, wanted in sorted(prices.items()):
        offer = live.get(code)
        if offer is None or code in other:
            continue
        if offer["price"] == wanted or any(o["price"] == wanted for o in offer["scheduled"]):
            report.ok(f"{label} {code}: {wanted}")
            continue
        report.change(f"{label} {code}: {offer['price']} -> {wanted} from {start}")
        reprice.append((code, wanted))
    if missing:
        needs_price = intro["mode"] != "FREE_TRIAL"
        unpriced = [code for code in missing if needs_price and code not in prices]
        if unpriced:
            report.note(f"{label}: missing in {len(unpriced)} territories the file states no price for — left as it is")
        missing = [code for code in missing if code not in unpriced]
        if missing:
            report.change(f"{label}: create in {len(missing)} territories ({', '.join(missing[:8])}{'…' if len(missing) > 8 else ''})")
    elif not reprice and not other:
        report.ok(f"{label}: in every territory")
    if not report.execute:
        return
    for code in missing:
        point = _point_at(client, subscription_id, code, prices[code]) if intro["mode"] != "FREE_TRIAL" else None
        _create_intro(client, subscription_id, code, intro, point, None)
    for code, wanted in reprice:
        offer = live[code]
        for later in offer["scheduled"]:
            client.delete(f"subscriptionIntroductoryOffers/{later['id']}")
        if offer["start"] >= start:
            client.delete(f"subscriptionIntroductoryOffers/{offer['id']}")
        elif offer["end"] != start:
            client.patch(
                f"subscriptionIntroductoryOffers/{offer['id']}",
                {"data": {"type": "subscriptionIntroductoryOffers", "id": offer["id"], "attributes": {"endDate": start}}},
            )
        _create_intro(client, subscription_id, code, intro, _point_at(client, subscription_id, code, wanted), start)


## -- the run ----------------------------------------------------------------------------------------


def sync_subscriptions(client: AppStoreConnectClient, *, bundle_id: str, execute: bool) -> Report:
    """Plan (by default) or bring App Store Connect's subscriptions to ``subscriptions.json``."""
    settings = apple_settings()
    group, products = load_products()
    report = Report(execute=execute)
    report.section(f"App Store subscriptions of {bundle_id}")
    app = resolve_app(client, bundle_id)
    group_id = ensure_group(client, app["id"], group, settings, report)
    for product in products:
        ensure_product(client, group_id, product, settings, report)
    print(report.summary())
    return report


__all__ = [
    "AppleProduct",
    "SubscriptionsFileError",
    "current_prices",
    "intro_offers",
    "load_products",
    "sync_subscriptions",
]
