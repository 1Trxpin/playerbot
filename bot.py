import os
import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands

import asyncpg
from dotenv import load_dotenv
from aiohttp import web

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

# Comma-separated Discord IDs allowed to MANAGE the league (teams + ranking)
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
    """ONLY Discord IDs in ALLOWED_IDS can use the command."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id not in ALLOWED_IDS:
            msg = "❌ Only league staff (ALLOWED_IDS) can use this command."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return False
        return True

    return app_commands.check(predicate)


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

        # Ensure "Free Agent" team always exists (so /unrank can move players there)
        await conn.execute(
            """
            INSERT INTO teams (name, owner_roblox, manager_roblox, logo_asset_id)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (name) DO NOTHING;
            """,
            FREE_AGENT_TEAM, "System", None, None
        )


async def fetch_team_names_like(current: str, include_free_agent: bool = True) -> list[str]:
    """Return up to 25 team names matching the typed text."""
    assert pool is not None
    current = (current or "").strip().lower()

    async with pool.acquire() as conn:
        if include_free_agent:
            rows = await conn.fetch(
                """
                SELECT name
                FROM teams
                WHERE LOWER(name) LIKE $1
                ORDER BY name
                LIMIT 25
                """,
                f"%{current}%",
            )
        else:
            rows = await conn.fetch(
                """
                SELECT name
                FROM teams
                WHERE name <> $1 AND LOWER(name) LIKE $2
                ORDER BY name
                LIMIT 25
                """,
                FREE_AGENT_TEAM, f"%{current}%",
            )

    return [r["name"] for r in rows]


# -------------------------
# WEB API for Roblox (Railway)
# -------------------------

routes = web.RouteTableDef()


@routes.get("/leaderboard")
async def leaderboard_api(request):
    """Return all ranked players + team logos for Roblox."""
    assert pool is not None

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.roblox_user, p.team_name, t.logo_asset_id
            FROM players p
            LEFT JOIN teams t ON t.name = p.team_name
            ORDER BY LOWER(p.roblox_user)
            """
        )

    return web.json_response([
        {
            "player": r["roblox_user"],
            "team": r["team_name"],
            "logo": r["logo_asset_id"],  # can be null
        }
        for r in rows
    ])


async def start_web_server():
    """Start aiohttp server on Railway's PORT."""
    app = web.Application()
    app.add_routes(routes)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "8000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print(f"🌐 Web API listening on port {port}")


@bot.event
async def setup_hook():
    # start the web server once, before the bot fully comes online
    await start_web_server()


# -------------------------
# Bot lifecycle
# -------------------------

@bot.event
async def on_ready():
    global pool

    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN missing (Railway -> playerbot -> Variables).")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL missing (Railway Postgres -> DATABASE_URL).")

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    await init_db()

    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"✅ Synced slash commands to guild {GUILD_ID}")

    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")


# -------------------------
# Team management (LOCKED to ALLOWED_IDS)
# -------------------------

@bot.tree.command(name="setteam", description="Create/update a league team (staff only).")
@app_commands.describe(
    team="Team name (league team)",
    owner="Owner Roblox username",
    manager="Manager Roblox username (optional)",
    logo_asset_id="Roblox image asset id (optional)"
)
@require_allowed_only()
async def setteam(
    interaction: discord.Interaction,
    team: str,
    owner: str,
    manager: str | None = None,
    logo_asset_id: int | None = None,
):
    assert pool is not None

    if team.strip().lower() == FREE_AGENT_TEAM.lower():
        return await interaction.response.send_message(
            f"❌ `{FREE_AGENT_TEAM}` is reserved. You cannot edit it.",
            ephemeral=True
        )

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO teams (name, owner_roblox, manager_roblox, logo_asset_id)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (name) DO UPDATE SET
                owner_roblox = EXCLUDED.owner_roblox,
                manager_roblox = EXCLUDED.manager_roblox,
                logo_asset_id = EXCLUDED.logo_asset_id;
            """,
            team, owner, manager, logo_asset_id
        )

    embed = discord.Embed(
        title="✅ Team Saved",
        description=f"**{team}** is now a valid league team.",
        color=discord.Color.green(),
    )
    embed.add_field(name="Owner", value=owner, inline=True)
    embed.add_field(name="Manager", value=manager or "None", inline=True)
    embed.add_field(name="Logo Asset ID", value=str(logo_asset_id) if logo_asset_id else "None", inline=False)
    if logo_asset_id:
        embed.set_thumbnail(url=rbxthumb_asset(int(logo_asset_id)))

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="deleteteam", description="Delete a league team (staff only).")
@app_commands.describe(teamname="Team name to delete")
@require_allowed_only()
async def deleteteam(interaction: discord.Interaction, teamname: str):
    assert pool is not None

    if teamname.strip().lower() == FREE_AGENT_TEAM.lower():
        return await interaction.response.send_message(
            f"❌ `{FREE_AGENT_TEAM}` is reserved and cannot be deleted.",
            ephemeral=True
        )

    async with pool.acquire() as conn:
        # Block deletion if the team still has players
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM players WHERE team_name=$1",
            teamname
        )
        if count and int(count) > 0:
            return await interaction.response.send_message(
                f"❌ Cannot delete **{teamname}** because it has **{count}** players.\n"
                f"Move/unrank those players first.",
                ephemeral=True
            )

        result = await conn.execute(
            "DELETE FROM teams WHERE name=$1",
            teamname
        )

    if result.endswith("0"):
        return await interaction.response.send_message("❌ Team not found.", ephemeral=True)

    await interaction.response.send_message(f"✅ Deleted team **{teamname}**.")


# -------------------------
# Player ranking (LOCKED to ALLOWED_IDS)
# -------------------------

@bot.tree.command(name="rankplayer", description="Assign a Roblox player to a league team (staff only).")
@app_commands.describe(
    robloxuser="Roblox username",
    team="League team (must exist)",
    rank="Rank (Player/Manager/Owner/etc.)"
)
@require_allowed_only()
async def rankplayer(
    interaction: discord.Interaction,
    robloxuser: str,
    team: str,
    rank: str,
):
    assert pool is not None
    now = utc_now_iso()

    if team.strip().lower() == FREE_AGENT_TEAM.lower():
        return await interaction.response.send_message(
            f"❌ Use `/unrank robloxuser: {robloxuser}` to set Free Agent.",
            ephemeral=True
        )

    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM teams WHERE name=$1", team)
        if not exists:
            return await interaction.response.send_message(
                f"❌ **{team}** is not a valid league team.\n"
                f"Create it first with `/setteam`.",
                ephemeral=True
            )

        old_team = await conn.fetchval(
            "SELECT team_name FROM players WHERE roblox_user=$1",
            robloxuser
        )

        await conn.execute(
            """
            INSERT INTO players (roblox_user, team_name, rank, updated_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (roblox_user) DO UPDATE SET
                team_name = EXCLUDED.team_name,
                rank = EXCLUDED.rank,
                updated_at = EXCLUDED.updated_at;
            """,
            robloxuser, team, rank, now
        )

        logo_asset_id = await conn.fetchval(
            "SELECT logo_asset_id FROM teams WHERE name=$1",
            team
        )

    if old_team and old_team.lower() != team.lower():
        desc = f"🔄 **{robloxuser}** moved from **{old_team}** to **{team}** as **{rank}**"
    elif old_team:
        desc = f"✅ **{robloxuser}** updated in **{team}** as **{rank}**"
    else:
        desc = f"✅ **{robloxuser}** added to **{team}** as **{rank}**"

    embed = discord.Embed(title="Player Ranked", description=desc, color=discord.Color.green())
    embed.add_field(name="Updated", value=now, inline=False)
    if logo_asset_id:
        embed.set_thumbnail(url=rbxthumb_asset(int(logo_asset_id)))

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="unrank", description="Set a player to Free Agent (staff only).")
@app_commands.describe(robloxuser="Roblox username")
@require_allowed_only()
async def unrank(interaction: discord.Interaction, robloxuser: str):
    assert pool is not None
    now = utc_now_iso()

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO players (roblox_user, team_name, rank, updated_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (roblox_user) DO UPDATE SET
                team_name = EXCLUDED.team_name,
                rank = EXCLUDED.rank,
                updated_at = EXCLUDED.updated_at;
            """,
            robloxuser, FREE_AGENT_TEAM, "Free Agent", now
        )

    await interaction.response.send_message(f"✅ **{robloxuser}** is now a **Free Agent**.")


# -------------------------
# Viewing commands (PUBLIC)
# -------------------------

@bot.tree.command(name="teamview", description="View a team’s owner/manager/players.")
@app_commands.describe(teamname="Team name")
async def teamview(interaction: discord.Interaction, teamname: str):
    assert pool is not None

    async with pool.acquire() as conn:
        team_row = await conn.fetchrow(
            "SELECT owner_roblox, manager_roblox, logo_asset_id FROM teams WHERE name=$1",
            teamname
        )
        if not team_row:
            return await interaction.response.send_message("❌ Team not found.", ephemeral=True)

        players = await conn.fetch(
            "SELECT roblox_user, rank FROM players WHERE team_name=$1 ORDER BY LOWER(roblox_user)",
            teamname
        )

    embed = discord.Embed(
        title=f"Information for {teamname} ({len(players)} Players)",
        color=discord.Color.dark_teal(),
    )
    embed.add_field(name="Owner", value=team_row["owner_roblox"], inline=False)
    embed.add_field(name="Manager", value=team_row["manager_roblox"] or "None", inline=False)

    if team_row["logo_asset_id"]:
        embed.set_thumbnail(url=rbxthumb_asset(int(team_row["logo_asset_id"])))
        embed.add_field(name="Logo Asset ID", value=str(team_row["logo_asset_id"]), inline=False)

    if not players:
        embed.add_field(name="Players", value="None", inline=False)
    else:
        lines = [f"{p['roblox_user']} ({p['rank'] or 'None'})" for p in players]
        chunk = ""
        part = 1
        for line in lines:
            add = ("\n" if chunk else "") + line
            if len(chunk) + len(add) > 900:
                embed.add_field(name=f"Players (Part {part})", value=chunk, inline=False)
                part += 1
                chunk = line
            else:
                chunk += add
        if chunk:
            embed.add_field(name=f"Players (Part {part})", value=chunk, inline=False)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="playerinfo", description="Show info about a Roblox player.")
@app_commands.describe(robloxuser="Roblox username")
async def playerinfo(interaction: discord.Interaction, robloxuser: str):
    assert pool is not None

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT p.team_name, p.rank, p.updated_at,
                   t.logo_asset_id, t.owner_roblox, t.manager_roblox
            FROM players p
            LEFT JOIN teams t ON t.name = p.team_name
            WHERE p.roblox_user=$1
            """,
            robloxuser
        )

    if not row:
        return await interaction.response.send_message("❌ Player not found.", ephemeral=True)

    embed = discord.Embed(
        title=f"{robloxuser}'s Information!",
        color=discord.Color.orange(),
    )
    embed.add_field(name="Team", value=row["team_name"], inline=True)
    embed.add_field(name="Rank", value=row["rank"] or "None", inline=True)
    embed.add_field(name="Last Update", value=row["updated_at"], inline=False)

    embed.add_field(name="Team Owner", value=row["owner_roblox"] or "Unknown", inline=True)
    embed.add_field(name="Team Manager", value=row["manager_roblox"] or "None", inline=True)

    if row["logo_asset_id"]:
        embed.set_thumbnail(url=rbxthumb_asset(int(row["logo_asset_id"])))
        embed.add_field(name="Logo Asset ID", value=str(row["logo_asset_id"]), inline=False)

    await interaction.response.send_message(embed=embed)


# -------------------------
# Autocomplete (Dropdowns)
# -------------------------

@teamview.autocomplete("teamname")
async def teamname_autocomplete(interaction: discord.Interaction, current: str):
    names = await fetch_team_names_like(current, include_free_agent=True)
    return [app_commands.Choice(name=n, value=n) for n in names]


@rankplayer.autocomplete("team")
async def rankplayer_team_autocomplete(interaction: discord.Interaction, current: str):
    names = await fetch_team_names_like(current, include_free_agent=False)
    return [app_commands.Choice(name=n, value=n) for n in names]


@deleteteam.autocomplete("teamname")
async def deleteteam_autocomplete(interaction: discord.Interaction, current: str):
    names = await fetch_team_names_like(current, include_free_agent=False)
    return [app_commands.Choice(name=n, value=n) for n in names]


# -------------------------
# Run
# -------------------------

bot.run(TOKEN)
























