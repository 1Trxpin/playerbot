import os
import datetime as dt
import discord
from discord import app_commands
from discord.ext import commands
import asyncpg
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")

ALLOWED_IDS = set(
    int(x.strip())
    for x in os.getenv("ALLOWED_IDS", "").split(",")
    if x.strip().isdigit()
)

INTENTS = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=INTENTS)

pool: asyncpg.Pool | None = None


# ---------- Helpers ----------

def is_authorized(interaction: discord.Interaction) -> bool:
    if interaction.user.id in ALLOWED_IDS:
        return True
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
    return (
        "https://www.roblox.com/asset-thumbnail/image"
        f"?assetId={asset_id}&width=420&height=420&format=png"
    )


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
                team_name TEXT NOT NULL REFERENCES teams(name),
                rank TEXT,
                updated_at TEXT NOT NULL
            );
        """)


# ---------- Ready ----------

@bot.event
async def on_ready():
    global pool

    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN missing.")

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL missing.")

    pool = await asyncpg.create_pool(DATABASE_URL)
    await init_db()

    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"✅ Synced commands to guild {GUILD_ID}")

    print(f"✅ Logged in as {bot.user}")


# ---------- Commands ----------

@bot.tree.command(name="setteam", description="Create or update a team.")
@app_commands.describe(
    team="Team name",
    owner="Owner Roblox username",
    manager="Manager Roblox username",
    logo_asset_id="Roblox image asset id"
)
@require_auth()
async def setteam(
    interaction: discord.Interaction,
    team: str,
    owner: str,
    manager: str | None = None,
    logo_asset_id: int | None = None,
):
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

    embed = discord.Embed(title="Team Saved", color=discord.Color.green())
    embed.add_field(name="Team", value=team)
    embed.add_field(name="Owner", value=owner)
    embed.add_field(name="Manager", value=manager or "None")
    if logo_asset_id:
        embed.set_thumbnail(url=rbxthumb_asset(logo_asset_id))

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="rankplayer", description="Add or update a player.")
@app_commands.describe(
    robloxuser="Roblox username",
    team="Team name",
    rank="Rank"
)
@require_auth()
async def rankplayer(
    interaction: discord.Interaction,
    robloxuser: str,
    team: str,
    rank: str,
):
    now = dt.datetime.utcnow().isoformat()

    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM teams WHERE name=$1", team)
        if not exists:
            return await interaction.response.send_message(
                "❌ Team does not exist.", ephemeral=True
            )

        await conn.execute(
            """
            INSERT INTO players (roblox_user, team_name, rank, updated_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (roblox_user) DO UPDATE SET
                team_name=$2, rank=$3, updated_at=$4;
            """,
            robloxuser, team, rank, now
        )

    await interaction.response.send_message(
        f"✅ **{robloxuser}** ranked **{rank}** in **{team}**."
    )


# ---------- TEAM VIEW ----------

@bot.tree.command(name="teamview", description="View team info.")
@app_commands.describe(teamname="Team name")
@require_auth()
async def teamview(interaction: discord.Interaction, teamname: str):
    async with pool.acquire() as conn:
        team = await conn.fetchrow(
            "SELECT * FROM teams WHERE name=$1", teamname
        )

        if not team:
            return await interaction.response.send_message(
                "❌ Team not found.", ephemeral=True
            )

        players = await conn.fetch(
            "SELECT roblox_user, rank FROM players WHERE team_name=$1 ORDER BY roblox_user",
            teamname
        )

    embed = discord.Embed(
        title=f"{teamname} ({len(players)} Players)",
        color=discord.Color.blue(),
    )

    embed.add_field(name="Owner", value=team["owner_roblox"], inline=False)
    embed.add_field(name="Manager", value=team["manager_roblox"] or "None", inline=False)

    if team["logo_asset_id"]:
        embed.set_thumbnail(url=rbxthumb_asset(team["logo_asset_id"]))

    if players:
        embed.add_field(
            name="Players",
            value="\n".join(f"{p['roblox_user']} ({p['rank']})" for p in players),
            inline=False,
        )
    else:
        embed.add_field(name="Players", value="None", inline=False)

    await interaction.response.send_message(embed=embed)


# ---------- AUTOCOMPLETE (THIS IS THE NEW PART) ----------

@teamview.autocomplete("teamname")
async def teamname_autocomplete(interaction: discord.Interaction, current: str):
    current = (current or "").lower()

    async with pool.acquire() as conn:
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

    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]


# ---------- PLAYER INFO ----------

@bot.tree.command(name="playerinfo", description="View player info.")
@app_commands.describe(robloxuser="Roblox username")
@require_auth()
async def playerinfo(interaction: discord.Interaction, robloxuser: str):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT p.*, t.owner_roblox, t.manager_roblox, t.logo_asset_id
            FROM players p
            LEFT JOIN teams t ON p.team_name = t.name
            WHERE roblox_user=$1
            """,
            robloxuser,
        )

    if not row:
        return await interaction.response.send_message(
            "❌ Player not found.", ephemeral=True
        )

    embed = discord.Embed(title=f"{robloxuser}'s Info", color=discord.Color.orange())
    embed.add_field(name="Team", value=row["team_name"])
    embed.add_field(name="Rank", value=row["rank"])
    embed.add_field(name="Owner", value=row["owner_roblox"])
    embed.add_field(name="Manager", value=row["manager_roblox"] or "None")

    if row["logo_asset_id"]:
        embed.set_thumbnail(url=rbxthumb_asset(row["logo_asset_id"]))

    await interaction.response.send_message(embed=embed)


# ---------- RUN ----------

bot.run(TOKEN)





