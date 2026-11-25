"""The app's subscriptions on Google Play, from ``appstore/subscriptions.json``.

Layer: asset engine, store publishing.
Rules:
  - The file is the source. An entry is one plan a customer buys (one period); its ``play`` block
    names where it lives on Play: the subscription (``productId``, by default the entry's own) and
    the base plan (``basePlanId``), and the price per Play region, written with its currency
    (``"5.99 EUR"``). Several entries may share one Play subscription, each its own base plan (one
    subscription with a monthly and a yearly plan, as kcalculate and NutriSpy have it). The currency
    is checked against Play's own for the region, so a price is never read in the wrong money. The
    managed regions are ``store.json`` → ``play.regions``, and every entry prices every one of them.
  - A Play subscription's listing (title, description, benefits) is ``playProducts.<productId>`` where
    several entries share it, else the entry's ``name`` and ``description`` — a string in the source
    locale, or ``{locale: text}`` — with the ``play`` block's ``benefits``.
  - Read first, then plan; with ``execute`` only what differs is written. A missing subscription is
    created with its base plans, which are activated; a missing base plan or region is added.
  - A price that differs is set. Where it went **down**, the region's existing subscribers are
    migrated to it too (``basePlans:migratePrices``), so nobody renews at more than a newcomer pays
    (NutriSpy, Aaron, 2026-11-21). A raise is never migrated here: that is an opt-in or notified
    price increase with its own rules, taken in the console.
  - What cannot change, or is a decision, is reported, never forced: a base plan's billing period, a
    region the store sells in that the file does not manage, a base plan or subscription the file
    does not name.
  - A base plan's ``offers`` (free trials, relative discounts) are compared and reported, never
    written: they are console decisions, and a relative discount follows the base price by itself.
  - Every price write names Play's current regions version, read live: Play refuses a write in an
    older version than the one a subscription already uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from asset_engine.project import get_project
from asset_engine.store.listing import read_json, store_locales
from asset_engine.store.play.client import PlayClient
from asset_engine.store.report import Report


## Apple's period names (the file's ``subscriptionPeriod``) as Play's ISO 8601 billing periods.
PERIODS = {
    "ONE_WEEK": "P1W",
    "ONE_MONTH": "P1M",
    "TWO_MONTHS": "P2M",
    "THREE_MONTHS": "P3M",
    "SIX_MONTHS": "P6M",
    "ONE_YEAR": "P1Y",
}
## Play's caps on a subscription listing.
TITLE_CAP = 55
DESCRIPTION_CAP = 80
BENEFITS_MAX = 4
BENEFIT_CAP = 40


class SubscriptionsFileError(ValueError):
    """``subscriptions.json`` or ``store.json`` does not describe a valid set of Play products."""


@dataclass(frozen=True)
class Price:
    amount: Decimal
    currency: str

    @classmethod
    def parse(cls, raw: str, *, where: str) -> Price:
        parts = raw.split()
        if len(parts) != 2:
            raise SubscriptionsFileError(f"{where}: price {raw!r} is not '<amount> <currency>', e.g. '5.99 EUR'")
        return cls(Decimal(parts[0]), parts[1].upper())

    @classmethod
    def from_money(cls, money: dict) -> Price:
        return cls(
            Decimal(money.get("units") or "0") + Decimal(money.get("nanos") or 0) / Decimal(10**9),
            money["currencyCode"],
        )

    def money(self) -> dict:
        units = int(self.amount)
        return {"currencyCode": self.currency, "units": str(units), "nanos": int((self.amount - units) * 10**9)}

    def __str__(self) -> str:
        return f"{self.amount:.2f} {self.currency}"


@dataclass(frozen=True)
class PlayBasePlan:
    base_plan_id: str
    period: str
    prices: dict[str, Price]
    ## The base plan's offers as ``offer_summary`` gives them, or ``None`` when the file states none.
    offers: list[dict] | None = None


def _phase_summary(phase: dict) -> dict:
    """One offer phase in the file's words: its duration and repeat, and what it charges — the same in
    every region (``free``, a ``relativeDiscount``) or ``perRegion`` where it differs by region."""
    kinds = set()
    for config in phase.get("regionalConfigs", []):
        if "free" in config:
            kinds.add(("free", None))
        elif "relativeDiscount" in config:
            kinds.add(("relativeDiscount", float(config["relativeDiscount"])))
        elif "absoluteDiscount" in config:
            kinds.add(("absoluteDiscount", None))
        elif "price" in config:
            kinds.add(("price", None))
    summary = {"duration": phase.get("duration"), "recurrenceCount": phase.get("recurrenceCount", 1)}
    if len(kinds) == 1:
        kind, value = next(iter(kinds))
        if kind in ("free", "relativeDiscount"):
            summary[kind] = True if kind == "free" else value
            return summary
    summary["perRegion"] = sorted(kind for kind, _ in kinds)
    return summary


def offer_summary(offer: dict) -> dict:
    return {"offerId": offer["offerId"], "phases": [_phase_summary(phase) for phase in offer.get("phases", [])]}


@dataclass(frozen=True)
class PlaySubscription:
    product_id: str
    ## Play language tag -> {"languageCode", "title", "description", "benefits"}.
    listings: dict[str, dict]
    base_plans: list[PlayBasePlan] = field(default_factory=list)


def _localized(value: str | dict, *, source_tag: str, tags: dict[str, str]) -> dict[str, str]:
    if isinstance(value, str):
        return {source_tag: value}
    return {tags[code]: text for code, text in value.items() if code in tags}


def play_regions() -> list[str]:
    return list(read_json("store.json")["play"]["regions"])


def _listings(product_id: str, name, description, benefits: list[str], *, source_tag: str, tags: dict[str, str]) -> dict[str, dict]:
    titles = _localized(name, source_tag=source_tag, tags=tags)
    descriptions = _localized(description, source_tag=source_tag, tags=tags)
    if len(benefits) > BENEFITS_MAX or any(len(benefit) > BENEFIT_CAP for benefit in benefits):
        raise SubscriptionsFileError(f"{product_id}: at most {BENEFITS_MAX} benefits of {BENEFIT_CAP} chars")
    listings: dict[str, dict] = {}
    for tag, title in titles.items():
        text = descriptions.get(tag, "")
        if len(title) > TITLE_CAP or len(text) > DESCRIPTION_CAP:
            raise SubscriptionsFileError(
                f"{product_id} [{tag}]: Play caps a title at {TITLE_CAP} and a description at {DESCRIPTION_CAP} chars"
            )
        listings[tag] = {"languageCode": tag, "title": title, "description": text, "benefits": list(benefits)}
    return listings


def load_products() -> list[PlaySubscription]:
    """The Play subscriptions of ``subscriptions.json``, checked against Play's caps and ``store.json``."""
    data = read_json("subscriptions.json")
    regions = set(play_regions())
    tags = store_locales("play")
    source_tag = tags[get_project().source_locale]
    shared = data.get("playProducts", {})
    subscriptions: dict[str, PlaySubscription] = {}
    for entry in data["subscriptions"]:
        play = entry.get("play")
        if play is None:
            ## An App Store-only entry.
            continue
        product_id = play.get("productId", entry["productId"])
        if entry["subscriptionPeriod"] not in PERIODS:
            raise SubscriptionsFileError(f"{entry['productId']}: period {entry['subscriptionPeriod']} has no Play equivalent")
        prices = {region: Price.parse(raw, where=f"{product_id} {region}") for region, raw in play["prices"].items()}
        if set(prices) != regions:
            raise SubscriptionsFileError(
                f"{product_id}/{play['basePlanId']}: Play prices for {sorted(prices)}, store.json manages {sorted(regions)}"
            )
        if product_id not in subscriptions:
            listing = shared.get(product_id) or {
                "name": entry["name"],
                "description": entry["description"],
                "benefits": play.get("benefits") or [],
            }
            subscriptions[product_id] = PlaySubscription(
                product_id=product_id,
                listings=_listings(
                    product_id,
                    listing["name"],
                    listing["description"],
                    listing.get("benefits") or [],
                    source_tag=source_tag,
                    tags=tags,
                ),
            )
        subscriptions[product_id].base_plans.append(
            PlayBasePlan(
                base_plan_id=play["basePlanId"],
                period=PERIODS[entry["subscriptionPeriod"]],
                prices=prices,
                offers=play.get("offers"),
            )
        )
    return list(subscriptions.values())


## -- Play's regions -------------------------------------------------------------------------------


def region_currencies(client: PlayClient) -> tuple[str, dict[str, str]]:
    """Play's current regions version and each region's currency, from one price conversion."""
    answer = client.post("pricing:convertRegionPrices", {"price": {"currencyCode": "EUR", "units": "1"}})
    currencies = {region: row["price"]["currencyCode"] for region, row in answer["convertedRegionPrices"].items()}
    return answer["regionVersion"]["version"], currencies


def _check_currencies(products: list[PlaySubscription], currencies: dict[str, str]) -> None:
    for product in products:
        for plan in product.base_plans:
            for region, price in plan.prices.items():
                if region not in currencies:
                    raise SubscriptionsFileError(f"{product.product_id}: Play sells in no region {region!r}")
                if currencies[region] != price.currency:
                    raise SubscriptionsFileError(
                        f"{product.product_id}/{plan.base_plan_id} {region}: priced in {price.currency}, Play bills "
                        f"{region} in {currencies[region]}"
                    )


## -- sync -------------------------------------------------------------------------------------------


def _base_plan(plan: PlayBasePlan, *, legacy_compatible: bool) -> dict:
    return {
        "basePlanId": plan.base_plan_id,
        "autoRenewingBasePlanType": {"billingPeriodDuration": plan.period, "legacyCompatible": legacy_compatible},
        "regionalConfigs": [
            {"regionCode": region, "newSubscriberAvailability": True, "price": price.money()}
            for region, price in sorted(plan.prices.items())
        ],
    }


def _activate(client: PlayClient, product_id: str, base_plan_id: str) -> None:
    client.post(
        f"subscriptions/{product_id}/basePlans/{base_plan_id}:activate",
        {"packageName": client.package_name, "productId": product_id, "basePlanId": base_plan_id},
    )


def _create(client: PlayClient, product: PlaySubscription, version: str, report: Report) -> None:
    for plan in product.base_plans:
        prices = ", ".join(f"{region} {price}" for region, price in sorted(plan.prices.items()))
        report.change(f"{product.product_id}: create base plan {plan.base_plan_id} ({plan.period}), {prices}")
    if not report.execute:
        return
    client.post(
        "subscriptions",
        {
            "packageName": client.package_name,
            "productId": product.product_id,
            "listings": list(product.listings.values()),
            ## The first plan is the one older billing libraries see as "the" subscription; Play allows one.
            "basePlans": [_base_plan(plan, legacy_compatible=index == 0) for index, plan in enumerate(product.base_plans)],
        },
        productId=product.product_id,
        **{"regionsVersion.version": version},
    )
    for plan in product.base_plans:
        _activate(client, product.product_id, plan.base_plan_id)


def _sync_listings(client: PlayClient, product: PlaySubscription, live: dict, version: str, report: Report) -> None:
    current = {row["languageCode"]: row for row in live.get("listings", [])}
    keys = ("title", "description", "benefits")
    differing = [
        tag
        for tag, wanted in product.listings.items()
        if {k: current.get(tag, {}).get(k, [] if k == "benefits" else "") for k in keys} != {k: wanted[k] for k in keys}
    ]
    if not differing:
        report.ok(f"{product.product_id}: listing")
        return
    report.change(f"{product.product_id}: listing {', '.join(differing)}")
    if report.execute:
        listings = {**current, **{tag: product.listings[tag] for tag in differing}}
        client.patch(
            f"subscriptions/{product.product_id}",
            {"packageName": client.package_name, "productId": product.product_id, "listings": list(listings.values())},
            updateMask="listings",
            **{"regionsVersion.version": version},
        )


def _sync_base_plans(client: PlayClient, product: PlaySubscription, live: dict, version: str, report: Report) -> None:
    live_plans = {plan["basePlanId"]: plan for plan in live.get("basePlans", [])}
    dirty = False
    activate: list[str] = []
    lowered: dict[str, list[str]] = {}
    for plan in product.base_plans:
        label = f"{product.product_id}/{plan.base_plan_id}"
        current_plan = live_plans.get(plan.base_plan_id)
        if current_plan is None:
            report.change(f"{label}: add ({plan.period})")
            live.setdefault("basePlans", []).append(_base_plan(plan, legacy_compatible=False))
            dirty = True
            activate.append(plan.base_plan_id)
            continue
        period = (current_plan.get("autoRenewingBasePlanType") or {}).get("billingPeriodDuration")
        if period != plan.period:
            report.note(f"{label}: period {period}, file {plan.period} — a period never changes")
        configs = {config["regionCode"]: config for config in current_plan.get("regionalConfigs", [])}
        for region, wanted in sorted(plan.prices.items()):
            config = configs.get(region)
            if config is None:
                report.change(f"{label} {region}: add at {wanted}")
                current_plan.setdefault("regionalConfigs", []).append(
                    {"regionCode": region, "newSubscriberAvailability": True, "price": wanted.money()}
                )
                dirty = True
                continue
            current = Price.from_money(config["price"])
            available = config.get("newSubscriberAvailability", True)
            if current == wanted and available:
                report.ok(f"{label} {region}: {current}")
                continue
            if current != wanted:
                cut = wanted.amount < current.amount
                report.change(
                    f"{label} {region}: {current} -> {wanted} "
                    f"({'cut, existing subscribers migrated' if cut else 'raise, new subscribers only'})"
                )
                if cut:
                    lowered.setdefault(plan.base_plan_id, []).append(region)
            else:
                report.change(f"{label} {region}: open to new subscribers")
            config["price"] = wanted.money()
            config["newSubscriberAvailability"] = True
            dirty = True
        unmanaged = sorted(set(configs) - set(plan.prices))
        if unmanaged:
            report.note(
                f"{label}: {len(unmanaged)} more region(s) on Play, not managed by the file — left as they are"
            )
        if plan.offers is not None:
            _check_offers(client, product.product_id, plan, report)
        state = current_plan.get("state")
        if state == "DRAFT":
            report.change(f"{label}: activate (draft)")
            activate.append(plan.base_plan_id)
        elif state not in (None, "ACTIVE"):
            report.note(f"{label}: state {state} — reactivating is a console decision")
    for base_plan_id in sorted(set(live_plans) - {plan.base_plan_id for plan in product.base_plans}):
        report.note(
            f"{product.product_id}/{base_plan_id}: on Play ({live_plans[base_plan_id].get('state')}), not in the file — left as it is"
        )
    if not report.execute:
        return
    if dirty:
        client.patch(
            f"subscriptions/{product.product_id}", live, updateMask="basePlans", **{"regionsVersion.version": version}
        )
    for base_plan_id in activate:
        _activate(client, product.product_id, base_plan_id)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for base_plan_id, regions in lowered.items():
        client.post(
            f"subscriptions/{product.product_id}/basePlans/{base_plan_id}:migratePrices",
            {
                "packageName": client.package_name,
                "productId": product.product_id,
                "basePlanId": base_plan_id,
                "regionalPriceMigrations": [{"regionCode": region, "oldestAllowedPriceVersionTime": now} for region in regions],
                "regionsVersion": {"version": version},
            },
        )


def live_offers(client: PlayClient, product_id: str, base_plan_id: str) -> list[dict]:
    """The base plan's active offers, as ``offer_summary`` gives them."""
    rows = client.get(f"subscriptions/{product_id}/basePlans/{base_plan_id}/offers").get("subscriptionOffers", [])
    return [offer_summary(row) for row in rows if row.get("state") == "ACTIVE"]


def _check_offers(client: PlayClient, product_id: str, plan: PlayBasePlan, report: Report) -> None:
    """Offers are compared and reported, never written: a trial or a discount is a console decision,
    and a relative discount follows the base plan's price by itself."""
    label = f"{product_id}/{plan.base_plan_id}"
    live = {offer["offerId"]: offer for offer in live_offers(client, product_id, plan.base_plan_id)}
    wanted = {offer["offerId"]: offer for offer in plan.offers or []}
    for offer_id in sorted(set(wanted) | set(live)):
        if wanted.get(offer_id) == live.get(offer_id):
            report.ok(f"{label} offer {offer_id}")
        elif offer_id not in live:
            report.note(f"{label} offer {offer_id}: in the file, not active on Play — create it in the Play Console")
        elif offer_id not in wanted:
            report.note(f"{label} offer {offer_id}: active on Play, not in the file")
        else:
            report.note(f"{label} offer {offer_id}: differs from the file ({live[offer_id]['phases']}) — change it in the Play Console")


def sync_subscriptions(client: PlayClient, *, execute: bool) -> Report:
    """Plan (by default) or bring Play's subscriptions to ``subscriptions.json``."""
    report = Report(execute=execute)
    report.section(f"Play subscriptions of {client.package_name}")
    products = load_products()
    version, currencies = region_currencies(client)
    _check_currencies(products, currencies)
    live = {row["productId"]: row for row in client.list_all("subscriptions", "subscriptions")}
    for product in products:
        if product.product_id not in live:
            _create(client, product, version, report)
            continue
        _sync_listings(client, product, live[product.product_id], version, report)
        _sync_base_plans(client, product, live[product.product_id], version, report)
    for product_id in sorted(set(live) - {p.product_id for p in products}):
        report.note(f"{product_id}: on Play, not in subscriptions.json — left as it is")
    print(report.summary())
    return report


__all__ = [
    "PERIODS",
    "PlayBasePlan",
    "PlaySubscription",
    "Price",
    "SubscriptionsFileError",
    "live_offers",
    "load_products",
    "offer_summary",
    "play_regions",
    "region_currencies",
    "sync_subscriptions",
]
