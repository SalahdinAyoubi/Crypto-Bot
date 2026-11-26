import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone

import aiohttp
import discord
from discord.ext import commands, tasks

TOKEN = os.getenv("DISCORD_TOKEN")
DATA_DIR = os.getenv("DATA_DIR", ".")
DATA_FILE = os.path.join(DATA_DIR, "data.json")

API = "https://api.coingecko.com/api/v3"
GREEN, RED, BLUE, GOLD = 0x2ECC71, 0xE74C3C, 0x3498DB, 0xF1C40F

MIN_INTERVAL = 5 * 60          # أقل مدة للتنبيه: 5 دقائق
MAX_INTERVAL = 7 * 24 * 3600   # أطول مدة: 7 أيام
MAX_PER_USER = 5               # أقصى عدد تنبيهات للمستخدم
MAX_PER_GUILD = 20             # أقصى عدد تنبيهات للخادم
QUICK_COOLDOWN = 5             # ثواني بين طلبات الاسم المباشر لنفس المستخدم


# ------------------------- التخزين -------------------------
def load_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("alerts", [])
    data.setdefault("next_id", 1)
    data.setdefault("quick_off", [])
    return data


def save_data():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(DATA, f, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    except OSError as e:
        print("save error:", e)


DATA = load_data()
COINS = {}      # اسم/رمز/معرّف العملة (بحروف صغيرة) -> معرّف CoinGecko
COOLDOWN = {}


# ------------------------- أدوات مساعدة -------------------------
def normalize(text):
    return re.sub(r"\s+", " ", text.strip().lower())


def fmt_price(v):
    if v is None:
        return "—"
    return f"${v:,.2f}" if v >= 1 else f"${v:,.6f}"


def fmt_big(v):
    if v is None:
        return "—"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if v >= div:
            return f"${v / div:,.2f}{unit}"
    return f"${v:,.0f}"


INTERVAL_RE = re.compile(r"(\d+)([mhd])")
UNITS = {"m": 60, "h": 3600, "d": 86400}


def parse_interval(text):
    text = text.strip().lower().replace(" ", "")
    parts = INTERVAL_RE.findall(text)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != text:
        return None
    return sum(int(n) * UNITS[u] for n, u in parts)


def human_interval(sec):
    d, rem = divmod(int(sec), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = []
    if d:
        parts.append(f"{d} يوم")
    if h:
        parts.append(f"{h} ساعة")
    if m:
        parts.append(f"{m} دقيقة")
    return " و".join(parts)


def error_embed(text):
    return discord.Embed(description=f"❌ {text}", color=RED)


def ok_embed(text):
    return discord.Embed(description=f"✅ {text}", color=GREEN)


# ------------------------- CoinGecko -------------------------
async def fetch_json(path, params=None):
    try:
        async with bot.session.get(f"{API}{path}", params=params) as r:
            if r.status != 200:
                return None
            return await r.json()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


async def fetch_markets(ids):
    """يجلب بيانات عدة عملات بطلبات مجمّعة."""
    ids = list(ids)
    result = []
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        data = await fetch_json(
            "/coins/markets",
            {"vs_currency": "usd", "ids": ",".join(chunk), "per_page": 100},
        )
        if data is None:
            return None
        result.extend(data)
    return result


async def refresh_coins():
    data = await fetch_json(
        "/coins/markets",
        {"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": 1},
    )
    if not data:
        return
    index = {}
    for c in data:
        for key in (c["id"], c["name"], c["symbol"]):
            index.setdefault(normalize(key), c["id"])
    COINS.clear()
    COINS.update(index)


async def resolve_coin(query):
    key = normalize(query)
    if key in COINS:
        return COINS[key]
    data = await fetch_json("/search", {"query": query})
    coins = (data or {}).get("coins") or []
    if not coins:
        return None
    for c in coins:
        if key in (c["symbol"].lower(), c["name"].lower(), c["id"]):
            return c["id"]
    return coins[0]["id"]


# ------------------------- البطاقات -------------------------
def coin_embed(c, footer="البيانات من CoinGecko"):
    change = c.get("price_change_percentage_24h") or 0
    up = change >= 0
    embed = discord.Embed(
        title=f"{c['name']} ({c['symbol'].upper()})",
        description=f"**{fmt_price(c['current_price'])}**",
        color=GREEN if up else RED,
        timestamp=datetime.now(timezone.utc),
    )
    if c.get("image"):
        embed.set_thumbnail(url=c["image"])
    embed.add_field(
        name="التغير (24س)",
        value=f"{'🟢 ▲' if up else '🔴 ▼'} {change:+.2f}%",
        inline=True,
    )
    embed.add_field(name="الترتيب", value=f"#{c.get('market_cap_rank') or '—'}", inline=True)
    embed.add_field(name="القيمة السوقية", value=fmt_big(c.get("market_cap")), inline=True)
    embed.add_field(name="أعلى سعر (24س)", value=fmt_price(c.get("high_24h")), inline=True)
    embed.add_field(name="أدنى سعر (24س)", value=fmt_price(c.get("low_24h")), inline=True)
    embed.add_field(name="حجم التداول", value=fmt_big(c.get("total_volume")), inline=True)
    embed.set_footer(text=footer)
    return embed


# ------------------------- البوت -------------------------
class CryptoBot(commands.Bot):
    async def setup_hook(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        refresh_loop.start()
        alert_loop.start()

    async def close(self):
        await self.session.close()
        await super().close()


intents = discord.Intents.default()
intents.message_content = True
bot = CryptoBot(command_prefix="!", intents=intents, help_command=None)


@bot.event
async def on_ready():
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="!help | btc")
    )
    print(f"Logged in as {bot.user}")


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.content.startswith("!"):
        await bot.process_commands(message)
        return
    await quick_price(message)


async def quick_price(message):
    """الرد بالسعر عند كتابة اسم العملة أو رمزها فقط."""
    if message.guild and message.guild.id in DATA["quick_off"]:
        return
    text = normalize(message.content)
    if not text or len(text) > 30:
        return
    coin_id = COINS.get(text)
    if not coin_id:
        return
    now = time.time()
    if now - COOLDOWN.get(message.author.id, 0) < QUICK_COOLDOWN:
        return
    COOLDOWN[message.author.id] = now
    data = await fetch_markets([coin_id])
    if not data:
        return
    try:
        await message.reply(embed=coin_embed(data[0]), mention_author=False)
    except discord.HTTPException:
        pass


# ------------------------- الأوامر -------------------------
@bot.command(name="price", aliases=["p"], usage="<العملة>")
async def price(ctx, *, coin: str = "bitcoin"):
    async with ctx.typing():
        coin_id = await resolve_coin(coin)
        data = await fetch_markets([coin_id]) if coin_id else None
    if not data:
        await ctx.send(embed=error_embed("لم أجد هذه العملة، أو الخدمة مشغولة. جرّب الاسم الكامل أو الرمز."))
        return
    await ctx.send(embed=coin_embed(data[0]))


@bot.command(name="top", usage="[العدد]")
async def top(ctx, n: int = 10):
    n = max(1, min(n, 15))
    async with ctx.typing():
        data = await fetch_json(
            "/coins/markets",
            {"vs_currency": "usd", "order": "market_cap_desc", "per_page": n, "page": 1},
        )
    if not data:
        await ctx.send(embed=error_embed("تعذّر جلب البيانات حاليًا، حاول بعد قليل."))
        return
    lines = []
    for i, c in enumerate(data, 1):
        ch = c.get("price_change_percentage_24h") or 0
        dot = "🟢" if ch >= 0 else "🔴"
        lines.append(
            f"`{i:>2}.` **{c['symbol'].upper()}** — {fmt_price(c['current_price'])}  {dot} {ch:+.2f}%"
        )
    embed = discord.Embed(
        title=f"🏆 أفضل {n} عملات رقمية",
        description="\n".join(lines),
        color=BLUE,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="البيانات من CoinGecko")
    await ctx.send(embed=embed)


@bot.command(name="alert", aliases=["setalert"], usage="<العملة> <المدة> [#القناة]")
@commands.guild_only()
async def alert(ctx, coin: str, interval: str, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    seconds = parse_interval(interval)
    if seconds is None:
        await ctx.send(embed=error_embed("صيغة المدة غير صحيحة. أمثلة: `30m` `2h` `1d` `1h30m`"))
        return
    if seconds < MIN_INTERVAL or seconds > MAX_INTERVAL:
        await ctx.send(embed=error_embed("المدة يجب أن تكون بين **5 دقائق** و**7 أيام**."))
        return

    me_perms = channel.permissions_for(ctx.guild.me)
    if not (me_perms.view_channel and me_perms.send_messages and me_perms.embed_links):
        await ctx.send(embed=error_embed(f"لا أملك صلاحية الإرسال وتضمين الروابط في {channel.mention}."))
        return
    if not channel.permissions_for(ctx.author).send_messages:
        await ctx.send(embed=error_embed("لا تملك صلاحية الكتابة في هذه القناة."))
        return

    mine = sum(1 for a in DATA["alerts"] if a["user_id"] == ctx.author.id)
    in_guild = sum(1 for a in DATA["alerts"] if a["guild_id"] == ctx.guild.id)
    if mine >= MAX_PER_USER:
        await ctx.send(embed=error_embed(f"وصلت إلى الحد الأقصى ({MAX_PER_USER} تنبيهات). احذف أحدها بـ `!stopalert`."))
        return
    if in_guild >= MAX_PER_GUILD:
        await ctx.send(embed=error_embed(f"وصل الخادم إلى الحد الأقصى ({MAX_PER_GUILD} تنبيهًا)."))
        return

    async with ctx.typing():
        coin_id = await resolve_coin(coin)
        data = await fetch_markets([coin_id]) if coin_id else None
    if not data:
        await ctx.send(embed=error_embed("لم أجد هذه العملة، أو الخدمة مشغولة. جرّب الاسم الكامل أو الرمز."))
        return

    c = data[0]
    next_run = time.time() + seconds
    alert_data = {
        "id": DATA["next_id"],
        "guild_id": ctx.guild.id,
        "channel_id": channel.id,
        "user_id": ctx.author.id,
        "coin_id": c["id"],
        "coin_name": c["name"],
        "symbol": c["symbol"].upper(),
        "interval": seconds,
        "next_run": next_run,
    }
    DATA["next_id"] += 1
    DATA["alerts"].append(alert_data)
    save_data()

    embed = discord.Embed(title="⏰ تم إعداد التنبيه الدوري", color=GREEN)
    embed.set_thumbnail(url=c.get("image"))
    embed.add_field(name="العملة", value=f"{c['name']} ({alert_data['symbol']})", inline=True)
    embed.add_field(name="كل", value=human_interval(seconds), inline=True)
    embed.add_field(name="القناة", value=channel.mention, inline=True)
    embed.add_field(name="أول تحديث", value=f"<t:{int(next_run)}:R>", inline=True)
    embed.add_field(name="رقم التنبيه", value=f"`#{alert_data['id']}`", inline=True)
    embed.set_footer(text="لإيقافه: !stopalert " + str(alert_data["id"]))
    await ctx.send(embed=embed)


@bot.command(name="alerts", aliases=["myalerts"])
@commands.guild_only()
async def alerts(ctx):
    items = [a for a in DATA["alerts"] if a["guild_id"] == ctx.guild.id]
    if not items:
        await ctx.send(embed=discord.Embed(
            description="لا توجد تنبيهات في هذا الخادم.\nأنشئ واحدًا بـ `!alert btc 1h`",
            color=GOLD,
        ))
        return
    lines = []
    for a in items:
        lines.append(
            f"**`#{a['id']}`** • **{a['coin_name']}** ({a['symbol']}) — كل {human_interval(a['interval'])}\n"
            f"　↳ <#{a['channel_id']}> • التالي <t:{int(a['next_run'])}:R> • <@{a['user_id']}>"
        )
    embed = discord.Embed(
        title="⏰ التنبيهات الدورية",
        description="\n\n".join(lines),
        color=GOLD,
    )
    embed.set_footer(text="لحذف تنبيه: !stopalert <الرقم>")
    await ctx.send(embed=embed)


@bot.command(name="stopalert", aliases=["delalert"], usage="<رقم التنبيه>")
@commands.guild_only()
async def stopalert(ctx, alert_id: int):
    found = next(
        (a for a in DATA["alerts"] if a["id"] == alert_id and a["guild_id"] == ctx.guild.id),
        None,
    )
    if not found:
        await ctx.send(embed=error_embed("لا يوجد تنبيه بهذا الرقم في هذا الخادم."))
        return
    if ctx.author.id != found["user_id"] and not ctx.author.guild_permissions.manage_guild:
        await ctx.send(embed=error_embed("يمكن حذف التنبيه لصاحبه أو لمن يملك صلاحية إدارة الخادم فقط."))
        return
    DATA["alerts"].remove(found)
    save_data()
    await ctx.send(embed=ok_embed(f"تم حذف التنبيه `#{alert_id}` ({found['coin_name']})."))


@bot.command(name="quickprice", usage="on | off")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def quickprice(ctx, state: str):
    state = state.lower()
    gid = ctx.guild.id
    if state in ("on", "تشغيل"):
        if gid in DATA["quick_off"]:
            DATA["quick_off"].remove(gid)
        save_data()
        await ctx.send(embed=ok_embed("تم **تشغيل** الرد على اسم العملة المباشر."))
    elif state in ("off", "ايقاف", "إيقاف"):
        if gid not in DATA["quick_off"]:
            DATA["quick_off"].append(gid)
        save_data()
        await ctx.send(embed=ok_embed("تم **إيقاف** الرد على اسم العملة المباشر."))
    else:
        await ctx.send(embed=error_embed("الاستخدام: `!quickprice on` أو `!quickprice off`"))


@bot.command(name="help", aliases=["h", "commands"])
async def help_cmd(ctx):
    embed = discord.Embed(
        title="📖 دليل أوامر البوت",
        description="أهلًا بك! هذا البوت يعرض أسعار العملات الرقمية لحظيًا، ويرسلها تلقائيًا في الأوقات التي تحددها.",
        color=BLUE,
    )
    embed.add_field(
        name="💰 الأسعار",
        value=(
            "• اكتب **اسم العملة أو رمزها فقط** بدون أي أمر، مثل: `btc` أو `bitcoin`\n"
            "• `!price <العملة>` — سعر عملة مع التفاصيل\n"
            "• `!top [العدد]` — أفضل العملات بالقيمة السوقية، مثل `!top 5`"
        ),
        inline=False,
    )
    embed.add_field(
        name="⏰ التنبيهات الدورية",
        value=(
            "• `!alert <العملة> <المدة> [#القناة]` — إرسال السعر تلقائيًا كل مدة\n"
            "　مثال: `!alert btc 1h` أو `!alert eth 30m #الأسعار`\n"
            "• صيغ المدة: `30m` دقائق، `2h` ساعات، `1d` أيام، `1h30m`\n"
            "• المدة من **5 دقائق** إلى **7 أيام**\n"
            "• `!alerts` — عرض تنبيهات الخادم\n"
            "• `!stopalert <الرقم>` — حذف تنبيه"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚙️ للمشرفين",
        value="• `!quickprice on/off` — تشغيل أو إيقاف الرد على اسم العملة المباشر",
        inline=False,
    )
    embed.set_footer(text=f"حتى {MAX_PER_USER} تنبيهات لكل مستخدم • البيانات من CoinGecko")
    await ctx.send(embed=embed)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
        usage = f"!{ctx.command.name} {ctx.command.usage or ''}".strip()
        await ctx.send(embed=error_embed(f"طريقة الاستخدام: `{usage}`\nاكتب `!help` لعرض كل الأوامر."))
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send(embed=error_embed("هذا الأمر للمشرفين فقط."))
    elif isinstance(error, commands.NoPrivateMessage):
        await ctx.send(embed=error_embed("هذا الأمر يعمل داخل الخوادم فقط."))
    else:
        print("command error:", repr(error))


# ------------------------- المهام الدورية -------------------------
@tasks.loop(hours=1)
async def refresh_loop():
    await refresh_coins()


@tasks.loop(seconds=30)
async def alert_loop():
    now = time.time()
    due = [a for a in DATA["alerts"] if a["next_run"] <= now]
    if not due:
        return
    markets = await fetch_markets({a["coin_id"] for a in due})
    if not markets:
        return  # نعيد المحاولة في الدورة القادمة
    by_id = {c["id"]: c for c in markets}
    dead = set()

    for a in due:
        a["next_run"] = now + a["interval"]
        coin = by_id.get(a["coin_id"])
        if not coin:
            continue
        channel = bot.get_channel(a["channel_id"])
        if channel is None:
            try:
                channel = await bot.fetch_channel(a["channel_id"])
            except discord.NotFound:
                dead.add(a["id"])
                continue
            except discord.HTTPException:
                continue
        try:
            await channel.send(
                embed=coin_embed(coin, footer=f"⏰ تحديث دوري كل {human_interval(a['interval'])}")
            )
        except discord.HTTPException:
            pass

    if dead:
        DATA["alerts"] = [a for a in DATA["alerts"] if a["id"] not in dead]
    save_data()


@alert_loop.before_loop
@refresh_loop.before_loop
async def before_loops():
    await bot.wait_until_ready()


bot.run(TOKEN)
