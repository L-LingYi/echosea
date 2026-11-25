"""QQ Identity Strong Binding Service: QQ number as primary key with dynamic group card & known_names history."""
from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import QQIdentity


class IdentityService:
    """Manages canonical user profiles keyed strictly by QQ number."""

    async def upsert_from_message(
        self,
        session: AsyncSession,
        qq_id: int,
        nickname: str = "",
        group_id: int | None = None,
        group_card: str = "",
    ) -> QQIdentity:
        identity = await session.get(QQIdentity, qq_id)
        now = datetime.now(timezone.utc)

        if identity is None:
            known: list[str] = []
            for candidate in (nickname.strip(), group_card.strip()):
                if candidate and candidate not in known:
                    known.append(candidate)
            cards = {str(group_id): group_card.strip()} if (group_id and group_card.strip()) else {}
            identity = QQIdentity(
                qq_id=qq_id,
                nickname=nickname.strip(),
                group_cards=cards,
                known_names=known,
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(identity)
            await session.flush()
            return identity

        # Update dynamic nickname, group card, and preserve historical known_names
        known = list(identity.known_names or [])
        cards = dict(identity.group_cards or {})

        if nickname.strip():
            identity.nickname = nickname.strip()
            if nickname.strip() not in known:
                known.append(nickname.strip())

        if group_id is not None and group_card.strip():
            cards[str(group_id)] = group_card.strip()
            if group_card.strip() not in known:
                known.append(group_card.strip())

        identity.known_names = known[-30:]  # Keep latest 30 distinct historical names
        identity.group_cards = cards
        identity.last_seen_at = now
        await session.flush()
        return identity

    async def resolve_display_name(self, session: AsyncSession, qq_id: int, group_id: int | None = None) -> str:
        identity = await session.get(QQIdentity, qq_id)
        if not identity:
            return str(qq_id)
        if group_id is not None:
            card = (identity.group_cards or {}).get(str(group_id))
            if card:
                return card
        return identity.nickname or str(qq_id)

    async def list_identities(self, session: AsyncSession, limit: int = 100) -> list[QQIdentity]:
        stmt = select(QQIdentity).order_by(QQIdentity.last_seen_at.desc()).limit(limit)
        res = await session.execute(stmt)
        return list(res.scalars().all())
