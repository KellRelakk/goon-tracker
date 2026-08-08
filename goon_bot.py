"""
Discord-бот для отслеживания "Гунов" (Goon Squad) в Escape from Tarkov.

Парсит два сайта:
1. https://www.tarkov-goon-tracker.com/pve
2. https://www.goon-tracker.com/pvetracker

Оба сайта помечают время своих записей таймзоной (первый — "z"/UTC,
второй — "PST"), поэтому бот конвертирует их во время МСК (UTC+3).

Команда в Discord: !гуны

Установка зависимостей:
    pip install -r requirements.txt

Запуск:
    export DISCORD_TOKEN="твой_токен_бота"   (Windows PowerShell: $env:DISCORD_TOKEN="...")
    python goon_bot.py
"""

import os
import re
import logging
import asyncio
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from bs4 import BeautifulSoup
from discord.ext import commands

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("goon-bot")

HEADERS = {
    # Некоторые сайты блокируют запросы без нормального User-Agent
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

URLS = {
    "tarkov_goon_tracker": "https://www.tarkov-goon-tracker.com/pve",
    "goon_tracker": "https://www.goon-tracker.com/pvetracker",
}

MSK = timezone(timedelta(hours=3), name="MSK")
UTC = timezone.utc
PST = timezone(timedelta(hours=-8), name="PST")  # сайт goon-tracker.com жёстко подписывает время как PST

MAP_NAMES_RU = {
    "customs": "Таможня",
    "shoreline": "Берег",
    "woods": "Лес",
    "lighthouse": "Маяк",
}


def translate_map(map_name: str | None) -> str | None:
    """Переводит название карты на русский, если оно известно; иначе возвращает как есть."""
    if not map_name:
        return map_name
    return MAP_NAMES_RU.get(map_name.strip().lower(), map_name)


def to_msk_str(dt: datetime) -> str:
    """Форматирует datetime (с уже выставленным tzinfo) как строку в МСК."""
    return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК")


def humanize_ago(dt: datetime) -> str:
    """Считает, сколько времени прошло с момента dt, и возвращает строку вида
    "5 мин. назад" / "2 ч. назад" / "1 дн. назад"."""
    delta = datetime.now(UTC) - dt.astimezone(UTC)
    seconds = max(int(delta.total_seconds()), 0)
    minutes = seconds // 60
    hours = minutes // 60
    days = hours // 24

    if days > 0:
        return f"{days} дн. назад"
    if hours > 0:
        return f"{hours} ч. назад"
    if minutes > 0:
        return f"{minutes} мин. назад"
    return "только что"


# ---------------------------------------------------------------------------
# Загрузка страниц
# ---------------------------------------------------------------------------

async def fetch_html(session: aiohttp.ClientSession, url: str) -> str | None:
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                log.warning("Сайт %s ответил статусом %s", url, resp.status)
                return None
            return await resp.text()
    except Exception as e:
        log.warning("Не удалось загрузить %s: %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Парсеры под каждый сайт
# ---------------------------------------------------------------------------

def parse_tarkov_goon_tracker(html: str) -> dict:
    """
    https://www.tarkov-goon-tracker.com/pve

    Берём первую строку таблицы "Recent Goon Trackings":
    Map | Time | Tracker | Logged In
    Время в столбце "Time" отмечено суффиксом "z" (Zulu / UTC),
    например: "Aug 07, 2026 10:49 AM z".
    """
    soup = BeautifulSoup(html, "html.parser")

    map_name = None
    time_msk = None
    reporter = None
    relative = None

    table = soup.find("table")
    if table:
        rows = table.find_all("tr")
        if len(rows) > 1:
            cells = [c.get_text(strip=True) for c in rows[1].find_all(["td", "th"])]
            if len(cells) >= 1:
                map_name = cells[0]
            if len(cells) >= 2:
                raw_time = cells[1]
                m = re.match(r"([A-Za-z]{3} \d{1,2}, \d{4} \d{1,2}:\d{2} [AP]M)\s*z?", raw_time, re.IGNORECASE)
                if m:
                    try:
                        dt = datetime.strptime(m.group(1), "%b %d, %Y %I:%M %p").replace(tzinfo=UTC)
                        time_msk = to_msk_str(dt)
                        relative = humanize_ago(dt)
                    except ValueError:
                        pass
            if len(cells) >= 3:
                reporter = cells[2]

    # Фолбэк на заголовочную фразу, если таблицу не удалось распарсить
    if not map_name:
        text = soup.get_text(" ", strip=True)
        m = re.search(r"last seen on:?\s*([A-Za-z]+)", text, re.IGNORECASE)
        if m:
            map_name = m.group(1).strip()

    extra_parts = []
    if reporter:
        extra_parts.append(f"Репортер: {reporter}")
    if relative:
        extra_parts.append(relative)

    return {
        "source": "Tarkov Goon Tracker (PvE)",
        "map": translate_map(map_name),
        "time_msk": time_msk,
        "extra": "\n".join(extra_parts) if extra_parts else None,
        "url": URLS["tarkov_goon_tracker"],
    }


def parse_goon_tracker(html: str) -> dict:
    """
    https://www.goon-tracker.com/pvetracker

    Блок:
        Last Seen on PvE Mode:
        <Карта>
        Time: 2026-08-07 07:51:00 PST
        Last seen: 5 minutes ago
    """
    soup = BeautifulSoup(html, "html.parser")
    # Флэттеним весь текст страницы через пробел. Это надёжнее, чем резать по
    # номерам строк: если "Time:" и сама дата лежат в разных HTML-тегах, номера
    # строк "плывут", а склеенный через пробел текст сохраняет порядок и рядом
    # стоящие подписи/значения всё равно оказываются друг рядом с другом.
    text = soup.get_text(" ", strip=True)

    map_name = None

    map_match = re.search(r"Last Seen on PvE Mode:\s*([A-Za-z]+)", text, re.IGNORECASE)
    if map_match:
        map_name = map_match.group(1)

    time_msk = None
    dt = None
    time_match = re.search(
        r"Time:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*PST",
        text,
        re.IGNORECASE,
    )
    if time_match:
        try:
            dt = datetime.strptime(time_match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=PST)
            time_msk = to_msk_str(dt)
        except ValueError:
            dt = None

    relative = humanize_ago(dt) if dt else None

    return {
        "source": "Goon-Tracker.com (PvE)",
        "map": translate_map(map_name),
        "time_msk": time_msk,
        "extra": relative,
        "url": URLS["goon_tracker"],
    }


# ---------------------------------------------------------------------------
# Discord-бот
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True  # нужно для чтения команд с префиксом "!"

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


@bot.event
async def on_ready():
    log.info("Бот запущен как %s", bot.user)


def fmt_field(map_name, time_msk, extra=None):
    if not map_name:
        return "⚠️ Не удалось распарсить данные (возможно, изменилась разметка сайта)"
    value = f"Карта: **{map_name}**"
    if time_msk:
        value += f"\nПоследний раз видели: **{time_msk}**"
    else:
        value += "\nВремя: не удалось определить"
    if extra:
        value += f"\n{extra}"
    return value


@bot.command(name="гуны")
@commands.cooldown(1, 15, commands.BucketType.guild)  # не чаще раза в 15 сек на сервер, чтобы не спамить сайты
async def goons(ctx: commands.Context):
    """Показывает последние замеченные локации Гунов (PvE) с двух трекеров, время в МСК."""
    async with ctx.typing():
        async with aiohttp.ClientSession() as session:
            html_tgt, html_gt = await asyncio.gather(
                fetch_html(session, URLS["tarkov_goon_tracker"]),
                fetch_html(session, URLS["goon_tracker"]),
            )

        embed = discord.Embed(
            title="🎯 Где сейчас Гуны (Goon Squad) — Escape from Tarkov [PvE]",
            color=discord.Color.dark_red(),
        )

        # 1. tarkov-goon-tracker.com
        if html_tgt:
            d = parse_tarkov_goon_tracker(html_tgt)
            embed.add_field(
                name=f"📍 {d['source']}",
                value=fmt_field(d["map"], d["time_msk"], d["extra"]),
                inline=False,
            )
        else:
            embed.add_field(name="📍 Tarkov Goon Tracker (PvE)", value="⚠️ Сайт недоступен", inline=False)

        # 2. goon-tracker.com
        if html_gt:
            d = parse_goon_tracker(html_gt)
            embed.add_field(
                name=f"📍 {d['source']}",
                value=fmt_field(d["map"], d["time_msk"], d["extra"]),
                inline=False,
            )
        else:
            embed.add_field(name="📍 Goon-Tracker.com (PvE)", value="⚠️ Сайт недоступен", inline=False)

        embed.set_footer(text="Данные собраны с публичных коммьюнити-трекеров, могут не совпадать между источниками")

        await ctx.send(embed=embed)


@goons.error
async def goons_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ Подожди ещё {error.retry_after:.0f} сек. перед повторным запросом.")
    else:
        log.exception("Ошибка в команде !гуны: %s", error)
        await ctx.send("❌ Что-то пошло не так при получении данных. Попробуй позже.")


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Не найден DISCORD_TOKEN. Установи переменную окружения перед запуском.")
    bot.run(token)
