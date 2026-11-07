from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bale_campaign_bot.bale.models import Chat, User
from bale_campaign_bot.services.contacts import ContactImportService
from bale_campaign_bot.services.subscribers import SubscriberService


async def test_contact_import_only_matches_linked_opt_in_users(
    test_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    subscribers = SubscriberService(test_session_factory)
    await subscribers.start(
        user=User(id=800, is_bot=False, first_name="Opted in"),
        chat=Chat(id=800, type="private"),
        source=None,
        consent_version="v1",
    )
    await subscribers.link_phone(800, "09123456789")
    service = ContactImportService(test_session_factory)

    report = await service.match("phone\n09123456789\n+989876543210\nnot a phone\n\n")

    assert (report.matched, report.unmatched, report.invalid) == (1, 1, 2)
    # Import reports do not create subscriptions or link the unmatched phone.
    assert await subscribers.stop(9_999) is False


async def test_contact_import_deduplicates_numbers_and_limits_rows(
    test_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = ContactImportService(test_session_factory)
    report = await service.match("09123456789\n+989123456789\n")
    assert (report.matched, report.unmatched, report.invalid) == (0, 1, 0)

    try:
        await service.match("\n".join("09123456789" for _ in range(50_002)))
    except ValueError as exc:
        assert "row limit" in str(exc)
    else:
        raise AssertionError("oversized row count should be rejected")
