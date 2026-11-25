"""CLI for publishing an app on the stores: listing, record and subscriptions, Apple and Play.

Layer: asset engine, store publishing. An app runs it through a one-line shim
(``python -m marketing.publish``) after installing its ``AssetProject``. It replaces the fastlane
lanes (``deliver``, ``supply``) the apps used to fork.
Rules:
  - Every command plans by default: it reads the store, prints what differs and writes nothing.
    ``--apply`` writes, and only what differs. The exit code is 0 for a plan or a completed write,
    1 for a failure, and 2 for a plan that found differences when ``--check`` asks the store to match
    the repo (a migration's acceptance test).
  - ``--language`` accepts repeats and comma-separated values and defaults to every locale the app
    publishes to that store (``listing.listed_languages``). A locale skipped for incomplete copy is
    named, never dropped silently.
  - The keys and ids come from ``PublishFiles``; a store an app has no id or key for is an error
    naming the field, not a guess.
  - ``listing --store apple`` also places the App Store creative assets (``connect.creatives``) on the
    version, after the localizations exist; ``record --store apple`` follows the listing on a first
    release, as the new primary language needs its localization first.
"""

from __future__ import annotations

import argparse
import logging
import sys

from asset_engine.project import PublishFiles
from asset_engine.store import listing as source
from asset_engine.store.listing import CapExceeded, FramesMissing, ListingIncomplete


logger = logging.getLogger(__name__)

EPILOG = """\
examples:
  {prog} listing --store apple                      # plan against the version in preparation (or live)
  {prog} listing --store apple --version 2026.12.1 --apply   # creates the version if none is in preparation
  {prog} listing --store apple --no-images          # text only
  {prog} record --store apple --apply               # availability, price, age rating, build, languages
  {prog} promo --store apple --apply                # promotional text on the live version, no build
  {prog} subscriptions --store apple
  {prog} listing --store play
  {prog} listing --store play --version-code 12 --track internal --apply   # with What's New
  {prog} subscriptions --store play --apply
  {prog} pull --store apple                         # write store.json / subscriptions.json from the store
  {prog} custom-pages --store apple --page histamine --language en,de   # plan one custom product page
  {prog} custom-pages --store play --page histamine  # the paste bundle for Play's custom listing
"""


def _split_csv(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        out.extend(part for part in (p.strip() for p in value.split(",")) if part)
    return out


def _languages(store: str, codes: list[str]) -> list[str]:
    """The requested locales that the app publishes to ``store``; unknown codes end the run."""
    known = source.store_locales(store)
    unknown = [code for code in codes if code not in known]
    if unknown:
        raise SystemExit(f"not {store} locales of this app: {', '.join(unknown)} (known: {', '.join(known)})")
    listed = source.listed_languages(store)
    chosen = list(dict.fromkeys(codes)) if codes else list(known)
    for language in chosen:
        if language not in listed:
            print(f"[{language}] SKIPPED: not listed, or its copy lacks a field {store} needs", file=sys.stderr)
    return [language for language in chosen if language in listed]


def _apple_client(files: PublishFiles):
    from asset_engine.store.connect import AppStoreConnectClient

    if not files.apple_bundle_id or files.apple_key is None:
        raise SystemExit("this app gives no App Store bundle id or key (PublishFiles.apple_bundle_id / apple_key)")
    return AppStoreConnectClient(key_getter=files.apple_key)


def _play_client(files: PublishFiles):
    from asset_engine.store.play.client import PlayClient

    if not files.play_package or files.play_key is None:
        raise SystemExit("this app gives no Play package or key (PublishFiles.play_package / play_key)")
    return PlayClient(package_name=files.play_package, key_getter=files.play_key)


def _store_errors() -> tuple[type[BaseException], ...]:
    from asset_engine.store.connect import NETWORK_ERRORS as APPLE_NETWORK_ERRORS
    from asset_engine.store.connect import AppStoreConnectError
    from asset_engine.store.connect.creatives import CreativesIncomplete
    from asset_engine.store.connect.subscriptions import SubscriptionsFileError as AppleFileError
    from asset_engine.store.play.client import NETWORK_ERRORS as PLAY_NETWORK_ERRORS
    from asset_engine.store.play.client import PlayApiError
    from asset_engine.store.play.subscriptions import SubscriptionsFileError as PlayFileError
    from asset_engine.store.connect.custom_pages import PageLocked, PageMissing
    from asset_engine.store.pull import PullUnsupported

    return (
        AppStoreConnectError,
        CreativesIncomplete,
        PlayApiError,
        AppleFileError,
        PlayFileError,
        PullUnsupported,
        PageLocked,
        PageMissing,
        ListingIncomplete,
        CapExceeded,
        FramesMissing,
        *APPLE_NETWORK_ERRORS,
        *PLAY_NETWORK_ERRORS,
    )


def _exit(args: argparse.Namespace, changes: int) -> int:
    return 2 if (args.check and changes) else 0


## -- commands -------------------------------------------------------------------------------------


def _cmd_listing(args: argparse.Namespace) -> int:
    files = source.publish_files()
    languages = _languages(args.store, _split_csv(args.language))
    if not languages:
        print("no locale to publish", file=sys.stderr)
        return 1
    images = not args.no_images
    if args.store == "play":
        from asset_engine.store.play.listing import upload_listing

        report = upload_listing(
            _play_client(files),
            [source.play_listing(language, images=images) for language in languages],
            execute=args.apply,
            track=args.track,
            version_code=args.version_code,
            known_tags=set(source.store_locales("play").values()),
        )
        return _exit(args, report.changes)

    from asset_engine.store.connect.creatives import upload_creatives
    from asset_engine.store.connect.listing import publish_listing

    client = _apple_client(files)
    listings = [source.apple_listing(language, images=images) for language in languages]
    report, version_string = publish_listing(
        client,
        bundle_id=files.apple_bundle_id,
        listings=listings,
        version_string=args.version,
        execute=args.apply,
        add_languages=args.add_languages,
    )
    if args.no_images:
        return _exit(args, report.changes)
    creatives = upload_creatives(
        client=client,
        bundle_id=files.apple_bundle_id,
        locales={language: source.store_locales("apple")[language] for language in languages},
        version_string=version_string if args.apply else None,
        execute=args.apply,
    )
    return _exit(args, report.changes + creatives.planned)


def _cmd_promo(args: argparse.Namespace) -> int:
    if args.store != "apple":
        raise SystemExit("promo is Apple's: Play has no promotional text")
    from asset_engine.store.connect.listing import publish_promotional_text

    files = source.publish_files()
    tags = source.store_locales("apple")
    codes = _split_csv(args.language) or list(tags)
    texts = {tags[code]: source.apple_promotional_text(code) for code in codes}
    report = publish_promotional_text(
        _apple_client(files), bundle_id=files.apple_bundle_id, texts=texts, execute=args.apply
    )
    return _exit(args, report.changes)


def _cmd_record(args: argparse.Namespace) -> int:
    if args.store != "apple":
        raise SystemExit("record is Apple's: on Play availability, rating and price are set in the console")
    from asset_engine.store.connect.record import apply_record

    files = source.publish_files()
    report = apply_record(
        _apple_client(files),
        bundle_id=files.apple_bundle_id,
        version_string=args.version,
        build_version=args.build_version,
        execute=args.apply,
    )
    return _exit(args, report.changes)


PLAY_PASTE = """\
# Custom store listing: {page}

The Google Play Developer API has no custom-store-listing resource (`edits.listings` is keyed by
language alone and *is* the default listing), so this is pasted into the Console by hand. A custom
listing differs from the main one in text only: create it with "Duplicate an existing listing" → the
default store listing, which copies every graphic, then paste the two fields per language below.
Play Console -> Grow -> Store presence -> Custom store listings; attach it to its keywords, country,
URL or segment there.

Regenerate with `{prog} custom-pages --store play --page {page}`.
"""


def _cmd_custom_pages(args: argparse.Namespace) -> int:
    from asset_engine.project import get_project

    files = source.publish_files()
    registry = get_project().pages
    pages = [registry.check_slug(slug) for slug in _split_csv(args.page)]
    codes = _split_csv(args.language)
    failed = 0
    changes = 0
    for page in pages:
        languages = codes or [code for code in registry.locales(page) if code in source.store_locales(args.store)]
        if args.store == "play":
            out = source.appstore_dir() / "build" / "play-custom-listings" / page
            out.mkdir(parents=True, exist_ok=True)
            parts = [PLAY_PASTE.format(page=page, prog=args.prog)]
            for language in languages:
                tag = source.store_locales("play")[language]
                short, full = source.play_page_texts(language, page)
                parts.append(f"---\n\n## {tag}\n\n### Short description\n\n```\n{short}\n```\n\n### Full description\n\n```\n{full}\n```\n")
            (out / "PASTE.md").write_text("\n".join(parts), encoding="utf-8")
            print(f"{page}: {len(languages)} language(s) -> {out / 'PASTE.md'}")
            continue
        from asset_engine.store.connect.custom_pages import custom_page_client, publish_page

        bundles = [source.page_bundle(language, page, images=not (args.no_images or args.keywords_only)) for language in languages]
        report = publish_page(
            custom_page_client(files.apple_key),
            bundle_id=files.apple_bundle_id,
            page=page,
            bundles=bundles,
            execute=args.apply,
            page_name=args.page_name,
            keywords_only=args.keywords_only,
        )
        changes += report.changes
        if report.refused:
            failed += 1
        if report.page_url:
            print(f"  url     {report.page_url}")
    return 1 if failed else _exit(args, changes)


def _cmd_pull(args: argparse.Namespace) -> int:
    from asset_engine.store.pull import pull_apple, pull_play

    files = source.publish_files()
    if args.store == "play":
        pull_play(_play_client(files), regions=_split_csv(args.regions))
    else:
        pull_apple(_apple_client(files), bundle_id=files.apple_bundle_id, extra_territories=_split_csv(args.territories))
    return 0


def _cmd_subscriptions(args: argparse.Namespace) -> int:
    files = source.publish_files()
    if args.store == "play":
        from asset_engine.store.play.subscriptions import sync_subscriptions as sync_play

        report = sync_play(_play_client(files), execute=args.apply)
    else:
        from asset_engine.store.connect.subscriptions import sync_subscriptions as sync_apple

        report = sync_apple(_apple_client(files), bundle_id=files.apple_bundle_id, execute=args.apply)
    return _exit(args, report.changes)


## -- parser ----------------------------------------------------------------------------------------


def build_parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Publish the app's listing, record and subscriptions on the stores. Plans unless --apply.",
        epilog=EPILOG.format(prog=prog),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(command: argparse.ArgumentParser, stores: tuple[str, ...] = ("apple", "play")) -> None:
        command.add_argument("--store", choices=stores, required=True)
        command.add_argument("--apply", action="store_true", help="Write what differs. Without it: plan only.")
        command.add_argument(
            "--check", action="store_true", help="Exit 2 when the plan finds differences (store must match the repo)."
        )

    def version(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--version",
            metavar="X.Y.Z",
            help="Apple: the App Store version. Default: the one in preparation. With --apply and none in "
            "preparation it is created; on a first release the record's '1.0' is renamed to it.",
        )

    listing = sub.add_parser("listing", help="Texts, screenshots, What's New; Apple also review details, creatives.")
    common(listing)
    version(listing)
    listing.add_argument("--language", action="append", metavar="CODE", help="Locale code; repeatable, comma-separated.")
    listing.add_argument("--no-images", action="store_true", help="Text only; leave every image as it is.")
    listing.add_argument(
        "--add-languages", action="store_true", help="Apple: create store languages the version does not have yet."
    )
    listing.add_argument("--version-code", type=int, metavar="N", help="Play: also write What's New on this build's release.")
    listing.add_argument("--track", metavar="TRACK", help="Play: the track that build sits on (default: production).")
    listing.set_defaults(func=_cmd_listing)

    promo = sub.add_parser("promo", help="Apple: the promotional text on the live version, without a build.")
    common(promo, ("apple",))
    promo.add_argument("--language", action="append", metavar="CODE", help="Locale code; repeatable, comma-separated.")
    promo.set_defaults(func=_cmd_promo)

    record = sub.add_parser(
        "record", help="Apple: content rights, age rating, availability, free price, build, store languages."
    )
    common(record, ("apple",))
    version(record)
    record.add_argument("--build-version", metavar="X.Y.Z", help="The binary's version to attach. Default: --version.")
    record.set_defaults(func=_cmd_record)

    subscriptions = sub.add_parser("subscriptions", help="Products, prices and copy of appstore/subscriptions.json.")
    common(subscriptions)
    subscriptions.set_defaults(func=_cmd_subscriptions)

    custom = sub.add_parser(
        "custom-pages",
        help="Apple: custom product pages as drafts (promotional text, keywords, screenshots); "
        "Play: the paste bundle for custom store listings.",
    )
    common(custom)
    custom.add_argument("--page", action="append", required=True, metavar="SLUG", help="Page slug; repeatable, comma-separated.")
    custom.add_argument("--language", action="append", metavar="CODE", help="Default: the locales the page carries copy for.")
    custom.add_argument("--page-name", metavar="NAME", help="Apple: the Console name, when the slug's name is reserved.")
    custom.add_argument("--keywords-only", action="store_true", help="Apple: sync the ticked keywords and nothing else.")
    custom.add_argument("--no-images", action="store_true", help="Apple: leave the screenshots alone.")
    custom.set_defaults(func=_cmd_custom_pages)

    pull = sub.add_parser(
        "pull", help="Write the store's settings and subscriptions into appstore/store.json and subscriptions.json."
    )
    pull.add_argument("--store", choices=("apple", "play"), required=True)
    pull.add_argument("--territories", action="append", metavar="ISO3", help="Apple: also pull prices of these territories.")
    pull.add_argument("--regions", action="append", metavar="ISO2", help="Play: pull prices of these regions only.")
    pull.set_defaults(func=_cmd_pull, check=False)
    return parser


def main(argv: list[str] | None = None, *, prog: str = "python -m marketing.publish") -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser(prog).parse_args(argv)
    args.prog = prog
    try:
        return args.func(args)
    except _store_errors():
        logger.exception("%s --store %s failed", args.command, args.store)
        return 1


__all__ = ["build_parser", "main"]
