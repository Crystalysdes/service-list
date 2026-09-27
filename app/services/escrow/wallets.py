"""Where the garant sends a side's money: a USDT BEP20 address, checked (EIP-55) and remembered for the next
deal. The bot's own deposit addresses are never accepted: money sent there would come back to the deal."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import DealInvoice, EscrowWallet
from app.services import evm
from app.services.escrow import money


async def remembered(session: AsyncSession, user_id: int) -> str | None:
    row = await session.get(EscrowWallet, (user_id, money.CURRENCY), populate_existing=True)
    return row.address if row is not None else None


async def remember(session: AsyncSession, user_id: int, address: str) -> None:
    now = utcnow()
    stmt = insert(EscrowWallet).values(
        user_id=user_id, currency=money.CURRENCY, address=address, updated_at=now
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[EscrowWallet.user_id, EscrowWallet.currency],
        set_={"address": address, "updated_at": now},
    )
    await session.execute(stmt)


async def own_addresses(session: AsyncSession) -> set[str]:
    """The deposit addresses of the bot's invoices (lower-case)."""
    rows = await session.execute(select(DealInvoice.address).where(DealInvoice.address.is_not(None)))
    return {address for address in rows.scalars() if address}


async def check(session: AsyncSession, text: str) -> str:
    """A typed address → its EIP-55 form, or ``evm.AddressError`` (format / checksum / forbidden)."""
    return evm.normalize(text, forbidden=await own_addresses(session))


def tx_url(txid: str) -> str:
    return f"https://bscscan.com/tx/{txid}"


def address_url(address: str) -> str:
    return f"https://bscscan.com/token/{evm.USDT_BEP20_CONTRACT}?a={address}"
