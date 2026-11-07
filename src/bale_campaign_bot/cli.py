"""Operator CLI for onboarding, health checks, and database migrations."""

from __future__ import annotations

import argparse
import asyncio
import csv
import getpass
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

import yaml
from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy import text

from bale_campaign_bot.bale.client import BaleClient
from bale_campaign_bot.bale.errors import BaleError
from bale_campaign_bot.config.business import (
    BusinessConfigError,
    load_business_config,
    validate_business_config,
)
from bale_campaign_bot.config.settings import Settings
from bale_campaign_bot.db.repositories import SubscriberRepository
from bale_campaign_bot.db.session import create_engine, create_session_factory, ensure_sqlite_parent
from bale_campaign_bot.services.contacts import ContactImportService

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAX_CONTACT_FILE_BYTES = 1_000_000


def _ask(label: str, *, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    answer = input(f"{label}{suffix}: ").strip()
    return answer or (default or "")


def _ask_yes_no(label: str, *, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = input(f"{label} [{hint}]: ").strip().casefold()
    return default if not answer else answer in {"y", "yes"}


def _atomic_private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _business_answers() -> dict[str, object]:
    print("Business setup / راه‌اندازی کسب‌وکار")
    business: dict[str, object] = {
        "name": _ask("Business name / نام کسب‌وکار"),
        "short_description": _ask("Short description / معرفی کوتاه"),
        "welcome_message": _ask("Welcome message / پیام خوش‌آمد"),
        "about_text": _ask("About text / درباره کسب‌وکار"),
        "contact": {
            "phone": _ask("Public phone (optional) / تلفن عمومی (اختیاری)"),
            "website": _ask("Website URL (optional) / وب‌سایت (اختیاری)"),
            "address": _ask("Address (optional) / نشانی (اختیاری)"),
            "social_links": [
                item.strip()
                for item in _ask(
                    "Social links, comma-separated (optional) / شبکه‌های اجتماعی"
                ).split(",")
                if item.strip()
            ],
        },
        "working_hours": _ask("Working hours (optional) / ساعات کاری"),
        "support_contact": _ask("Support contact (optional) / راه ارتباطی پشتیبانی"),
        "consent_text": _ask("Opt-in consent text / متن رضایت برای عضویت"),
        "consent_version": _ask("Consent text version / نسخه متن رضایت", default="1"),
        "phone_linking_enabled": _ask_yes_no(
            "Allow subscribers to share their phone number? / دریافت شماره با رضایت کاربر؟"
        ),
        "opt_out_confirmation_text": _ask(
            "Opt-out confirmation / پیام تأیید لغو عضویت",
            default="عضویت شما لغو شد. برای عضویت دوباره /start را بفرستید.",
        ),
        "footer_enabled": False,
        "timezone": _ask("IANA timezone / منطقه زمانی", default="Asia/Tehran"),
        "locale": _ask("Locale (fa/en) / زبان", default="fa"),
    }
    buttons: list[dict[str, str]] = []
    while _ask_yes_no("Add a menu button? / افزودن دکمه منو؟"):
        label = _ask("Button label / عنوان دکمه")
        action_type = _ask("Action type: url or callback / نوع: url یا callback", default="url")
        action = _ask("URL or callback data / نشانی یا داده callback")
        buttons.append({"label": label, action_type: action})
    business["default_menu"] = buttons
    business["footer_enabled"] = _ask_yes_no(
        "Enable a separate footer message? / فعال‌کردن پیام پایانی؟"
    )
    if business["footer_enabled"]:
        business["footer_text"] = _ask("Footer text / متن پیام پایانی")
    defaults: dict[str, object] = {
        "send_rate_per_second": float(_ask("Default sends per second", default="10")),
        "concurrency": int(_ask("Concurrent sends", default="5")),
        "quiet_hours": {
            "enabled": _ask_yes_no("Enable quiet hours? / فعال‌کردن ساعات سکوت؟"),
            "start": _ask("Quiet-hours start (HH:MM)", default="22:00"),
            "end": _ask("Quiet-hours end (HH:MM)", default="08:00"),
        },
    }
    return {"business": business, "campaign_defaults": defaults}


async def run_init(
    *,
    token_reader: Callable[[], str] = getpass.getpass,
    env_path: Path | None = None,
    business_path: Path | None = None,
) -> int:
    env_file = env_path or PROJECT_ROOT / ".env"
    config_file = business_path or PROJECT_ROOT / "config" / "business.yaml"
    existing = [path for path in (env_file, config_file) if path.exists()]
    if existing and not _ask_yes_no(
        "Files already exist: " + ", ".join(str(path) for path in existing) + ". Overwrite?"
    ):
        print("Initialization cancelled; existing files were not changed.")
        return 1

    token = token_reader().strip()
    if not token:
        print("Bot token is required / توکن بازو الزامی است")
        return 1
    if any(character.isspace() for character in token):
        print("Bot token cannot contain whitespace / توکن نباید فاصله داشته باشد")
        return 1
    try:
        async with BaleClient(token) as client:
            bot = await client.get_me()
    except BaleError as exc:
        print(f"Token validation failed ({type(exc).__name__}, method={exc.method}).")
        return 1
    print(f"Validated bot / بازوی تأییدشده: @{bot.username or bot.first_name}")

    try:
        raw_config = _business_answers()
        validated = validate_business_config(raw_config)
    except (BusinessConfigError, ValueError) as exc:
        message = str(exc)
        print(f"پیکربندی معتبر نیست / Invalid business configuration: {message}")
        return 1

    relative_config = (
        config_file.relative_to(PROJECT_ROOT).as_posix()
        if config_file.is_relative_to(PROJECT_ROOT)
        else str(config_file)
    )
    quoted_token = '"' + token.replace("\\", "\\\\").replace('"', '\\"') + '"'
    env_content = "\n".join(
        (
            f"BALE_BOT_TOKEN={quoted_token}",
            "DATABASE_URL=sqlite+aiosqlite:///./data/bale_campaign_bot.db",
            "ADMIN_IDS=",
            "LOG_LEVEL=INFO",
            "LOG_JSON=true",
            "HTTPS_PROXY=",
            f"BUSINESS_CONFIG_PATH={relative_config}",
            f"SEND_RATE_PER_SECOND={validated.campaign_defaults.send_rate_per_second}",
            f"SEND_CONCURRENCY={validated.campaign_defaults.concurrency}",
            f"TIMEZONE={validated.business.timezone}",
            "UPDATE_MODE=polling",
            "WEBHOOK_PUBLIC_URL=",
            "WEBHOOK_PATH_SECRET=",
            "WEBHOOK_BIND_HOST=0.0.0.0",
            "WEBHOOK_BIND_PORT=8080",
            "WEBHOOK_MAX_BODY_BYTES=1000000",
            "",
        )
    )
    business_content = yaml.safe_dump(
        validated.model_dump(mode="json", exclude_none=True),
        allow_unicode=True,
        sort_keys=False,
    )
    _atomic_private_write(env_file, env_content)
    _atomic_private_write(config_file, business_content)
    print(f"Wrote {env_file} and {config_file} with mode 0600.")
    print(
        "Admin setup: no ID is hard-coded. Start the bot and claim ownership in a private chat "
        "with the one-time /claim code printed by the first run."
    )
    return 0


def _alembic_config(database_url: str) -> AlembicConfig:
    config = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    return config


def run_migrations(database_url: str) -> None:
    ensure_sqlite_parent(database_url)
    command.upgrade(_alembic_config(database_url), "head")


def _print_check(stream: TextIO, label: str, ok: bool, detail: str) -> None:
    status = "OK" if ok else "FAIL"
    print(f"[{status}] {label}: {detail}", file=stream)


def _safe_settings_errors(error: ValidationError) -> str:
    details = [
        f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
        for item in error.errors(include_input=False, include_context=False, include_url=False)
    ]
    return "\n".join(details)


def _csv_safe_cell(value: object) -> object:
    if isinstance(value, str) and value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


async def run_doctor(settings: Settings, *, stream: TextIO = sys.stdout) -> int:
    results: list[bool] = []

    try:
        business_config = load_business_config(settings.business_config_path)
        _print_check(stream, "business config", True, f"valid ({business_config.business.locale})")
        results.append(True)
    except BusinessConfigError as exc:
        _print_check(stream, "business config", False, str(exc))
        print(
            "Fix: edit BUSINESS_CONFIG_PATH and rerun `python -m bale_campaign_bot.cli doctor`.",
            file=stream,
        )
        results.append(False)

    engine = None
    try:
        engine = create_engine(settings.database_url)
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
            _print_check(stream, "database", True, "connection succeeded")
            results.append(True)
            try:
                current = await connection.scalar(text("SELECT version_num FROM alembic_version"))
                heads = ScriptDirectory.from_config(
                    _alembic_config(settings.database_url)
                ).get_heads()
                migrated = current is not None and current in heads
                _print_check(
                    stream,
                    "migrations",
                    migrated,
                    f"at {current}" if migrated else "database is not at the latest revision",
                )
                if not migrated:
                    print("Fix: run `python -m bale_campaign_bot.cli migrate`.", file=stream)
                results.append(migrated)
            except Exception:
                _print_check(
                    stream, "migrations", False, "alembic_version is missing or unreadable"
                )
                print("Fix: run `python -m bale_campaign_bot.cli migrate`.", file=stream)
                results.append(False)
    except Exception as exc:
        _print_check(stream, "database", False, type(exc).__name__)
        print("Fix: verify DATABASE_URL and that the database server is available.", file=stream)
        results.extend((False, False))
    finally:
        if engine is not None:
            await engine.dispose()

    token = settings.token_value
    if token is None:
        _print_check(stream, "Bale token", False, "BALE_BOT_TOKEN is not set")
        print("Fix: run `python -m bale_campaign_bot.cli init` or set BALE_BOT_TOKEN.", file=stream)
        results.extend((False, False))
    else:
        try:
            async with BaleClient(token, proxy=settings.https_proxy) as client:
                bot = await client.get_me()
                _print_check(
                    stream,
                    "Bale token",
                    True,
                    f"authenticated as @{bot.username or bot.first_name}",
                )
                results.append(True)
                try:
                    webhook = await client.get_webhook_info()
                    if settings.update_mode == "webhook":
                        secret = settings.webhook_path_secret
                        public_url = settings.webhook_public_url
                        expected = (
                            f"{public_url.rstrip('/')}/{secret.get_secret_value()}"
                            if public_url is not None and secret is not None
                            else ""
                        )
                        configured = webhook.url == expected
                        detail = (
                            "configured webhook matches settings"
                            if configured
                            else "webhook URL differs from settings"
                        )
                        _print_check(stream, "update intake", configured, detail)
                        if not configured:
                            print(
                                "Fix: check WEBHOOK_PUBLIC_URL/WEBHOOK_PATH_SECRET and "
                                "restart the bot.",
                                file=stream,
                            )
                        results.append(configured)
                    else:
                        polling = not webhook.url
                        detail = (
                            "long polling (no webhook URL)"
                            if polling
                            else "a webhook is still configured"
                        )
                        _print_check(stream, "update intake", polling, detail)
                        if not polling:
                            print(
                                "Fix: restart in UPDATE_MODE=polling; startup clears the webhook.",
                                file=stream,
                            )
                        results.append(polling)
                except BaleError as exc:
                    _print_check(
                        stream, "update intake", False, f"{type(exc).__name__} from {exc.method}"
                    )
                    print("Fix: verify bot API access and webhook configuration.", file=stream)
                    results.append(False)
        except BaleError as exc:
            _print_check(stream, "Bale token", False, f"{type(exc).__name__} from {exc.method}")
            print("Fix: verify BALE_BOT_TOKEN and outbound HTTPS access.", file=stream)
            results.extend((False, False))

    return 0 if all(results) else 1


async def run_import_contacts(
    settings: Settings, source: Path, *, stream: TextIO = sys.stdout
) -> int:
    try:
        if source.stat().st_size > MAX_CONTACT_FILE_BYTES:
            raise ValueError(f"file exceeds the {MAX_CONTACT_FILE_BYTES} byte limit")
        content = source.read_text(encoding="utf-8-sig")
        engine = create_engine(settings.database_url)
        try:
            report = await ContactImportService(create_session_factory(engine)).match(content)
        finally:
            await engine.dispose()
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"Contact import failed: {exc}", file=stream)
        return 1
    print(
        f"Matched linked subscribers: {report.matched}; unmatched (cannot be messaged): "
        f"{report.unmatched}; invalid: {report.invalid}",
        file=stream,
    )
    return 0


async def run_export_subscribers(
    settings: Settings,
    destination: Path,
    *,
    include_inactive: bool = False,
) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent, text=True
    )
    temporary_path = Path(temporary_name)
    os.fchmod(descriptor, 0o600)
    engine = create_engine(settings.database_url)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(
                ("chat_id", "first_name", "last_name", "username", "is_active", "phone_e164")
            )
            last_id = 0
            session_factory = create_session_factory(engine)
            while True:
                async with session_factory() as session:
                    rows = await SubscriberRepository(session).export_page(
                        after_id=last_id,
                        limit=1000,
                        include_inactive=include_inactive,
                    )
                if not rows:
                    break
                for subscriber_id, *values in rows:
                    writer.writerow([_csv_safe_cell(value) for value in values])
                    last_id = subscriber_id
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, destination)
        os.chmod(destination, 0o600)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    finally:
        await engine.dispose()
    print(f"Exported subscriber records to {destination} (mode 0600).")
    print("Phone numbers cannot be used to message people who have not started the bot.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bale-campaign-bot")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="validate the bot token and create configuration")
    subparsers.add_parser("doctor", help="check configuration, database, migrations, and Bale API")
    subparsers.add_parser("migrate", help="apply database migrations to head")
    import_parser = subparsers.add_parser(
        "import-contacts", help="match a CSV/text phone list to linked opt-in subscribers"
    )
    import_parser.add_argument("file", type=Path)
    export_parser = subparsers.add_parser("export", help="export subscriber records as private CSV")
    export_parser.add_argument("file", type=Path)
    export_parser.add_argument(
        "--include-inactive", action="store_true", help="include unsubscribed records"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "init":
        return asyncio.run(run_init())
    try:
        settings = Settings()
    except ValidationError as exc:
        print(
            f"Invalid settings / تنظیمات نامعتبر:\n{_safe_settings_errors(exc)}",
            file=sys.stderr,
        )
        return 2
    if args.command == "doctor":
        return asyncio.run(run_doctor(settings))
    if args.command == "migrate":
        run_migrations(settings.database_url)
        print("Database is migrated to head.")
        return 0
    if args.command == "import-contacts":
        return asyncio.run(run_import_contacts(settings, args.file))
    if args.command == "export":
        return asyncio.run(
            run_export_subscribers(
                settings,
                args.file,
                include_inactive=args.include_inactive,
            )
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
