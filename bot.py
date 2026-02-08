# bot.py
# Discord slash-command bot for Roblox team/player registration + ranking
# - Access restricted to whitelisted Discord IDs (and optionally server admins)
# - Stores team owner/manager + Roblox logo ASSET ID
# - Commands: /setteam /rankplayer /teamview /playerinfo

import os
import datetime as dt
import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

# Comma-separated Discord user IDs allowed to use commands
ALLOWED_IDS = set(
    int(x.strip())
    for x in os.getenv("ALLOWED_IDS", "").split(",")
    if x.strip().isdigit()
)

DB_PATH = "leaderboard.db"

INTENTS = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=INTENTS)


# ---------- Helpers ----------

def is_authorized(interaction: discord.Interaction) -> bool:
    # Whitelist by Discord ID
    if interaction.user.id in ALLOWED_IDS:
        return True
    # Optional: allow server admins too
    if interaction.user.guild_permissions.administrator:
        return True
    return False


def require_auth():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not is_authorized(interaction):
            msg = "❌ You are not authorized to use this bot."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return False
        return True

    return app_commands.check(predicate)


def rbxthumb_asset(asset_id: int) -> str:
    # Direct image URL Discord can display
    return (
        "https://www.roblox.com/asset-thumbnail/image"
        f"?assetId={asset_id}&width=420&height=420&format=png"
    )


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS teams (
                name TEXT PRIMARY KEY,
                owner_roblox TEXT NOT NULL,
                manager_roblox TEXT,
                logo_asset_id INTEGER
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS players (
                roblox_user TEXT PRIMARY KEY,
                team_name TEXT NOT NULL,
                rank TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(team_name) REFERENCES teams(name)
            )
            """
        )
        await db.commit()


@bot.event
async def on_ready():
    await init_db()

    # Sync slash commands to your server (fast updates while developing)
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"✅ Synced slash commands to guild {GUILD_ID}")

    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")


# ---------- Slash Commands ----------

@bot.tree.command(name="setteam", description="Create/update a team’s owner/manager/logo asset id.")
@app_commands.describe(
    team="Team name",
    owner="Owner Roblox username",
    manager="Manager Roblox username (optional)",
    logo_asset_id="Roblox image asset id (optional)"
)
@require_auth()
async def setteam(
    interaction: discord.Interaction,
    team: str,
    owner: str,
    manager: str | None = None,
    logo_asset_id: int | None = None,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO teams (name, owner_roblox, manager_roblox, logo_asset_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                owner_roblox=excluded.owner_roblox,
                manager_roblox=excluded.manager_roblox,
                logo_asset_id=excluded.logo_asset_id
            """,
            (team, owner, manager, logo_asset_id),
        )
        await db.commit()

    embed = discord.Embed(
        title="Team Updated",
        description=f"✅ **{team}** saved.",
        color=discord.Color.green(),
    )
    embed.add_field(name="Owner", value=owner, inline=True)
    embed.add_field(name="Manager", value=manager or "None", inline=True)
    embed.add_field(name="Logo Asset ID", value=str(logo_asset_id) if logo_asset_id else "None", inline=False)
    if logo_asset_id:
        embed.set_thumbnail(url=rbxthumb_asset(int(logo_asset_id)))

    await interaction.response.send_message(embed=embed)


# ✅ UPDATED: /rankplayer now assigns player to team AND rank, and requires the team exists.
@bot.tree.command(name="rankplayer", description="Add or update a player’s team and rank.")
@app_commands.describe(
    robloxuser="Roblox username",
    team="Team name to assign",
    rank="Rank inside the team (Owner, Manager, Player, etc.)"
)
@require_auth()
async def rankplayer(
    interaction: discord.Interaction,
    robloxuser: str,
    team: str,
    rank: str,
):
    now = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"

    async with aiosqlite.connect(DB_PATH) as db:
        # Require team to exist (prevents typos creating new teams)
        cur = await db.execute("SELECT name FROM teams WHERE name = ?", (team,))
        team_exists = await cur.fetchone()
        if not team_exists:
            return await interaction.response.send_message(
                f"❌ Team **{team}** does not exist. Create it first with `/setteam`.",
                ephemeral=True
            )

        # Check previous team (so embed can say added vs moved)
        cur = await db.execute("SELECT team_name FROM players WHERE roblox_user = ?", (robloxuser,))
        old = await cur.fetchone()
        old_team = old[0] if old else None

        # Upsert player
        await db.execute(
            """
            INSERT INTO players (roblox_user, team_name, rank, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(roblox_user) DO UPDATE SET
                team_name=excluded.team_name,
                rank=excluded.rank,
                updated_at=excluded.updated_at
            """,
            (robloxuser, team, rank, now),
        )
        await db.commit()

        # Get team logo for embed thumbnail
        cur = await db.execute("SELECT logo_asset_id FROM teams WHERE name = ?", (team,))
        row = await cur.fetchone()
        logo_asset_id = row[0] if row else None

    if old_team and old_team.lower() != team.lower():
        desc = f"🔄 **{robloxuser}** moved from **{old_team}** to **{team}** as **{rank}**"
    elif old_team:
        desc = f"✅ **{robloxuser}** updated in **{team}** as **{rank}**"
    else:
        desc = f"✅ **{robloxuser}** added to **{team}** as **{rank}**"

    embed = discord.Embed(
        title="Player Ranked",
        description=desc,
        color=discord.Color.green(),
    )
    embed.add_field(name="Rank", value=rank, inline=True)
    embed.add_field(name="Updated", value=now, inline=False)

    if logo_asset_id:
        embed.set_thumbnail(url=rbxthumb_asset(int(logo_asset_id)))

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="teamview", description="View a team’s owner/manager/players.")
@app_commands.describe(teamname="Team name")
@require_auth()
async def teamview(interaction: discord.Interaction, teamname: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT owner_roblox, manager_roblox, logo_asset_id FROM teams WHERE name = ?",
            (teamname,),
        )
        team_row = await cur.fetchone()

        if not team_row:
            return await interaction.response.send_message("❌ Team not found.", ephemeral=True)

        owner_roblox, manager_roblox, logo_asset_id = team_row

        cur = await db.execute(
            "SELECT roblox_user, rank FROM players WHERE team_name = ? ORDER BY roblox_user COLLATE NOCASE",
            (teamname,),
        )
        players = await cur.fetchall()

    embed = discord.Embed(
        title=f"Information for {teamname} ({len(players)} Players)",
        color=discord.Color.dark_teal(),
    )
    embed.add_field(name="Owner", value=owner_roblox, inline=False)
    embed.add_field(name="Manager", value=manager_roblox or "None", inline=False)

    if logo_asset_id:
        embed.set_thumbnail(url=rbxthumb_asset(int(logo_asset_id)))
        embed.add_field(name="Logo Asset ID", value=str(logo_asset_id), inline=False)

    if not players:
        embed.add_field(name="Players", value="None", inline=False)
    else:
        # Format: Username (Rank)
        lines = [f"{u} ({r or 'None'})" for (u, r) in players]

        # Chunk to fit embed limits
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
@require_auth()
async def playerinfo(interaction: discord.Interaction, robloxuser: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT p.team_name, p.rank, p.updated_at,
                   t.logo_asset_id, t.owner_roblox, t.manager_roblox
            FROM players p
            LEFT JOIN teams t ON t.name = p.team_name
            WHERE p.roblox_user = ?
            """,
            (robloxuser,),
        )
        row = await cur.fetchone()

    if not row:
        return await interaction.response.send_message("❌ Player not found.", ephemeral=True)

    team_name, rank, updated_at, logo_asset_id, owner_roblox, manager_roblox = row

    embed = discord.Embed(
        title=f"{robloxuser}'s Information!",
        color=discord.Color.orange(),
    )
    embed.add_field(name="Team", value=team_name, inline=True)
    embed.add_field(name="Rank", value=rank or "None", inline=True)
    embed.add_field(name="Last Update", value=updated_at, inline=False)

    embed.add_field(name="Team Owner", value=owner_roblox or "Unknown", inline=True)
    embed.add_field(name="Team Manager", value=manager_roblox or "None", inline=True)

    if logo_asset_id:
        embed.set_thumbnail(url=rbxthumb_asset(int(logo_asset_id)))
        embed.add_field(name="Logo Asset ID", value=str(logo_asset_id), inline=False)

    await interaction.response.send_message(embed=embed)


# --- Start ---
if not TOKEN:
    # Print what Railway is actually seeing (safe: doesn't print the token)
    print("ENV CHECK: DISCORD_TOKEN is missing or empty.")
    print("ENV CHECK: Available keys include:", ", ".join(sorted(list(os.environ.keys()))[:30]), "...")
    raise RuntimeError("DISCORD_TOKEN is missing. Add it in Railway -> Service -> Variables.")

bot.run(TOKEN)



