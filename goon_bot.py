"""
Discord-бот для отслеживания "Гунов" (Goon Squad) в Escape from Tarkov.

Парсит три сайта:
1. https://www.tarkov-goon-tracker.com/pve
2. https://www.goon-tracker.com/pvetracker
3. https://eft.su/goons

Команда в Discord: !гуны

Установка зависимостей:
    pip install -r requirements.txt

Запуск:
    export DISCORD_TOKEN="твой_токен_бота"   (Windows: set DISCORD_TOKEN=...)
    python goon_bot.py
"""

import os
import re
import logging
import asyncio

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
    "eft_su": "https://eft.su/goons",
}


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
    Ищем фразу "The Goons were last seen on: <Карта>",
    и первую строку таблицы "Recent Goon Trackings" как доп. подтверждение.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    map_name = None
    m = re.search(r"last seen on:?\s*([A-Za-z]+)", text, re.IGNORECASE)
    if m:
        map_name = m.group(1).strip()

    time_str = None
    reporter = None
    table = soup.find("table")
    if table:
        rows = table.find_all("tr")
        # первая строка обычно заголовок, вторая - первая запись
        if len(rows) > 1:
            cells = rows[1].find_all(["td", "th"])
            cell_texts = [c.get_text(strip=True) for c in cells]
            if len(cell_texts) >= 1 and not map_name:
                map_name = cell_texts[0]
            if len(cell_texts) >= 2:
                time_str = cell_texts[1]
            if len(cell_texts) >= 3:
                reporter = cell_texts[2]

    return {
        "source": "Tarkov Goon Tracker (PvE)",
        "map": map_name,
        "time": time_str,
        "extra": f"Репортер: {reporter}" if reporter else None,
        "url": URLS["tarkov_goon_tracker"],
    }


def parse_goon_tracker(html: str) -> dict:
    """
    https://www.goon-tracker.com/pvetracker
    Блок:
        Last Seen on PvE Mode:
        <Карта>
        Time: <дата/время>
        Last seen: <относительное время>
    """
    soup = BeautifulSoup(html, "html.parser")
    lines = [l.strip() for l in soup.get_text("\n", strip=True).split("\n") if l.strip()]

    map_name = None
    time_str = None
    relative = None

    for i, line in enumerate(lines):
        if "Last Seen on PvE Mode" in line:
            if i + 1 < len(lines):
                map_name = lines[i + 1]
            if i + 2 < len(lines) and lines[i + 2].lower().startswith("time:"):
                time_str = lines[i + 2].split(":", 1)[1].strip()
            if i + 3 < len(lines) and lines[i + 3].lower().startswith("last seen:"):
                relative = lines[i + 3].split(":", 1)[1].strip()
            break

    return {
        "source": "Goon-Tracker.com (PvE)",
        "map": map_name,
        "time": time_str,
        "extra": f"Когда: {relative}" if relative else None,
        "url": URLS["goon_tracker"],
    }


def parse_eft_su(html: str) -> dict:
    """
    https://eft.su/goons
    Сайт показывает отдельно последнее появление в PVP и в PVE.
    Нам нужен только PVE-блок. Ссылки на карту вида <a href="/m/lighthouse">...</a>.
    """
    soup = BeautifulSoup(html, "html.parser")

    map_name = None
    time_str = None

    for a in soup.find_all("a", href=re.compile(r"^/m/")):
        text = a.get_text(" ", strip=True)
        if "PVE" not in text.upper():
            continue

        slug = a["href"].rsplit("/m/", 1)[-1]
        map_name = slug.replace("-", " ").title()

        date_match = re.search(r"(\d{1,2}\s+\S+\.?,?\s+\d{2}:\d{2})", text)
        time_str = date_match.group(1) if date_match else None
        break  # берём первое совпадение (PVE-блок идёт один раз на странице)

    return {
        "source": "EFT.SU (PvE)",
        "map": map_name,
        "time": time_str,
        "extra": None,
        "url": URLS["eft_su"],
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


def fmt_field(map_name, time_str, extra=None):
    if not map_name:
        return "⚠️ Не удалось распарсить данные (возможно, изменилась разметка сайта)"
    value = f"Карта: **{map_name}**"
    if time_str:
        value += f"\nВремя: {time_str}"
    if extra:
        value += f"\n{extra}"
    return value


@bot.command(name="гуны")
@commands.cooldown(1, 15, commands.BucketType.guild)  # не чаще раза в 15 сек на сервер, чтобы не спамить сайты
async def goons(ctx: commands.Context):
    """Показывает последние замеченные локации Гунов с трёх трекеров."""
    async with ctx.typing():
        async with aiohttp.ClientSession() as session:
            html_tgt, html_gt, html_eft = await asyncio.gather(
                fetch_html(session, URLS["tarkov_goon_tracker"]),
                fetch_html(session, URLS["goon_tracker"]),
                fetch_html(session, URLS["eft_su"]),
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
                value=fmt_field(d["map"], d["time"], d["extra"]),
                inline=False,
            )
        else:
            embed.add_field(name="📍 Tarkov Goon Tracker (PvE)", value="⚠️ Сайт недоступен", inline=False)

        # 2. goon-tracker.com
        if html_gt:
            d = parse_goon_tracker(html_gt)
            embed.add_field(
                name=f"📍 {d['source']}",
                value=fmt_field(d["map"], d["time"], d["extra"]),
                inline=False,
            )
        else:
            embed.add_field(name="📍 Goon-Tracker.com (PvE)", value="⚠️ Сайт недоступен", inline=False)

        # 3. eft.su
        if html_eft:
            d = parse_eft_su(html_eft)
            embed.add_field(
                name=f"📍 {d['source']}",
                value=fmt_field(d["map"], d["time"], d["extra"]),
                inline=False,
            )
        else:
            embed.add_field(name="📍 EFT.SU (PvE)", value="⚠️ Сайт недоступен", inline=False)

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
