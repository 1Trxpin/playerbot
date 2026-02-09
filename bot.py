import os
import datetime as dt
import discord
from discord import app_commands
from discord.ext import commands
import asyncpg
import aiohttp
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

ALLOWED_IDS = {
int(x.strip())
for x in os.getenv("ALLOWED_IDS", "").split(",")
if x.strip().isdigit()
}

INTENTS = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=INTENTS)

pool: asyncpg.Pool | None = None
FREE_AGENT_TEAM = "Free Agent"

routes = web.RouteTableDef()

# -------------------------

# Helpers

# -------------------------

def utc_now_iso() -> str:
return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

async def roblox_username_to_userid(username: str) -> int | None:
url = "[https://users.roblox.com/v1/usernames/users](https://users.roblox.com/v1/usernames/users)"
payload = {"usernames": [username], "excludeBannedUsers": False}

```
async with aiohttp.ClientSession() as session:
    async with session.post(url, json=payload) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()

items = data.get("data", [])
if not items:
    return None
return int(items[0]["id"])
```

async def init_db():
assert pool is not None
async with pool.acquire() as conn:

```
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS teams (
            name TEXT PRIMARY KEY,
            owner_roblox TEXT NOT NULL,
            manager_roblox TEXT,
            logo_asset_id BIGINT
        );
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS players (
            roblox_user_id BIGINT PRIMARY KEY,
            roblox_username TEXT NOT NULL,
            team_name TEXT NOT NULL REFERENCES teams(name) ON DELETE RESTRICT,
            rank TEXT,
            updated_at TEXT NOT NULL
        );
    """)

    await conn.execute(
        """
        INSERT INTO teams (name, owner_roblox, manager_roblox, logo_asset_id)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (name) DO NOTHING;
        """,
        FREE_AGENT_TEAM, "System", None, None
    )
```

# -------------------------

# Bot lifecycle

# -------------------------

@bot.event
async def setup_hook():
app = web.Application()
app.add_routes(routes)

```
runner = web.AppRunner(app)
await runner.setup()

site = web.TCPSite(runner, "0.0.0.0", 8000)
await site.start()
```

@bot.event
async def on_ready():
global pool

```
pool = await asyncpg.create_pool(DATABASE_URL)
await init_db()

if GUILD_ID:
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)

print(f"Logged in as {bot.user}")
```

# -------------------------

# Rank player (UserId system)

# -------------------------

@bot.tree.command(name="rankplayer")
@app_commands.check(lambda i: i.user.id in ALLOWED_IDS)
async def rankplayer(interaction: discord.Interaction, robloxuser: str, team: str, rank: str):

```
user_id = await roblox_username_to_userid(robloxuser)
if not user_id:
    return await interaction.response.send_message("Roblox user not found.", ephemeral=True)

now = utc_now_iso()

async with pool.acquire() as conn:
    await conn.execute(
        """
        INSERT INTO players (roblox_user_id, roblox_username, team_name, rank, updated_at)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (roblox_user_id) DO UPDATE SET
            roblox_username = EXCLUDED.roblox_username,
            team_name = EXCLUDED.team_name,
            rank = EXCLUDED.rank,
            updated_at = EXCLUDED.updated_at;
        """,
        user_id, robloxuser, team, rank, now
    )

await interaction.response.send_message(f"{robloxuser} ranked to {team}.")
```

@bot.tree.command(name="unrank")
@app_commands.check(lambda i: i.user.id in ALLOWED_IDS)
async def unrank(interaction: discord.Interaction, robloxuser: str):

```
user_id = await roblox_username_to_userid(robloxuser)
if not user_id:
    return await interaction.response.send_message("Roblox user not found.", ephemeral=True)

now = utc_now_iso()

async with pool.acquire() as conn:
    await conn.execute(
        """
        INSERT INTO players (roblox_user_id, roblox_username, team_name, rank, updated_at)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (roblox_user_id) DO UPDATE SET
            roblox_username = EXCLUDED.roblox_username,
            team_name = EXCLUDED.team_name,
            rank = EXCLUDED.rank,
            updated_at = EXCLUDED.updated_at;
        """,
        user_id, robloxuser, FREE_AGENT_TEAM, "Free Agent", now
    )

await interaction.response.send_message(f"{robloxuser} is now Free Agent.")
```

# -------------------------

# API for Roblox

# -------------------------

@routes.get("/leaderboard")
async def leaderboard_api(request):

```
async with pool.acquire() as conn:
    rows = await conn.fetch(
        """
        SELECT p.roblox_user_id, p.roblox_username, p.team_name, t.logo_asset_id
        FROM players p
        LEFT JOIN teams t ON t.name = p.team_name
        """
    )

return web.json_response([
    {
        "userId": int(r["roblox_user_id"]),
        "username": r["roblox_username"],
        "team": r["team_name"],
        "logo": r["logo_asset_id"],
    }
    for r in rows
])
```

bot.run(TOKEN)



















