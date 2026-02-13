import os
import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands

import asyncpg
from dotenv import load_dotenv
from aiohttp import web

# -------------------------
# ENV
# -------------------------

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

ALLOWED_IDS = set(
    int(x.strip())
    for x in os.getenv("ALLOWED_IDS", "").split(",")
    if x.strip().isdigit()
)

INTENTS = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=INTENTS)

pool: asyncpg.Pool | None = None
FREE_AGENT_TEAM = "Free Agent"

# -------------------------
# Helpers
# -------------------------

def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def rbxthumb_asset(asset_id: int) -> str:
    return (
        "https://www.roblox.com/asset-thumbnail/image"
        f"?assetId={asset_id}&width=420&height=420&format=png"
    )


def require_allowed_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id not in ALLOWED_IDS:
            msg = "❌ Only league staff can use this command."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return False
        return True

    return app_commands.check(predicate)


# -------------------------
# Database
# -------------------------

async def init_db():
    assert pool is not None
    async with pool.acquire() as conn:
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
                roblox_user TEXT PRIMARY KEY,
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


async def fetch_team_names_like(current: str, include_free_agent: bool = True):
    assert pool is not None
    current = (current or "").strip().lower()

    async with pool.acquire() as conn:
        if include_free_agent:
            rows = await conn.fetch(
                "SELECT name FROM teams WHERE LOWER(name) LIKE $1 ORDER BY name LIMIT 25",
                f"%{current}%"
            )
        else:
            rows = await conn.fetch(
                "SELECT name FROM teams WHERE name <> $1 AND LOWER(name) LIKE $2 ORDER BY name LIMIT 25",
                FREE_AGENT_TEAM, f"%{current}%"
            )

    return [r["name"] for r in rows]


# -------------------------
# Bot Ready
# -------------------------

@bot.event
async def on_ready():
    global pool

    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN missing.")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL missing.")

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    await init_db()

    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"✅ Synced commands to guild {GUILD_ID}")

    print(f"✅ Logged in as {bot.user}")


# -------------------------
# Team Commands
# -------------------------

@bot.tree.command(name="setteam")
@require_allowed_only()
async def setteam(interaction: discord.Interaction, team: str, owner: str, manager: str | None = None, logo_asset_id: int | None = None):
    assert pool is not None

    if team.lower() == FREE_AGENT_TEAM.lower():
        return await interaction.response.send_message("❌ Cannot edit Free Agent.", ephemeral=True)

    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO teams (name, owner_roblox, manager_roblox, logo_asset_id)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (name) DO UPDATE SET
                owner_roblox=EXCLUDED.owner_roblox,
                manager_roblox=EXCLUDED.manager_roblox,
                logo_asset_id=EXCLUDED.logo_asset_id;
        """, team, owner, manager, logo_asset_id)

    embed = discord.Embed(title="Team Saved", description=team, color=discord.Color.green())
    if logo_asset_id:
        embed.set_thumbnail(url=rbxthumb_asset(logo_asset_id))

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="rankplayer")
@require_allowed_only()
async def rankplayer(interaction: discord.Interaction, robloxuser: str, team: str, rank: str):
    assert pool is not None
    now = utc_now_iso()

    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM teams WHERE name=$1", team)
        if not exists:
            return await interaction.response.send_message("❌ Invalid team.", ephemeral=True)

        await conn.execute("""
            INSERT INTO players (roblox_user, team_name, rank, updated_at)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (roblox_user) DO UPDATE SET
                team_name=EXCLUDED.team_name,
                rank=EXCLUDED.rank,
                updated_at=EXCLUDED.updated_at;
        """, robloxuser, team, rank, now)

        logo = await conn.fetchval("SELECT logo_asset_id FROM teams WHERE name=$1", team)

    embed = discord.Embed(title="Player Ranked", description=f"{robloxuser} → {team}", color=discord.Color.green())
    if logo:
        embed.set_thumbnail(url=rbxthumb_asset(logo))

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="unrank")
@require_allowed_only()
async def unrank(interaction: discord.Interaction, robloxuser: str):
    assert pool is not None
    now = utc_now_iso()

    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO players (roblox_user, team_name, rank, updated_at)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (roblox_user) DO UPDATE SET
                team_name=EXCLUDED.team_name,
                rank=EXCLUDED.rank,
                updated_at=EXCLUDED.updated_at;
        """, robloxuser, FREE_AGENT_TEAM, "Free Agent", now)

    await interaction.response.send_message(f"{robloxuser} is now Free Agent.")


# -------------------------
# Roblox API
# -------------------------

routes = web.RouteTableDef()


@routes.get("/leaderboard")
async def leaderboard_api(request):
    assert pool is not None

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT p.roblox_user, p.team_name, t.logo_asset_id
            FROM players p
            LEFT JOIN teams t ON t.name=p.team_name
            ORDER BY LOWER(p.roblox_user)
        """)

    return web.json_response([
        {"player": r["roblox_user"], "team": r["team_name"], "logo": r["logo_asset_id"]}
        for r in rows
    ])


async def start_web_server():
    PORT = int(os.getenv("PORT", "8000"))

    app = web.Application()
    app.add_routes(routes)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    print(f"🌐 Web API listening on port {PORT}")


@bot.event
async def setup_hook():
    await start_web_server()


# -------------------------
# Run
# -------------------------

bot.run(TOKEN)


























