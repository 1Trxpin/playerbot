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

bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
pool: asyncpg.Pool | None = None

routes = web.RouteTableDef()
FREE_AGENT_TEAM = "Free Agent"

# ----------------- HELPERS -----------------

def utc_now_iso():
return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

async def roblox_username_to_userid(username: str):
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

# ----------------- DATABASE -----------------

async def init_db():
async with pool.acquire() as conn:

```
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS teams (
            name TEXT PRIMARY KEY,
            logo_asset_id BIGINT
        );
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS players (
            roblox_user_id BIGINT PRIMARY KEY,
            roblox_username TEXT NOT NULL,
            team_name TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)

    await conn.execute(
        """
        INSERT INTO teams (name, logo_asset_id)
        VALUES ($1, $2)
        ON CONFLICT (name) DO NOTHING;
        """,
        FREE_AGENT_TEAM, None
    )
```

# ----------------- BOT START -----------------

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
pool = await asyncpg.create_pool(DATABASE_URL)
await init_db()
print("BOT ONLINE:", bot.user)

# ----------------- COMMANDS -----------------

@bot.tree.command(name="rankplayer")
async def rankplayer(interaction: discord.Interaction, robloxuser: str, team: str):

```
user_id = await roblox_username_to_userid(robloxuser)
if not user_id:
    return await interaction.response.send_message("Roblox user not found.", ephemeral=True)

now = utc_now_iso()

async with pool.acquire() as conn:
    await conn.execute(
        """
        INSERT INTO players (roblox_user_id, roblox_username, team_name, updated_at)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (roblox_user_id) DO UPDATE SET
            roblox_username = EXCLUDED.roblox_username,
            team_name = EXCLUDED.team_name,
            updated_at = EXCLUDED.updated_at;
        """,
        user_id, robloxuser, team, now
    )

await interaction.response.send_message(f"{robloxuser} ranked to {team}.")
```

# ----------------- ROBLOX API -----------------

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





















