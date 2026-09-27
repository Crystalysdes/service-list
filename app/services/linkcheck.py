"""Dead-link checker.

A pass checks every listed service (plus services hidden for a dead link, to bring them back). Safeguards:
reference links ("canaries") must come out right before "dead" verdicts of their group (t.me / sites) are
trusted, and a pass that suddenly finds many new dead links is not applied at all (breaker). A service is
hidden only after ``dead_streak`` dead verdicts in a row spanning at least ``dead_min_hours``; a paid service
first gets ``paid_grace_hours`` to change the link. Any alive verdict resets the streak; a hidden Telegram
link whose known title comes back within ``auto_restore_days`` returns by itself, a site that opens again is
shown to staff first (its domain may have a new owner). A changed page title (another owner took the
username) is reported to the admins instead of being trusted.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import socket
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from yarl import URL

from app.bot.i18n import Translator, h
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Category, LinkCheck, Order, Service, User
from app.domain.linkcheck import (
    ALIVE,
    DEAD,
    SKIP,
    UNKNOWN,
    Page,
    Verdict,
    fingerprint,
    http_verdict,
    post_verdict,
    tme_verdict,
)
from app.domain.links import LOCAL_SUFFIXES, Link, try_normalize
from app.services.audit import audit
from app.services.catalog import request_sync
from app.services.notify import claim_notification, notify_staff, notify_user
from app.services.settings import LinkCheckSettings, LinkCheckState, Runtime, get_settings, save_settings
from app.services.timefmt import fmt_date, fmt_dt

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)
NXDOMAIN_CODES = {
    code for code in (getattr(socket, "EAI_NONAME", None), getattr(socket, "EAI_NODATA", None)) if code
}


# ------------------------------------------------------------------------------------------ HTTP
def _nxdomain(exc: BaseException) -> bool:
    error = getattr(exc, "os_error", None) or exc.__cause__
    return isinstance(error, socket.gaierror) and error.errno in NXDOMAIN_CODES


REDIRECTS = (301, 302, 303, 307, 308)


def public_address(address: str) -> bool:
    """An address on the internet (not loopback, a private network, link-local like cloud metadata...)."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def unsafe_hop(url: URL) -> bool:
    """A redirect the checker does not follow: not http(s), an unusual port, or an address of a private
    network written as a number (those never reach the resolver below)."""
    if url.scheme not in ("http", "https") or url.explicit_port not in (None, 80, 443):
        return True
    host = (url.host or "").rstrip(".").lower()
    if not host or host == "localhost" or host.endswith(LOCAL_SUFFIXES):
        return True
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False  # a name: its addresses are checked when it is resolved
    return not public_address(host.strip("[]"))


class PublicResolver(aiohttp.ThreadedResolver):
    """Resolves names to public addresses only: a site (or its redirect) whose name points into the bot's
    own network (the database, 127.0.0.1, cloud metadata) is simply not reached."""

    async def resolve(  # type: ignore[override]
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[Any]:
        found = [item for item in await super().resolve(host, port, family) if public_address(item["host"])]
        if not found:
            raise OSError(f"{host}: no public address")
        return found


class HttpFetcher:
    """GET/HEAD with redirects, a timeout and a size cap; errors become ``Page.error``. Only public
    addresses are reached, on every redirect too (see :class:`PublicResolver` and :func:`unsafe_hop`)."""

    def __init__(self, timeout: float = 10.0, max_redirects: int = 5, max_bytes: int = 1 << 20) -> None:
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.max_bytes = max_bytes
        self._session: aiohttp.ClientSession | None = None

    def _client(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                # the threaded resolver reports NXDOMAIN as socket.gaierror, which "dead" relies on
                connector=aiohttp.TCPConnector(resolver=PublicResolver(), limit=4),
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9,ru;q=0.8"},
                trust_env=True,
            )
        return self._session

    async def fetch(self, url: str, method: str = "GET") -> Page:
        try:
            current = URL(url)
            for _hop in range(self.max_redirects + 1):  # redirects are followed here, each one checked
                if unsafe_hop(current):
                    return Page(None, error="blocked")
                async with self._client().request(method, current, allow_redirects=False) as response:
                    location = response.headers.get("Location")
                    if response.status in REDIRECTS and location:
                        current = response.url.join(URL(location))
                        continue
                    return await self._page(response, method)
            return Page(None, error="redirects")
        except aiohttp.ClientConnectorDNSError as exc:
            return Page(None, error="dns" if _nxdomain(exc) else "network")
        except (aiohttp.ClientSSLError, aiohttp.ClientConnectorCertificateError):
            return Page(None, error="tls")
        except TimeoutError:
            return Page(None, error="timeout")
        except (aiohttp.ClientError, OSError, ValueError) as exc:
            return Page(None, error="dns" if _nxdomain(exc) else "network")

    async def _page(self, response: aiohttp.ClientResponse, method: str) -> Page:
        chunks: list[bytes] = []
        size = 0
        if method == "GET":
            async for chunk in response.content.iter_chunked(65536):
                chunks.append(chunk)
                size += len(chunk)
                if size >= self.max_bytes:
                    break
        body = b"".join(chunks)[: self.max_bytes]
        try:
            text = body.decode(response.charset or "utf-8", "replace")
        except LookupError:
            text = body.decode("utf-8", "replace")
        return Page(response.status, text, final_url=str(response.url))

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


# ------------------------------------------------------------------------------------------ report
@dataclass
class PassReport:
    started_at: datetime
    mode: str = "auto"  # auto / manual / import
    finished_at: datetime | None = None
    checked: int = 0
    alive: int = 0
    dead: int = 0
    unknown: int = 0
    hidden: list[int] = field(default_factory=list)
    grace: list[int] = field(default_factory=list)
    restored: list[int] = field(default_factory=list)
    suspicious: list[int] = field(default_factory=list)
    trust: dict[str, bool] = field(default_factory=dict)
    breaker: bool = False
    verdicts: dict[int, Verdict] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("verdicts")
        data["started_at"] = self.started_at.isoformat()
        data["finished_at"] = self.finished_at.isoformat() if self.finished_at else None
        return data


def _group(url: str) -> str:
    link = try_normalize(url)
    return "tg" if link is not None and link.is_telegram else "ext"


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


async def is_paid(session: AsyncSession, service: Service) -> bool:
    if any(f.status == "active" for f in service.features):
        return True
    paid = await session.scalar(
        select(func.count())
        .select_from(Order)
        .where(Order.service_id == service.id, Order.status == "fulfilled", Order.amount_cents > 0)
    )
    return bool(paid)


def hide_for_dead_link(service: Service, settings: LinkCheckSettings, now: datetime) -> None:
    service.status = "hidden"
    service.hidden_reason = "dead_link"
    service.link_state = DEAD
    service.link_dead_streak = max(service.link_dead_streak or 0, settings.dead_streak)
    service.link_first_dead_at = service.link_first_dead_at or now
    service.extra = {**(service.extra or {}), "dead_hidden_at": now.isoformat()}


def restore_service(service: Service) -> bool:
    """Admin "return": back to the list, link state reset; a reviewed title change is accepted."""
    extra = dict(service.extra or {})
    if extra.get("fp_alert"):
        service.link_fingerprint = extra["fp_alert"]
        extra["link_title"] = extra.get("fp_title")
    for key in ("fp_alert", "fp_title", "dead_hidden_at", "back_alert"):
        extra.pop(key, None)
    service.extra = extra
    service.link_state = "unknown"
    service.link_dead_streak = 0
    service.link_first_dead_at = None
    service.link_grace_until = None
    if service.status == "hidden" and service.hidden_reason in ("dead_link", "review"):
        service.status = "active"
        service.hidden_reason = None
        return True
    return False


# ------------------------------------------------------------------------------------------ checker
class LinkChecker:
    def __init__(self, ctx: AppContext, fetcher: Any | None = None, *, delay: float | None = None) -> None:
        self.ctx = ctx
        self.fetcher = fetcher or HttpFetcher()
        self.delay = delay  # None: LinkCheckSettings.request_delay_sec
        self._lock = asyncio.Lock()
        self.last_report: PassReport | None = None

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def close(self) -> None:
        close = getattr(self.fetcher, "close", None)
        if close is not None:
            await close()

    # ---------------------------------------------------------------------------- single link
    async def check(self, url: str) -> Verdict:
        link = try_normalize(url)
        if link is None:
            return Verdict(UNKNOWN, "некорректная ссылка")
        try:
            if link.kind == "tg_username":
                return await self._username(link)
            if link.kind == "tg_invite":
                return tme_verdict(await self.fetcher.fetch(f"https://t.me/+{link.invite}"))
            if link.kind == "tg_post":
                return post_verdict(
                    await self.fetcher.fetch(f"https://t.me/{link.username}/{link.post_id}?embed=1&mode=tme")
                )
            if link.kind == "external":
                return await self._external(link.url)
        except Exception:  # a checker bug must never hide a service
            log.exception("link check failed for %s", url)
            return Verdict(UNKNOWN, "ошибка проверки")
        return Verdict(SKIP, "не проверяется")

    async def _username(self, link: Link) -> Verdict:
        bot = self.ctx.bot
        if bot is not None:
            for _attempt in range(2):
                try:
                    chat = await bot.get_chat(f"@{link.username}")
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(min(exc.retry_after, 30))
                    continue
                except TelegramAPIError:
                    break  # users and bots are not visible to getChat: ask the t.me page
                title = chat.title or " ".join(filter(None, [chat.first_name, chat.last_name])) or None
                return Verdict(ALIVE, "getChat", title=title, fingerprint=fingerprint(title))
        return tme_verdict(await self.fetcher.fetch(f"https://t.me/{link.username}"))

    async def _external(self, url: str) -> Verdict:
        head = await self.fetcher.fetch(url, "HEAD")
        if head.status is not None and 200 <= head.status < 400:
            return Verdict(ALIVE, f"HTTP {head.status}")
        if head.error == "dns":
            return http_verdict(head)
        # HEAD is often refused or answered wrongly: GET decides
        return http_verdict(await self.fetcher.fetch(url, "GET"))

    async def quick_verdict(self, url: str) -> str:
        """For moderation cards: one check with a short overall timeout."""
        try:
            verdict = await asyncio.wait_for(self.check(url), 15)
        except TimeoutError:
            return UNKNOWN
        return verdict.usable

    async def canaries(self) -> dict[str, bool]:
        async with self.ctx.db.session() as session:
            settings = await get_settings(session, LinkCheckSettings)
        trust = {"tg": True, "ext": True}
        seen: set[tuple[str, str]] = set()
        for expected, urls in ((ALIVE, settings.canary_alive), (DEAD, settings.canary_dead)):
            for url in urls:
                group = _group(url)
                seen.add((group, expected))
                if (await self.check(url)).state != expected:
                    trust[group] = False
        for group in trust:  # a group needs both an alive and a dead reference
            if (group, ALIVE) not in seen or (group, DEAD) not in seen:
                trust[group] = False
        return trust

    async def canary_check(self) -> Any:
        from app.services.selftest import Check

        trust = await self.canaries()
        status = {True: "ок", False: "не сошлись"}
        detail = f"t.me — {status[trust['tg']]}, сайты — {status[trust['ext']]}"
        return Check("Эталонные ссылки (проверка мёртвых ссылок)", trust["tg"], detail)

    async def due(self) -> bool:
        async with self.ctx.db.session() as session:
            settings = await get_settings(session, LinkCheckSettings)
            state = await get_settings(session, LinkCheckState)
            runtime = await get_settings(session, Runtime)
        if not settings.enabled or not runtime.live:
            return False
        if state.last_pass_at is None:
            return True
        return utcnow() - state.last_pass_at >= timedelta(hours=settings.pass_interval_hours)

    # ---------------------------------------------------------------------------- pass
    async def _targets(
        self, session: AsyncSession, settings: LinkCheckSettings, service_ids: list[int] | None
    ) -> list[tuple[int, str, str]]:
        query = select(Service).where(
            or_(
                Service.status == "active",
                (Service.status == "hidden") & (Service.hidden_reason == "dead_link"),
            )
        )
        if service_ids is not None:
            query = query.where(Service.id.in_(service_ids))
        now = utcnow()
        targets = []
        for service in (await session.execute(query.order_by(Service.id))).scalars():
            if service.status == "hidden":
                hidden_at = _parse_dt((service.extra or {}).get("dead_hidden_at"))
                if hidden_at is not None and now - hidden_at > timedelta(days=settings.auto_restore_days):
                    continue
            targets.append((service.id, service.url, service.link_state or "unknown"))
        return targets

    async def run_pass(
        self, *, mode: str = "auto", apply: bool = True, service_ids: list[int] | None = None
    ) -> PassReport:
        async with self._lock:
            report = PassReport(started_at=utcnow(), mode=mode)
            async with self.ctx.db.session() as session:
                settings = await get_settings(session, LinkCheckSettings)
                targets = await self._targets(session, settings, service_ids)
            delay = settings.request_delay_sec if self.delay is None else self.delay
            report.trust = await self.canaries()
            checked: list[tuple[int, str, str, Verdict]] = []
            for index, (service_id, url, previous) in enumerate(targets):
                if index and delay:
                    await asyncio.sleep(delay)
                verdict = await self.check(url)
                if verdict.state == DEAD and not report.trust.get(_group(url), False):
                    verdict = Verdict(UNKNOWN, "эталоны не сошлись", verdict.title, verdict.fingerprint)
                checked.append((service_id, url, previous, verdict))
            new_dead = [c for c in checked if c[3].state == DEAD and c[2] != DEAD]
            if mode != "import" and len(new_dead) > max(2, settings.breaker_ratio * len(checked)):
                report.breaker = True
                checked = [
                    (
                        sid,
                        url,
                        prev,
                        Verdict(UNKNOWN, "предохранитель") if v.state == DEAD and prev != DEAD else v,
                    )
                    for sid, url, prev, v in checked
                ]
            for service_id, _url, _previous, verdict in checked:
                report.verdicts[service_id] = verdict
                report.checked += 1
                if verdict.state == ALIVE:
                    report.alive += 1
                elif verdict.state == DEAD:
                    report.dead += 1
                else:
                    report.unknown += 1
            if apply:
                await self._apply(checked, settings, report)
            report.finished_at = utcnow()
            if mode != "import":
                async with self.ctx.db.session() as session:
                    await save_settings(
                        session, LinkCheckState(last_pass_at=report.started_at, last_report=report.to_json())
                    )
                    await session.commit()
            await self._alerts(report, len(new_dead))
            self.last_report = report
            return report

    async def _apply(
        self, checked: list[tuple[int, str, str, Verdict]], settings: LinkCheckSettings, report: PassReport
    ) -> None:
        now = utcnow()
        notices: list[tuple[str, int, dict[str, Any]]] = []
        async with self.ctx.db.session() as session:
            for service_id, url, _previous, verdict in checked:
                service = await session.get(Service, service_id)
                if service is None or service.url != url:  # edited while the pass was running
                    continue
                session.add(
                    LinkCheck(
                        service_id=service.id,
                        verdict=verdict.usable,
                        detail=verdict.detail[:500],
                        fingerprint=verdict.fingerprint,
                    )
                )
                service.link_checked_at = now
                if verdict.state == ALIVE:
                    await self._alive(session, service, verdict, settings, now, report, notices)
                elif verdict.state == DEAD:
                    await self._dead(session, service, verdict, settings, now, report, notices)
            await session.commit()
        await self._send(notices, settings)
        if report.hidden or report.restored:
            request_sync(self.ctx)

    async def _alive(
        self,
        session: AsyncSession,
        service: Service,
        verdict: Verdict,
        settings: LinkCheckSettings,
        now: datetime,
        report: PassReport,
        notices: list[tuple[str, int, dict[str, Any]]],
    ) -> None:
        service.link_state = ALIVE
        service.link_dead_streak = 0
        service.link_first_dead_at = None
        service.link_grace_until = None
        extra = dict(service.extra or {})
        known = service.link_fingerprint  # the title the link had before (a first one is adopted below)
        if verdict.fingerprint:
            if not service.link_fingerprint:
                service.link_fingerprint = verdict.fingerprint
                extra["link_title"] = verdict.title
            elif verdict.fingerprint != service.link_fingerprint:
                if extra.get("fp_alert") != verdict.fingerprint:
                    extra["fp_alert"] = verdict.fingerprint
                    extra["fp_title"] = verdict.title
                    report.suspicious.append(service.id)
                    notices.append(
                        (
                            "takeover",
                            service.id,
                            {
                                "old": extra.get("link_title") or service.link_fingerprint,
                                "new": verdict.title,
                            },
                        )
                    )
            else:  # the known title is back
                extra.pop("fp_alert", None)
                extra.pop("fp_title", None)
        if (
            service.status == "hidden"
            and service.hidden_reason == "dead_link"
            and not extra.get("fp_alert")  # never bring back a link that now leads to someone else
        ):
            hidden_at = _parse_dt(extra.get("dead_hidden_at"))
            if hidden_at is None or now - hidden_at <= timedelta(days=settings.auto_restore_days):
                # only a Telegram link whose known title is back returns by itself: a site that comes back
                # after being gone may be an expired domain someone else bought, so staff look first
                if known and verdict.fingerprint == known:
                    service.status = "active"
                    service.hidden_reason = None
                    extra.pop("dead_hidden_at", None)
                    report.restored.append(service.id)
                    notices.append(("restored", service.id, {}))
                    await audit(session, None, "service.restore_alive", "service", service.id)
                elif not extra.get("back_alert"):
                    extra["back_alert"] = now.isoformat()
                    notices.append(("back", service.id, {}))
        service.extra = extra

    async def _dead(
        self,
        session: AsyncSession,
        service: Service,
        verdict: Verdict,
        settings: LinkCheckSettings,
        now: datetime,
        report: PassReport,
        notices: list[tuple[str, int, dict[str, Any]]],
    ) -> None:
        service.link_state = DEAD
        service.link_dead_streak = (service.link_dead_streak or 0) + 1
        if service.link_first_dead_at is None:
            service.link_first_dead_at = now
        if service.status != "active":
            return
        # the owner learns at once, while the service is still listed, and can replace the link in time
        first_warning = service.link_dead_streak == 1 and service.owner_id is not None
        if service.link_dead_streak < settings.dead_streak or now - service.link_first_dead_at < timedelta(
            hours=settings.dead_min_hours
        ):
            if first_warning:
                notices.append(("dead", service.id, {"detail": verdict.detail}))
            return
        if await is_paid(session, service):
            if service.link_grace_until is None:
                service.link_grace_until = now + timedelta(hours=settings.paid_grace_hours)
                report.grace.append(service.id)
                notices.append(("grace", service.id, {"detail": verdict.detail}))
                return
            if now < service.link_grace_until:
                return
        hide_for_dead_link(service, settings, now)
        report.hidden.append(service.id)
        notices.append(("hidden", service.id, {"detail": verdict.detail}))
        await audit(session, None, "service.hide_dead", "service", service.id, {"detail": verdict.detail})

    # ---------------------------------------------------------------------------- messages
    async def _send(
        self, notices: list[tuple[str, int, dict[str, Any]]], settings: LinkCheckSettings
    ) -> None:
        tz = self.ctx.config.timezone
        for kind, service_id, info in notices:
            async with self.ctx.db.session() as session:
                service = await session.get(Service, service_id)
                if service is None:
                    continue
                category = await session.get(Category, service.category_id)
                owner = await session.get(User, service.owner_id) if service.owner_id else None
            name, where, url = h(service.name), h(category.title if category else "?"), h(service.url)
            staff = InlineKeyboardBuilder()
            has_buttons = kind in ("hidden", "takeover", "back")
            text = ""
            if kind == "hidden":
                text = (
                    f"🙈 Скрыт «{name}» ({where}): ссылка {url} не открывается — {h(info['detail'])}. "
                    f"Проверок подряд: {service.link_dead_streak}, "
                    f"с {fmt_date(service.link_first_dead_at, tz)}. "
                    f"Если ссылка оживёт в течение {settings.auto_restore_days} дн., сервис вернётся сам."
                )
                staff.button(text="♻️ Вернуть", callback_data=f"lnk:restore:{service.id}")
            elif kind == "grace":
                text = (
                    f"⏳ У платного сервиса «{name}» ({where}) не открывается ссылка {url}. "
                    f"Владельцу дано время до {fmt_dt(service.link_grace_until, tz)}, потом сервис скроется."
                )
            elif kind == "restored":
                text = f"♻️ Ссылка «{name}» ({where}) снова открывается — сервис вернулся в список."
            elif kind == "back":
                text = (
                    f"🔎 Ссылка «{name}» ({where}) {url} снова открывается, но сервис остаётся скрытым, пока "
                    "вы не проверите, что это тот же владелец: домен с истёкшим сроком мог купить кто-то "
                    "другой."
                )
                staff.button(text="♻️ Вернуть в список", callback_data=f"lnk:restore:{service.id}")
            elif kind == "takeover":
                text = (
                    f"⚠️ По ссылке «{name}» ({where}) {url} сменилось название: «{h(info['old'])}» → "
                    f"«{h(info['new'])}». Возможно, юзернейм занял кто-то другой — проверьте."
                )
                staff.button(text="✅ Всё в порядке", callback_data=f"lnk:fpok:{service.id}")
                staff.button(text="🙈 Скрыть", callback_data=f"lnk:fphide:{service.id}")
            if kind != "dead":  # the staff hear about a dead link when the service is hidden
                await notify_staff(self.ctx, text, reply_markup=staff.as_markup() if has_buttons else None)
            if owner is None or kind in ("takeover", "back"):
                continue
            t = Translator(owner.lang)
            builder = InlineKeyboardBuilder()
            builder.button(text=t("lnk.change"), callback_data=f"my:{service.id}:ef:url")
            builder.button(text=t("pay.manage"), callback_data=f"my:{service.id}")
            builder.adjust(1)
            if kind == "dead":
                await notify_user(
                    self.ctx, owner.id, t("lnk.dead", name=name, url=url), reply_markup=builder.as_markup()
                )
            elif kind == "hidden":
                await notify_user(
                    self.ctx, owner.id, t("lnk.hidden", name=name, url=url), reply_markup=builder.as_markup()
                )
            elif kind == "grace":
                await notify_user(
                    self.ctx,
                    owner.id,
                    t("lnk.grace", name=name, url=url, until=fmt_dt(service.link_grace_until, tz)),
                    reply_markup=builder.as_markup(),
                )
            elif kind == "restored":
                await notify_user(self.ctx, owner.id, t("lnk.restored", name=name))

    async def _alerts(self, report: PassReport, new_dead: int) -> None:
        failed = [name for group, name in (("tg", "t.me"), ("ext", "сайты")) if not report.trust.get(group)]
        if failed:
            async with self.ctx.db.session() as session:
                first = await claim_notification(
                    session, f"linkcanary:{utcnow():%Y-%m-%d}:{'+'.join(failed)}"
                )
                await session.commit()
            if first:
                await notify_staff(
                    self.ctx,
                    "⚠️ Проверка ссылок: эталонные ссылки не сошлись ("
                    + ", ".join(failed)
                    + "). Пока это так, «мёртвые» результаты для них не учитываются и сервисы не скрываются. "
                    "Проверьте сеть сервера и эталоны в настройках.",
                )
        if report.breaker:
            await notify_staff(
                self.ctx,
                "🛑 Проверка ссылок: новых «мёртвых» ссылок подозрительно много — "
                f"{new_dead} из {report.checked}. "
                "Результаты не применены (предохранитель). Посмотрите вручную: /admin → 🔗 Ссылки.",
            )


HISTORY_DAYS = 60  # link check results kept (one row per service per pass: the table and backups grow)


async def prune_history(ctx: AppContext, now: datetime | None = None) -> int:
    cutoff = (now or utcnow()) - timedelta(days=HISTORY_DAYS)
    async with ctx.db.session() as session:
        result = await session.execute(delete(LinkCheck).where(LinkCheck.checked_at < cutoff))
        await session.commit()
    return int(result.rowcount or 0)


async def job_pass(ctx: AppContext) -> PassReport | None:
    checker: LinkChecker | None = ctx.get("linkcheck")
    if checker is None or checker.running or not await checker.due():
        return None
    await prune_history(ctx)
    return await checker.run_pass(mode="auto")


def start_background_pass(ctx: AppContext, mode: str, done: Any) -> bool:
    """Start a pass as a task (admin buttons); ``done(report)`` is awaited when it finishes."""
    checker: LinkChecker | None = ctx.get("linkcheck")
    if checker is None or checker.running:
        return False

    async def runner() -> None:
        try:
            report = await checker.run_pass(mode=mode, apply=mode != "import")
        except Exception:
            log.exception("link check pass failed")
            report = None
        with contextlib.suppress(Exception):
            await done(report)

    task = asyncio.create_task(runner())
    tasks: set[asyncio.Task[None]] = ctx.services.setdefault("linkcheck_tasks", set())
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return True


def report_lines(data: dict[str, Any], tz: str) -> list[str]:
    """Human-readable summary of a stored pass report (``PassReport.to_json()``)."""
    if not data:
        return ["Проверок ещё не было."]
    modes = {"auto": "автоматическая", "manual": "вручную", "import": "импорт"}
    started = _parse_dt(data.get("started_at"))
    lines = [
        f"Последняя проверка: {fmt_dt(started, tz)} ({modes.get(data.get('mode', ''), data.get('mode'))})",
        f"Проверено: {data.get('checked', 0)} — живых {data.get('alive', 0)}, не открываются "
        f"{data.get('dead', 0)}, не удалось проверить {data.get('unknown', 0)}",
    ]
    extra = [
        (len(data.get("hidden") or []), "скрыто"),
        (len(data.get("restored") or []), "вернулось"),
        (len(data.get("grace") or []), "дано время на замену"),
        (len(data.get("suspicious") or []), "сменилось название"),
    ]
    changes = ", ".join(f"{title} {count}" for count, title in extra if count)
    if changes:
        lines.append("Изменения: " + changes)
    trust = data.get("trust") or {}
    lines.append(
        f"Эталоны: t.me — {'ок' if trust.get('tg') else 'не сошлись'}, "
        f"сайты — {'ок' if trust.get('ext') else 'не сошлись'}"
    )
    if data.get("breaker"):
        lines.append("🛑 Сработал предохранитель — результаты не применены.")
    return lines
