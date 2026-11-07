"""Opt-in-safe matching for operator supplied phone lists."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bale_campaign_bot.db.repositories import SubscriberRepository
from bale_campaign_bot.services.subscribers import normalize_iranian_mobile

MAX_CONTACT_ROWS = 50_000
_PHONE_HEADERS = {"phone", "phone_number", "mobile", "شماره", "شماره تلفن", "تلفن"}


@dataclass(frozen=True, slots=True)
class ContactImportReport:
    matched: int
    unmatched: int
    invalid: int


def _phone_column(rows: list[list[str]]) -> tuple[int, int]:
    if not rows:
        return 0, 0
    header = [cell.strip().casefold() for cell in rows[0]]
    for index, cell in enumerate(header):
        if cell in _PHONE_HEADERS:
            return index, 1
    return 0, 0


class ContactImportService:
    """Match normalized numbers to subscribers who already opted in and shared a phone."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def match(self, content: str) -> ContactImportReport:
        try:
            rows = list(csv.reader(io.StringIO(content, newline="")))
        except csv.Error:
            return ContactImportReport(matched=0, unmatched=0, invalid=1)
        if len(rows) > MAX_CONTACT_ROWS + 1:
            raise ValueError(f"contact file exceeds the {MAX_CONTACT_ROWS} row limit")
        column, start = _phone_column(rows)
        numbers: set[str] = set()
        invalid = 0
        for row in rows[start:]:
            if column >= len(row) or not row[column].strip():
                invalid += 1
                continue
            normalized = normalize_iranian_mobile(row[column])
            if normalized is None:
                invalid += 1
            else:
                numbers.add(normalized)
        async with self._session_factory() as session:
            matched_numbers = await SubscriberRepository(session).find_linked_phones(numbers)
        return ContactImportReport(
            matched=len(matched_numbers),
            unmatched=len(numbers - matched_numbers),
            invalid=invalid,
        )
