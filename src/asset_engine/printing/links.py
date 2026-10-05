"""Where a printed product's QR codes point, and the forwarding pages behind them.

Layer: asset engine.
Rules:
  - A printed code cannot be corrected, so it never names a store. It names a page of our own,
    ``<base_url>/<path>/<campaign>/<link>/``, and that page forwards to the target. Moving the
    target later (a custom store page, a provider token, a new landing page) is an edit to
    the product's config and a ``links`` run, with the copies already handed out still working.
  - One code serves both stores. The ``app`` page reads the scanning device and forwards an
    iPhone to the App Store and an Android phone to Play; anything else, and a client without
    scripts, gets the landing page, which carries both badges. Two codes side by side cost the
    flyer half its artwork and made the reader pick the right one. An app with no store listing
    configured has no ``app`` link at all, only ``web``.
  - The landing page is a file of the site (``{language}/welcome.html``), which must exist before a
    code may point at it, or a route of a single-page site (``/``), which has no file to check.
  - The pages are generated, committed, and deployed with the website. A code whose page is not
    live is a dead flyer: deploy first, print second.
  - Nothing here deletes a page. A campaign dropped from a config may still be on paper.
  - The pages carry no tags. A scan is counted where it lands: the UTM parameters on the landing
    page, the install referrer on Play, the campaign token on the App Store.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode

from asset_engine.project import get_project


class LinkError(RuntimeError):
    """A code cannot be pointed where the config says."""


@dataclass(frozen=True)
class LinkSettings:
    """Where a product's codes point. ``source`` is the config file the settings were read from,
    named in every forwarding page so whoever finds one knows what regenerates it."""

    base_url: str
    site_dir: Path
    path: str
    utm_source: str
    utm_medium: str
    ## What a forwarding page is titled and links to, and the landing page per language
    ## (``{language}`` is replaced) a ``web`` code lands on.
    site_name: str
    landing_page: str
    ## How the pages are regenerated, named in the comment each page carries.
    command: str
    source: Path
    ## The store listing the ``app`` code forwards to; ``None`` for an app with none yet, which then
    ## has no ``app`` code.
    apple_id: str | None = None
    apple_provider_token: str = ""
    play_package: str | None = None

    @property
    def has_store(self) -> bool:
        return bool(self.apple_id and self.play_package)


def parse_links(raw: dict, *, source: Path) -> LinkSettings:
    """The ``links`` block of a product config. The store keys are optional, the rest required."""
    root = get_project().root
    return LinkSettings(
        base_url=raw["base_url"].rstrip("/"),
        site_dir=root / raw["site_dir"],
        path=raw["path"].strip("/"),
        utm_source=raw["utm_source"],
        utm_medium=raw["utm_medium"],
        site_name=raw["site_name"],
        landing_page=raw["landing_page"],
        command=raw["command"],
        source=source,
        apple_id=raw.get("apple_id") or None,
        apple_provider_token=raw.get("apple_provider_token", ""),
        play_package=raw.get("play_package") or None,
    )


@dataclass(frozen=True)
class Campaign:
    """One print run. Its name is the tag in every link and the folder of its forwarding pages."""

    name: str
    language: str


class LinkedConfig(Protocol):
    """What a product config needs to carry for its codes: the settings and its print runs."""

    links: LinkSettings
    campaigns: tuple[Campaign, ...]

WEB = "web"
APP = "app"
LINKS = (WEB, APP)

IOS = "ios"
ANDROID = "android"
## How the ``app`` page tells the stores apart, as the JavaScript pattern tested against the user
## agent, in the order tried. An iPad set to "desktop site" calls itself a Mac and falls through
## to the landing page, where the App Store badge is one tap away.
DEVICE_PATTERNS = {ANDROID: "/android/i", IOS: "/iPhone|iPad|iPod/"}


@dataclass(frozen=True)
class Link:
    """One QR code: the address printed in it, where that forwards to, and the page that does it.

    ``target`` is where every visitor ends up unless ``by_device`` names somewhere better for the
    device in hand; it is also what the page falls back to without scripts.
    """

    campaign: str
    name: str
    url: str
    target: str
    page: Path
    by_device: dict[str, str] = field(default_factory=dict)


def _utm(config: LinkedConfig, campaign: Campaign) -> dict[str, str]:
    return {
        "utm_source": config.links.utm_source,
        "utm_medium": config.links.utm_medium,
        "utm_campaign": campaign.name,
    }


def _landing_target(config: LinkedConfig, campaign: Campaign) -> str:
    landing_page = config.links.landing_page.format(language=campaign.language)
    if landing_page.startswith("/"):
        ## A route of a single-page site: the page is the site's own index, there is no file per
        ## route to check.
        return f"{landing_page}?{urlencode(_utm(config, campaign))}"
    landing = Path(landing_page)
    if not (config.links.site_dir / landing).is_file():
        raise LinkError(
            f"campaign {campaign.name!r} is in {campaign.language!r}, but the website has no "
            f"{landing} to send its codes to"
        )
    return f"/{landing.as_posix()}?{urlencode(_utm(config, campaign))}"


def _store_target(config: LinkedConfig, campaign: Campaign, device: str) -> str:
    links = config.links
    if device == IOS:
        ## No storefront in the path: Apple then serves the visitor's own. `ct` is ignored without
        ## the provider token, so the pair is added together or not at all.
        url = f"https://apps.apple.com/app/id{links.apple_id}"
        if links.apple_provider_token:
            url += "?" + urlencode({"pt": links.apple_provider_token, "ct": campaign.name})
        return url
    if device == ANDROID:
        ## Play hands `referrer` to the app on first open as one string, so the UTM set is encoded
        ## into it and then encoded again as a parameter value.
        query = {"id": links.play_package, "referrer": urlencode(_utm(config, campaign))}
        return f"https://play.google.com/store/apps/details?{urlencode(query)}"
    raise LinkError(f"unknown device {device!r} — expected one of {tuple(DEVICE_PATTERNS)}")


def campaign_links(config: LinkedConfig, campaign: Campaign) -> dict[str, Link]:
    """The links of one campaign, by name: ``web`` to the landing page, ``app`` to the stores.

    Without a store listing in the settings there is no ``app`` link; a layout asking for one fails
    on the lookup, by name.
    """
    links = config.links
    landing = _landing_target(config, campaign)
    names = LINKS if links.has_store else (WEB,)
    by_device = {
        APP: {device: _store_target(config, campaign, device) for device in DEVICE_PATTERNS}
        if links.has_store
        else {},
    }
    return {
        name: Link(
            campaign=campaign.name,
            name=name,
            ## The trailing slash is part of the address: without it the host answers with a
            ## redirect to the directory first, one more hop on a phone that just scanned a code.
            url=f"{links.base_url}/{links.path}/{campaign.name}/{name}/",
            target=landing,
            page=links.site_dir / links.path / campaign.name / name / "index.html",
            by_device=by_device.get(name, {}),
        )
        for name in names
    }


def _script(link: Link) -> str:
    """The forwarding statement: the device's own target where one is named, else ``target``."""

    def quoted(url: str) -> str:
        ## json.dumps quotes the URL for the script; "</" is broken up so no value can close the tag.
        return json.dumps(url).replace("</", "<\\/")

    if not link.by_device:
        return f"window.location.replace({quoted(link.target)});"
    choice = "".join(
        f"{DEVICE_PATTERNS[device]}.test(ua) ? {quoted(url)} : " for device, url in link.by_device.items()
    )
    return f'var ua = navigator.userAgent || ""; window.location.replace({choice}{quoted(link.target)});'


def render_page(config: LinkedConfig, link: Link, language: str) -> str:
    """The forwarding page for ``link``.

    Same shape as ``website/index.html``: the script forwards, ``<noscript>`` carries the refresh
    for a client without one, and the body link is what is left if both fail. ``noindex`` keeps
    the page out of search; it is not disallowed in ``robots.txt``, because a crawler that may not
    fetch a page never sees its ``noindex``.
    """
    attribute = html.escape(link.target, quote=True)
    project = get_project()
    source = config.links.source.relative_to(project.root).as_posix()
    name = html.escape(config.links.site_name)
    return (
        "<!DOCTYPE html>\n"
        f'<html lang="{html.escape(language, quote=True)}">\n'
        "<head>\n"
        '    <meta charset="UTF-8">\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        '    <meta name="robots" content="noindex">\n'
        f"    <title>{name}</title>\n"
        f"    <!-- Generated by `{config.links.command} links` for the printed QR code of\n"
        f"         campaign {link.campaign} ({link.name}). Do not edit or delete: change the target\n"
        f"         in {source} and regenerate. -->\n"
        f"    <script>{_script(link)}</script>\n"
        "    <noscript>\n"
        f'        <meta http-equiv="refresh" content="0; url={attribute}">\n'
        "    </noscript>\n"
        "</head>\n"
        "<body>\n"
        f'    <p><a href="{attribute}">{name}</a></p>\n'
        "</body>\n"
        "</html>\n"
    )


def write_pages(config: LinkedConfig) -> list[tuple[Link, bool]]:
    """Write every campaign's forwarding pages; returns each link and whether its file changed."""
    written: list[tuple[Link, bool]] = []
    for campaign in config.campaigns:
        for link in campaign_links(config, campaign).values():
            content = render_page(config, link, campaign.language)
            changed = not link.page.is_file() or link.page.read_text(encoding="utf-8") != content
            if changed:
                link.page.parent.mkdir(parents=True, exist_ok=True)
                link.page.write_text(content, encoding="utf-8")
            written.append((link, changed))
    return written


__all__ = [
    "ANDROID",
    "APP",
    "DEVICE_PATTERNS",
    "IOS",
    "LINKS",
    "WEB",
    "Campaign",
    "Link",
    "LinkError",
    "LinkSettings",
    "LinkedConfig",
    "parse_links",
    "campaign_links",
    "render_page",
    "write_pages",
]
