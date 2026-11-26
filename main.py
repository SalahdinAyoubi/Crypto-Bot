import os
import aiohttp
import discord
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

API = "https://api.coingecko.com/api/v3/simple/price"


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


@bot.command(name="price")
async def price(ctx, coin: str = "bitcoin"):
    coin = coin.lower()
    params = {"ids": coin, "vs_currencies": "usd", "include_24hr_change": "true"}
    async with aiohttp.ClientSession() as session:
        async with session.get(API, params=params) as r:
            data = await r.json()

    if coin not in data:
        await ctx.send("❌ العملة غير موجودة. جرّب: bitcoin, ethereum, solana")
        return

    p = data[coin]["usd"]
    ch = data[coin].get("usd_24h_change", 0)
    arrow = "📈" if ch >= 0 else "📉"
    await ctx.send(f"💰 {coin.capitalize()}: ${p:,.2f}\n{arrow} آخر 24 ساعة: {ch:.2f}%")


bot.run(TOKEN)
