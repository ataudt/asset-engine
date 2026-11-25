"""Write the stores' live settings and subscriptions into an app's listing source.

Layer: asset engine, store publishing. Reads the stores, writes only the repo.
Rules:
  - ``pull`` is how an app moves onto the engine: it writes ``store.json`` and ``subscriptions.json``
    from what the stores hold, so that the first plan afterwards is empty — the proof that the repo
    describes the store. After that the files are the source and ``pull`` is not run again; a change
    made in a console is mirrored by hand or by pulling once more and reviewing the diff.
  - Merged, never replaced: a product is matched by its id, each store's half of it is overwritten
    from that store, the other half and every ``_comment`` stay as they are.
  - Prices are pulled for the territories (Apple) and regions (Play) an app manages, not for every
    one a store has: Apple fills the rest from the base territory and adjusts them itself, and the
    engine leaves them alone. Apple: the explicit ``territories`` of ``store.json``, else the base
    territory, plus ``--territories``. Play: ``play.regions`` of ``store.json``, else ``--regions``,
    else every region the base plan sells in.
  - A shape the file cannot express (two subscription groups) raises with what was found, rather
    than writing half of it.
"""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path

from asset_engine.project import get_project
from asset_engine.store.listing import appstore_dir, store_locales
from asset_engine.store.report import Report


class PullUnsupported(RuntimeError):
    """The store holds something ``subscriptions.json`` cannot express yet."""


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _write(path: Path, data: dict, report: Report) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    old = path.read_text(encoding="utf-8") if path.is_file() else ""
    if old == text:
        report.ok(f"{path.name}: unchanged")
        return
    report.change(f"{path.name}: written from the store")
    path.write_text(text, encoding="utf-8")


def _by_product(subscriptions: dict) -> dict[str, dict]:
    return {entry["productId"]: entry for entry in subscriptions.get("subscriptions", [])}


def _localized(texts: dict[str, str], tags: dict[str, str], primary: str) -> str | dict[str, str]:
    """A plain string when only the primary language carries copy, else ``{app locale: text}``."""
    if set(texts) <= {primary}:
        return texts.get(primary, "")
    codes = {tag: code for code, tag in tags.items()}
    return {codes[tag]: text for tag, text in sorted(texts.items()) if tag in codes}


## -- Apple -------------------------------------------------------------------------------------------


def pull_apple(client, *, bundle_id: str, extra_territories: list[str]) -> Report:
    from asset_engine.store.connect.app import API_V2, app_info, current_version, resolve_app
    from asset_engine.store.connect.subscriptions import current_prices, intro_offers

    report = Report(execute=True)
    report.section(f"pull App Store settings and subscriptions of {bundle_id}")
    root = appstore_dir()
    store_path, subs_path = root / "store.json", root / "subscriptions.json"
    store, subscriptions = _load(store_path), _load(subs_path)
    tags = store_locales("apple")

    app = resolve_app(client, bundle_id)
    app_id = app["id"]
    version = current_version(client, app_id)
    info = app_info(client, app_id, editable_only=False)
    relationships = info.get("relationships") or {}
    availability = client.get_or_none(f"apps/{app_id}/appAvailabilityV2")
    territories: list[str] | str = "ALL"
    available_new = False
    if availability is not None:
        available_new = bool(availability["data"]["attributes"].get("availableInNewTerritories"))
        rows = list(
            client.list_all(
                f"{API_V2}/appAvailabilities/{availability['data']['id']}/territoryAvailabilities",
                include="territory",
                limit=200,
            )
        )
        on = sorted(row["relationships"]["territory"]["data"]["id"] for row in rows if row["attributes"].get("available"))
        if len(on) < len(rows):
            territories = on
    schedule = client.get(f"apps/{app_id}/appPriceSchedule")
    base = (client.get(f"appPriceSchedules/{schedule['data']['id']}/baseTerritory").get("data") or {}).get("id")

    apple = dict(store.get("apple", {}))
    apple.update(
        {
            "territories": territories,
            "availableInNewTerritories": available_new,
            "baseTerritory": base,
            "primaryLocale": app["attributes"]["primaryLocale"],
            "contentRights": app["attributes"].get("contentRightsDeclaration"),
            "copyright": (version or {}).get("attributes", {}).get("copyright"),
            "primaryCategory": ((relationships.get("primaryCategory") or {}).get("data") or {}).get("id"),
            "secondaryCategory": ((relationships.get("secondaryCategory") or {}).get("data") or {}).get("id"),
            "releaseType": (version or {}).get("attributes", {}).get("releaseType") or "MANUAL",
        }
    )
    apple.setdefault("removeOtherLanguages", False)
    store["apple"] = apple

    priced = sorted(set((territories if isinstance(territories, list) else [base]) + extra_territories))
    groups = list(client.list_all(f"apps/{app_id}/subscriptionGroups", limit=50))
    if len(groups) > 1:
        raise PullUnsupported(
            f"{len(groups)} subscription groups ({', '.join(g['attributes']['referenceName'] for g in groups)}); "
            f"subscriptions.json holds one"
        )
    if groups:
        group = groups[0]
        group_names = {
            loc["attributes"]["locale"]: loc["attributes"]["name"]
            for loc in client.list_all(f"subscriptionGroups/{group['id']}/subscriptionGroupLocalizations", limit=50)
        }
        subscriptions["group"] = {
            "referenceName": group["attributes"]["referenceName"],
            "name": _localized(group_names, tags, apple["primaryLocale"]),
        }
        entries = _by_product(subscriptions)
        order = [entry["productId"] for entry in subscriptions.get("subscriptions", [])]
        for row in client.list_all(f"subscriptionGroups/{group['id']}/subscriptions", limit=50):
            attributes = row["attributes"]
            product_id = attributes["productId"]
            locs = list(client.list_all(f"subscriptions/{row['id']}/subscriptionLocalizations", limit=50))
            names = {loc["attributes"]["locale"]: loc["attributes"].get("name") or "" for loc in locs}
            descriptions = {loc["attributes"]["locale"]: loc["attributes"].get("description") or "" for loc in locs}
            current, _ = current_prices(client, row["id"])
            intro = intro_offers(client, row["id"])
            entry = dict(entries.get(product_id, {"productId": product_id}))
            entry.pop("introOffer", None)
            if intro:
                shapes = {(o["mode"], o["duration"], o["periods"]) for o in intro.values()}
                if len(shapes) > 1:
                    raise PullUnsupported(f"{product_id}: {len(shapes)} different introductory offers ({sorted(shapes)})")
                mode, duration, periods = next(iter(shapes))
                offer = {"mode": mode, "duration": duration, "periods": periods}
                if mode != "FREE_TRIAL":
                    offer["prices"] = {code: f"{intro[code]['price']:.2f}" for code in priced if code in intro}
                entry["introOffer"] = offer
            entry.update(
                {
                    "referenceName": attributes.get("name"),
                    "subscriptionPeriod": attributes.get("subscriptionPeriod"),
                    "groupLevel": attributes.get("groupLevel"),
                    "prices": {code: f"{current[code]:.2f}" for code in priced if code in current},
                    "name": _localized(names, tags, apple["primaryLocale"]),
                    "description": _localized(descriptions, tags, apple["primaryLocale"]),
                    "reviewNote": attributes.get("reviewNote") or "",
                }
            )
            entries[product_id] = entry
            if product_id not in order:
                order.append(product_id)
            report.ok(f"{product_id}: {attributes.get('state')}, priced in {len(entry['prices'])} territories")
        subscriptions["subscriptions"] = [entries[product_id] for product_id in order]
    _write(store_path, store, report)
    _write(subs_path, subscriptions, report)
    report.note(f"pulled {date.today().isoformat()}; review the diff, then run the plans")
    return report


## -- Play ----------------------------------------------------------------------------------------------


def _play_key(entry: dict) -> tuple[str, str] | None:
    play = entry.get("play")
    if not play:
        return None
    return play.get("productId", entry.get("productId")), play["basePlanId"]


def pull_play(client, *, regions: list[str]) -> Report:
    """Link every Play base plan to an entry and write its prices; write a shared subscription's listing.

    A base plan already linked (``play.productId`` / ``basePlanId``) keeps its entry. Otherwise it is
    linked to the entry of the same product id, or else to the one entry of its billing period that
    has no Play half yet (kcalculate: Apple's ``kcalculate_monthly_pro`` is Play's
    ``pro_subscription_id/monthly-plan-id``). A base plan with no counterpart becomes an entry of its
    own, Play only.
    """
    from asset_engine.store.play.subscriptions import PERIODS, Price, live_offers

    report = Report(execute=True)
    report.section(f"pull Play subscriptions of {client.package_name}")
    root = appstore_dir()
    store_path, subs_path = root / "store.json", root / "subscriptions.json"
    store, subscriptions = _load(store_path), _load(subs_path)
    managed = regions or list(store.get("play", {}).get("regions", []))
    periods = {iso: name for name, iso in PERIODS.items()}
    tags = store_locales("play")
    primary = tags[get_project().source_locale]
    entries: list[dict] = [dict(entry) for entry in subscriptions.get("subscriptions", [])]
    shared = dict(subscriptions.get("playProducts", {}))
    seen_regions: set[str] = set()
    for row in client.list_all("subscriptions", "subscriptions"):
        product_id = row["productId"]
        plans = row.get("basePlans", [])
        for plan in plans:
            base_plan_id = plan["basePlanId"]
            period = periods.get((plan.get("autoRenewingBasePlanType") or {}).get("billingPeriodDuration"))
            entry = next((e for e in entries if _play_key(e) == (product_id, base_plan_id)), None)
            if entry is None:
                entry = next((e for e in entries if not e.get("play") and e.get("productId") == product_id), None)
            if entry is None:
                candidates = [e for e in entries if not e.get("play") and e.get("subscriptionPeriod") == period]
                entry = candidates[0] if len(candidates) == 1 else None
            if entry is None:
                entry = {"productId": product_id, "subscriptionPeriod": period}
                entries.append(entry)
                report.note(f"{product_id}/{base_plan_id}: no App Store counterpart — a Play-only entry")
            configs = {config["regionCode"]: config for config in plan.get("regionalConfigs", [])}
            seen_regions |= set(configs)
            wanted = managed or sorted(configs)
            play = {"basePlanId": base_plan_id}
            if entry.get("productId") != product_id:
                play = {"productId": product_id, **play}
            play["prices"] = {region: str(Price.from_money(configs[region]["price"])) for region in wanted if region in configs}
            offers = live_offers(client, product_id, base_plan_id)
            if offers:
                play["offers"] = offers
            entry["play"] = play
            report.ok(f"{product_id}/{base_plan_id}: {plan.get('state')}, priced in {len(play['prices'])} regions")
        listings = {listing["languageCode"]: listing for listing in row.get("listings", [])}
        live_listing = {
            "name": _localized({tag: row.get("title", "") for tag, row in listings.items()}, tags, primary),
            "description": _localized({tag: row.get("description", "") for tag, row in listings.items()}, tags, primary),
            "benefits": next((row.get("benefits") for row in listings.values() if row.get("benefits")), []),
        }
        owners = [e for e in entries if (_play_key(e) or ("", ""))[0] == product_id]
        derived = owners[0] if len(owners) == 1 else None
        if derived is not None and "name" not in derived:
            derived["name"], derived["description"] = live_listing["name"], live_listing["description"]
        if (
            derived is None
            or (derived.get("name"), derived.get("description")) != (live_listing["name"], live_listing["description"])
        ):
            shared[product_id] = live_listing
        elif live_listing["benefits"]:
            derived["play"]["benefits"] = live_listing["benefits"]
    subscriptions["subscriptions"] = entries
    if shared:
        subscriptions["playProducts"] = shared
    store["play"] = {**store.get("play", {}), "regions": managed or sorted(seen_regions)}
    _write(store_path, store, report)
    _write(subs_path, subscriptions, report)
    report.note(f"pulled {date.today().isoformat()}; review the diff, then run the plans")
    return report


__all__ = ["PullUnsupported", "pull_apple", "pull_play"]
