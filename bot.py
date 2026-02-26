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

# Comma-separated Discord IDs allowed to MANAGE the league (teams + ranking)
ALLOWED_IDS = set(
    int(x.strip())
    for x in os.getenv("ALLOWED_IDS", "").split(",")
    if x.strip().isdigit()
)

FREE_AGENT_TEAM = "Free Agent"

# Discord
INTENTS = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=INTENTS)

# DB pool
pool: asyncpg.Pool | None = None

# Web API (Roblox)
routes = web.RouteTableDef()


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
    """Only Discord IDs in ALLOWED_IDS can use the command."""
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


# -------------------------
# Database
# -------------------------
async def init_db():
    """Create tables + ensure Free Agent exists + add division column if missing."""
    assert pool is not None
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS teams (
                name TEXT PRIMARY KEY,
                owner_roblox TEXT NOT NULL,
                manager_roblox TEXT,
                logo_asset_id BIGINT,
                division TEXT
            );
        """)

        # Safe migration if older DB existed without division
        try:
            await conn.execute("ALTER TABLE teams ADD COLUMN division TEXT;")
        except asyncpg.exceptions.DuplicateColumnError:
            pass

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS players (
                roblox_user TEXT PRIMARY KEY,
                team_name TEXT NOT NULL REFERENCES teams(name) ON DELETE RESTRICT,
                rank TEXT,
                updated_at TEXT NOT NULL
            );
        """)

        # Ensure Free Agent team always exists
        await conn.execute(
            """
            INSERT INTO teams (name, owner_roblox, manager_roblox, logo_asset_id, division)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (name) DO NOTHING;
            """,
            FREE_AGENT_TEAM, "System", None, None, "None"
        )


async def fetch_team_names_like(current: str, include_free_agent: bool = True) -> list[str]:
    """Autocomplete helper: return up to 25 matching team names."""
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
                f"%{current}%"
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
                FREE_AGENT_TEAM, f"%{current}%"
            )

    return [r["name"] for r in rows]


# -------------------------
# Web API for Roblox
# -------------------------
@routes.get("/health")
async def health(_request):
    return web.json_response({"ok": True})


@routes.get("/leaderboard")
async def leaderboard_api(_request):
    """Return all players + their team + team logo asset id."""
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

    data = [
        {"player": r["roblox_user"], "team": r["team_name"], "logo": r["logo_asset_id"]}
        for r in rows
    ]
    return web.json_response(data)


@routes.get("/player/{roblox_user}")
async def player_api(request):
    """Return one player by username (handy for Roblox)."""
    assert pool is not None
    roblox_user = request.match_info["roblox_user"]

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT p.roblox_user, p.team_name, p.rank, p.updated_at,
                   t.logo_asset_id, t.division
            FROM players p
            LEFT JOIN teams t ON t.name = p.team_name
            WHERE LOWER(p.roblox_user) = LOWER($1)
            """,
            roblox_user
        )

    if not row:
        return web.json_response({"found": False}, status=404)

    return web.json_response({
        "found": True,
        "player": row["roblox_user"],
        "team": row["team_name"],
        "rank": row["rank"],
        "updated_at": row["updated_at"],
        "logo": row["logo_asset_id"],
        "division": row["division"] or "None",
    })


async def start_web_server():
    # Railway is routing your domain to port 8000, so we force 8000.
    PORT = 8000

    app = web.Application()
    app.add_routes(routes)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    print(f"🌐 Web API listening on port {PORT}")


# -------------------------
# Bot lifecycle
# -------------------------
@bot.event
async def setup_hook():
    """Runs before on_ready; good place to start DB + web server."""
    global pool

    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing.")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing.")

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    await init_db()

    await start_web_server()


@bot.event
async def on_ready():
    # Slash commands sync (guild faster)
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"✅ Synced slash commands to guild {GUILD_ID}")

    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")


# -------------------------
# Team management (LOCKED)
# -------------------------
@bot.tree.command(name="setteam", description="Create/update a league team (staff only).")
@app_commands.describe(
    team="Team name (league team)",
    owner="Owner Roblox username",
    manager="Manager Roblox username (optional)",
    logo_asset_id="Roblox image asset id (optional)",
    division="Division name (e.g. Division 1, Division 2)"
)
@require_allowed_only()
async def setteam(
    interaction: discord.Interaction,
    team: str,
    owner: str,
    manager: str | None = None,
    logo_asset_id: int | None = None,
    division: str | None = None,
):
    assert pool is not None

    if team.strip().lower() == FREE_AGENT_TEAM.lower():
        return await interaction.response.send_message(
            f"❌ `{FREE_AGENT_TEAM}` is reserved. You cannot edit it.",
            ephemeral=True
        )

    div_value = (division or "None").strip() or "None"

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO teams (name, owner_roblox, manager_roblox, logo_asset_id, division)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (name) DO UPDATE SET
                owner_roblox = EXCLUDED.owner_roblox,
                manager_roblox = EXCLUDED.manager_roblox,
                logo_asset_id = EXCLUDED.logo_asset_id,
                division = EXCLUDED.division;
            """,
            team, owner, manager, logo_asset_id, div_value
        )

    embed = discord.Embed(
        title="✅ Team Saved",
        description=f"**{team}** is now a valid league team.",
        color=discord.Color.green(),
    )
    embed.add_field(name="Owner", value=owner, inline=True)
    embed.add_field(name="Manager", value=manager or "None", inline=True)
    embed.add_field(name="Division", value=div_value, inline=True)
    embed.add_field(
        name="Logo Asset ID",
        value=str(logo_asset_id) if logo_asset_id else "None",
        inline=False
    )
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

        result = await conn.execute("DELETE FROM teams WHERE name=$1", teamname)

    if result.endswith("0"):
        return await interaction.response.send_message("❌ Team not found.", ephemeral=True)

    await interaction.response.send_message(f"✅ Deleted team **{teamname}**.")


# -------------------------
# Player ranking (LOCKED)
# -------------------------
@bot.tree.command(name="rankplayer", description="Assign a Roblox player to a league team (staff only).")
@app_commands.describe(
    robloxuser="Roblox username",
    team="League team (must exist)",
    rank="Rank (Player/Manager/Owner/etc.)"
)
@require_allowed_only()
async def rankplayer(interaction: discord.Interaction, robloxuser: str, team: str, rank: str):
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
                f"❌ **{team}** is not a valid league team.\nCreate it first with `/setteam`.",
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
            "SELECT owner_roblox, manager_roblox, logo_asset_id, division FROM teams WHERE name=$1",
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
    embed.add_field(name="Division", value=team_row["division"] or "None", inline=False)

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


# -------------------------
# /playerinfo (shows Division from team + optional Team Owner ID)
# -------------------------
@bot.tree.command(name="playerinfo", description="Show info about a Roblox player.")
@app_commands.describe(robloxuser="Roblox username")
async def playerinfo(interaction: discord.Interaction, robloxuser: str):
    assert pool is not None

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT p.team_name, p.rank, p.updated_at,
                   t.owner_roblox, t.division
            FROM players p
            LEFT JOIN teams t ON t.name = p.team_name
            WHERE LOWER(p.roblox_user) = LOWER($1)
            """,
            robloxuser
        )

    if not row:
        return await interaction.response.send_message("❌ Player not found.", ephemeral=True)

    team_name = row["team_name"] or FREE_AGENT_TEAM
    rank_raw = (row["rank"] or "None")
    rank_norm = rank_raw.strip().lower()
    updated = row["updated_at"] or "Unknown"
    division = row["division"] or "None"
    team_owner_id = row["owner_roblox"] or "Unknown"

    suspended = "❌"
    manager_status = "✅" if rank_norm == "manager" else "❌"
    owner_status = "✅" if rank_norm == "owner" else "❌"
    staff_status = "✅" if rank_norm in ("staff", "owner", "admin") else "❌"

    embed = discord.Embed(
        title=f"{robloxuser}'s Information!",
        color=discord.Color.orange(),
    )
    embed.add_field(name="Last Update", value=updated, inline=False)

    embed.add_field(name="Team", value=team_name, inline=True)
    embed.add_field(name="Division", value=division, inline=True)
    embed.add_field(name="Suspended", value=suspended, inline=True)

    embed.add_field(name="Manager", value=manager_status, inline=True)
    embed.add_field(name="Owner", value=owner_status, inline=True)
    embed.add_field(name="Staff", value=staff_status, inline=True)

    # Only show Team Owner ID if they are Manager or Owner (and not Free Agent)
    if team_name.lower() != FREE_AGENT_TEAM.lower() and rank_norm in ("manager", "owner"):
        embed.add_field(name="Team Owner ID", value=str(team_owner_id), inline=False)

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
































