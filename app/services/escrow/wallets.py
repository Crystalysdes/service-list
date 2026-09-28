"""Where the garant sends a side's money: an address of the deal's coin (USDT BEP20: EIP-55; BTC and LTC:
their checksums, app/services/coinaddr.py), checked and remembered for the next deal in that coin. The bot's
own deposit addresses (the invoices of deals and of orders) are never accepted: money sent there would come
back to the account.

The coin is always named: an address of one coin is never checked, kept or offered as another's."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import DealInvoice, EscrowWallet, Invoice
from app.services import coinaddr
from app.services.escrow.money import Coin


async def remembered(session: AsyncSession, user_id: int, *, coin: Coin) -> str | None:
    row = await session.get(EscrowWallet, (user_id, coin.code), populate_existing=True)
    return row.address if row is not None else None


async def remember(session: AsyncSession, user_id: int, address: str, *, coin: Coin) -> None:
    now = utcnow()
    stmt = insert(EscrowWallet).values(user_id=user_id, currency=coin.code, address=address, updated_at=now)
    stmt = stmt.on_conflict_do_update(
        index_elements=[EscrowWallet.user_id, EscrowWallet.currency],
        set_={"address": address, "updated_at": now},
    )
    await session.execute(stmt)


async def own_addresses(session: AsyncSession) -> set[str]:
    """The deposit addresses of the bot's invoices, of deals and of orders (keys)."""
    deals = await session.execute(select(DealInvoice.address).where(DealInvoice.address.is_not(None)))
    orders = await session.execute(select(Invoice.address).where(Invoice.address.is_not(None)))
    return {coinaddr.key(address) for address in [*deals.scalars(), *orders.scalars()] if address}


async def check(session: AsyncSession, text: str, *, coin: Coin) -> str:
    """A typed address of the coin → its stored form (USDT: EIP-55), or ``evm.AddressError`` (format /
    checksum / forbidden / testnet / other_coin / ltc_p2sh / unsupported)."""
    return coinaddr.normalize(coin.code, text, forbidden=await own_addresses(session))


def tx_url(txid: str, *, coin: Coin) -> str:
    return coin.tx_url(txid)


def address_url(address: str, *, coin: Coin) -> str:
    return coin.address_url(address)
