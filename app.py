import discord
from discord import app_commands
from discord.ext import commands
import sqlite3
import aiohttp
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from threading import Thread
from http.server import SimpleHTTPRequestHandler, HTTPServer

# ==========================================
# CONFIGURATION & PERSISTENCE
# ==========================================
DB_FILE = "bot_data.db"
OWNER_ID = 1483537009963434006  # Bot Owner's Discord ID

def init_db():
    """Initializes the database schema if it does not exist."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Configuration table for channels and welcome messages
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS config (
            guild_id INTEGER PRIMARY KEY,
            one_day_channel_id INTEGER,
            custom_day_channel_id INTEGER,
            one_day_msg_id INTEGER,
            custom_day_msg_id INTEGER
        )
    """)
    
    # Resellers table for unlimited additions
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS resellers (
            user_id INTEGER PRIMARY KEY
        )
    """)
    
    # Cooldowns table to track user additions (24h cooldown)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_cooldowns (
            user_id INTEGER PRIMARY KEY,
            last_added_timestamp TEXT
        )
    """)
    
    # Authorized guilds list
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS authorized_guilds (
            guild_id INTEGER PRIMARY KEY
        )
    """)
    
    conn.commit()
    conn.close()

# Database helper functions
def get_config(guild_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT one_day_channel_id, custom_day_channel_id, one_day_msg_id, custom_day_msg_id FROM config WHERE guild_id = ?", (guild_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            "one_day_channel_id": row[0],
            "custom_day_channel_id": row[1],
            "one_day_msg_id": row[2],
            "custom_day_msg_id": row[3]
        }
    return None

def set_config(guild_id: int, one_day_channel_id: int, custom_day_channel_id: int, one_day_msg_id: int = 0, custom_day_msg_id: int = 0):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO config (guild_id, one_day_channel_id, custom_day_channel_id, one_day_msg_id, custom_day_msg_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
            one_day_channel_id = excluded.one_day_channel_id,
            custom_day_channel_id = excluded.custom_day_channel_id,
            one_day_msg_id = excluded.one_day_msg_id,
            custom_day_msg_id = excluded.custom_day_msg_id
    """, (guild_id, one_day_channel_id, custom_day_channel_id, one_day_msg_id, custom_day_msg_id))
    conn.commit()
    conn.close()

def update_config_msg_ids(guild_id: int, one_day_msg_id: int, custom_day_msg_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE config
        SET one_day_msg_id = ?, custom_day_msg_id = ?
        WHERE guild_id = ?
    """, (one_day_msg_id, custom_day_msg_id, guild_id))
    conn.commit()
    conn.close()

def is_reseller(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM resellers WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def add_reseller(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO resellers (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def remove_reseller(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM resellers WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def get_resellers_list():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM resellers")
    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows]

def get_cooldown(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT last_added_timestamp FROM user_cooldowns WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def set_cooldown(user_id: int, timestamp_str: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO user_cooldowns (user_id, last_added_timestamp)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            last_added_timestamp = excluded.last_added_timestamp
    """, (user_id, timestamp_str))
    conn.commit()
    conn.close()

def is_guild_authorized(guild_id: int) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM authorized_guilds WHERE guild_id = ?", (guild_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def authorize_guild_db(guild_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO authorized_guilds (guild_id) VALUES (?)", (guild_id,))
    conn.commit()
    conn.close()

def unauthorize_guild_db(guild_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM authorized_guilds WHERE guild_id = ?", (guild_id,))
    conn.commit()
    conn.close()

async def check_guild_authorized_async(guild: discord.Guild) -> bool:
    """Verifies if the guild is authorized (owned by owner, owner present, or explicitly authorized)."""
    if not guild:
        return False
    # Auto-authorize if owned by the bot owner
    if guild.owner_id == OWNER_ID:
        return True
    # Auto-authorize if owner is present in member list
    if guild.get_member(OWNER_ID) is not None:
        return True
    try:
        # Check API just in case cache is incomplete
        member = await guild.fetch_member(OWNER_ID)
        if member is not None:
            return True
    except discord.HTTPException:
        pass
    # Otherwise check database
    return is_guild_authorized(guild.id)

# ==========================================
# DISCORD BOT INITIALIZATION
# ==========================================
class UIDAdderBot(commands.Bot):
    def __init__(self):
        # Enable default intents and member cache intents
        intents = discord.Intents.default()
        intents.members = True
        intents.guilds = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Initialize SQLite tables
        init_db()
        print("Database schema verified.")

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        
        # Sync global slash commands
        try:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} slash command(s) globally.")
        except Exception as e:
            print(f"Failed to sync slash commands: {e}")
            
        # Verify guilds: leave any unauthorized guilds
        for guild in list(self.guilds):
            if not await check_guild_authorized_async(guild):
                print(f"Leaving unauthorized guild: {guild.name} (ID: {guild.id})")
                await guild.leave()
            else:
                # Restore channels banner message if it got deleted
                await restore_channel_messages(guild)

    async def on_guild_join(self, guild: discord.Guild):
        # Leave immediately if the guild is unauthorized
        if not await check_guild_authorized_async(guild):
            print(f"Joined unauthorized guild: {guild.name} (ID: {guild.id}). Leaving immediately...")
            await guild.leave()
        else:
            print(f"Joined authorized guild: {guild.name} (ID: {guild.id})")

bot = UIDAdderBot()

# ==========================================
# SETUP MESSAGES AUTO-RESTORE
# ==========================================
async def restore_channel_messages(guild: discord.Guild):
    """Checks if configured channel setup messages exist; sends them if missing."""
    cfg = get_config(guild.id)
    if not cfg:
        return
        
    one_day_chan_id = cfg["one_day_channel_id"]
    custom_chan_id = cfg["custom_day_channel_id"]
    one_day_msg_id = cfg["one_day_msg_id"]
    custom_msg_id = cfg["custom_day_msg_id"]
    
    new_one_day_msg_id = one_day_msg_id
    new_custom_msg_id = custom_msg_id
    updated = False
    
    # 1. Verify and restore One Day Channel message
    one_day_chan = guild.get_channel(one_day_chan_id)
    if one_day_chan:
        msg_exists = False
        if one_day_msg_id:
            try:
                await one_day_chan.fetch_message(one_day_msg_id)
                msg_exists = True
            except (discord.NotFound, discord.HTTPException):
                pass
                
        if not msg_exists:
            embed = discord.Embed(
                title="⚡ Free 1-Day UID Adder",
                description=(
                    "Add your game UID to the bypass system for **24 hours**.\n\n"
                    "**Rules & Guidelines:**\n"
                    "• Standard users can add **1 UID every 24 hours**.\n"
                    "• The duration is fixed to **1 Day** (no selection required).\n\n"
                    "**How to Use:**\n"
                    "Type `/add` and enter your game UID. Press Enter to submit."
                ),
                color=0x00FF87
            )
            embed.set_footer(text="Powered by Syntax Corporation")
            try:
                # Clear chat history in this channel before posting setup message to keep it clean
                try:
                    await one_day_chan.purge(limit=10)
                except Exception:
                    pass
                msg = await one_day_chan.send(embed=embed)
                new_one_day_msg_id = msg.id
                updated = True
            except Exception as e:
                print(f"Failed to restore message in One Day Channel: {e}")
                
    # 2. Verify and restore Custom Day Channel message
    custom_chan = guild.get_channel(custom_chan_id)
    if custom_chan:
        msg_exists = False
        if custom_msg_id:
            try:
                await custom_chan.fetch_message(custom_msg_id)
                msg_exists = True
            except (discord.NotFound, discord.HTTPException):
                pass
                
        if not msg_exists:
            embed = discord.Embed(
                title="✨ Custom Duration UID Adder",
                description=(
                    "Add your game UID to the bypass system with **custom duration**.\n\n"
                    "**Rules & Guidelines:**\n"
                    "• Standard users can select up to **30 days** (default is 30).\n"
                    "• Resellers can select up to **365 days**.\n\n"
                    "**How to Use:**\n"
                    "Type `/add`, enter your game UID, and optionally specify the `days` parameter."
                ),
                color=0x8A2BE2
            )
            embed.set_footer(text="Powered by Syntax Corporation")
            try:
                # Clear chat history in this channel
                try:
                    await custom_chan.purge(limit=10)
                except Exception:
                    pass
                msg = await custom_chan.send(embed=embed)
                new_custom_msg_id = msg.id
                updated = True
            except Exception as e:
                print(f"Failed to restore message in Custom Day Channel: {e}")
                
    if updated:
        update_config_msg_ids(guild.id, new_one_day_msg_id, new_custom_msg_id)

# ==========================================
# CUSTOM CHECKS
# ==========================================
def is_bot_owner():
    """Decorator check to restrict slash command only to the bot owner."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == OWNER_ID:
            return True
        raise app_commands.errors.CheckFailure("You do not have permission to use this command.")
    return app_commands.check(predicate)

# ==========================================
# OWNER COMMANDS (Slash Commands)
# ==========================================

# 1. Channels setup command
@bot.tree.command(name="setup_channels", description="[Owner Only] Configure the channels for 1-day and custom-day additions")
@app_commands.describe(
    one_day_channel="The channel designated for 1-day UID additions",
    custom_day_channel="The channel designated for custom duration UID additions"
)
@is_bot_owner()
async def setup_channels(interaction: discord.Interaction, one_day_channel: discord.TextChannel, custom_day_channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    
    # Save the configs with dummy message IDs
    set_config(interaction.guild_id, one_day_channel.id, custom_day_channel.id, 0, 0)
    
    # Force restore (which sends the setup messages and updates DB)
    await restore_channel_messages(interaction.guild)
    
    embed = discord.Embed(
        title="⚙️ Channels Configured",
        description=(
            f"Successfully set up the channels:\n"
            f"- **1-Day Channel**: {one_day_channel.mention}\n"
            f"- **Custom-Day Channel**: {custom_day_channel.mention}\n\n"
            "Welcome banner messages have been initialized in these channels."
        ),
        color=0x00FF87
    )
    await interaction.followup.send(embed=embed, ephemeral=True)

# 2. Reseller management command group
class ResellerGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="reseller", description="Manage resellers who can add unlimited UIDs (Owner Only)")

reseller_group = ResellerGroup()

@reseller_group.command(name="add", description="Add a user as a reseller")
@app_commands.describe(user="The Discord user to set as reseller")
@is_bot_owner()
async def reseller_add(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    add_reseller(user.id)
    
    embed = discord.Embed(
        title="⭐ Reseller Added",
        description=f"User {user.mention} (`{user.id}`) is now a reseller and has **unlimited** UID additions.",
        color=0x8A2BE2
    )
    await interaction.followup.send(embed=embed, ephemeral=True)

@reseller_group.command(name="remove", description="Remove a user's reseller status")
@app_commands.describe(user="The Discord user to remove from resellers")
@is_bot_owner()
async def reseller_remove(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    remove_reseller(user.id)
    
    embed = discord.Embed(
        title="🚫 Reseller Removed",
        description=f"User {user.mention} (`{user.id}`) has been removed from resellers and is now subject to standard limits.",
        color=0xFF3366
    )
    await interaction.followup.send(embed=embed, ephemeral=True)

@reseller_group.command(name="list", description="List all configured resellers")
@is_bot_owner()
async def reseller_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    resellers = get_resellers_list()
    
    if not resellers:
        desc = "No resellers configured yet."
    else:
        desc = "\n".join([f"- <@{uid}> (`{uid}`)" for uid in resellers])
        
    embed = discord.Embed(
        title="⭐ Resellers List",
        description=desc,
        color=0x8A2BE2
    )
    await interaction.followup.send(embed=embed, ephemeral=True)

bot.tree.add_command(reseller_group)

# 3. Guild authorization management
@bot.tree.command(name="authorize_guild", description="[Owner Only] Authorize a guild to use the bot")
@app_commands.describe(guild_id="The ID of the Discord guild to authorize")
@is_bot_owner()
async def authorize_guild(interaction: discord.Interaction, guild_id: str):
    await interaction.response.defer(ephemeral=True)
    try:
        g_id = int(guild_id.strip())
    except ValueError:
        await interaction.followup.send("Invalid Guild ID. It must be a numeric value.", ephemeral=True)
        return
        
    authorize_guild_db(g_id)
    
    embed = discord.Embed(
        title="✅ Guild Authorized",
        description=f"Guild ID `{g_id}` has been authorized to use the bot.",
        color=0x00FF87
    )
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="unauthorize_guild", description="[Owner Only] Revoke a guild's authorization")
@app_commands.describe(guild_id="The ID of the Discord guild to unauthorize")
@is_bot_owner()
async def unauthorize_guild(interaction: discord.Interaction, guild_id: str):
    await interaction.response.defer(ephemeral=True)
    try:
        g_id = int(guild_id.strip())
    except ValueError:
        await interaction.followup.send("Invalid Guild ID. It must be a numeric value.", ephemeral=True)
        return
        
    unauthorize_guild_db(g_id)
    
    # If the bot is currently in that guild, leave it
    target_guild = bot.get_guild(g_id)
    left_msg = ""
    if target_guild:
        try:
            await target_guild.leave()
            left_msg = "\n*Successfully left the guild.*"
        except Exception as e:
            left_msg = f"\n*Could not auto-leave guild: {e}*"
            
    embed = discord.Embed(
        title="🚫 Guild Revoked",
        description=f"Guild ID `{g_id}` authorization has been revoked.{left_msg}",
        color=0xFF3366
    )
    await interaction.followup.send(embed=embed, ephemeral=True)

# ==========================================
# PUBLIC COMMAND: ADD UID
# ==========================================
@bot.tree.command(name="add", description="Add a game UID to the bypass system")
@app_commands.describe(
    uid="The numeric UID to add",
    days="Number of days (default 30 for custom channel, ignored for 1-day channel)"
)
async def add_uid(interaction: discord.Interaction, uid: str, days: int = None):
    # 1. Defer immediately to avoid "Application did not respond" (ephemeral = True to protect user details and avoid spam)
    await interaction.response.defer(ephemeral=True)
    
    # 2. Check guild authorization
    if not await check_guild_authorized_async(interaction.guild):
        await interaction.followup.send("❌ This server is not authorized to use this bot. Leaving...", ephemeral=True)
        await interaction.guild.leave()
        return

    # 3. Check if channels are configured
    cfg = get_config(interaction.guild_id)
    if not cfg:
        embed = discord.Embed(
            title="⚠️ Configuration Required",
            description="The channels for this bot have not been configured yet.\nAsk the bot owner to run `/setup_channels`.",
            color=0xFFD700
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
        
    one_day_chan = cfg["one_day_channel_id"]
    custom_day_chan = cfg["custom_day_channel_id"]
    
    # Check if executed in one of the configured channels
    current_chan_id = interaction.channel_id
    if current_chan_id != one_day_chan and current_chan_id != custom_day_chan:
        embed = discord.Embed(
            title="❌ Invalid Channel",
            description=f"This command can only be used in:\n- <#{one_day_chan}> (1-Day Channel)\n- <#{custom_day_chan}> (Custom-Day Channel)",
            color=0xFF3366
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
        
    # 4. Input validation (Protection / Anti-bypass)
    uid_clean = uid.strip()
    if not uid_clean.isdigit():
        embed = discord.Embed(
            title="❌ Invalid UID Format",
            description="The UID must be numeric only.",
            color=0xFF3366
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
        
    if len(uid_clean) < 5 or len(uid_clean) > 15:
        embed = discord.Embed(
            title="❌ Invalid UID Length",
            description="The UID length must be between 5 and 15 digits.",
            color=0xFF3366
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    # 5. Cooldown Check / Reseller check
    user_id = interaction.user.id
    reseller_status = is_reseller(user_id)
    
    if not reseller_status:
        # Check 24-hour limit
        last_added = get_cooldown(user_id)
        if last_added:
            last_time = datetime.fromisoformat(last_added)
            now = datetime.now(timezone.utc)
            time_diff = now - last_time
            if time_diff < timedelta(hours=24):
                remaining = timedelta(hours=24) - time_diff
                hours, remainder = divmod(int(remaining.total_seconds()), 3600)
                minutes, seconds = divmod(remainder, 60)
                
                embed = discord.Embed(
                    title="⏳ Cooldown Active",
                    description=(
                        f"You can only add 1 UID every 24 hours.\n"
                        f"Your cooldown expires in **{hours}h {minutes}m {seconds}s**."
                    ),
                    color=0xFFD700
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

    # 6. Determine final duration days
    if current_chan_id == one_day_chan:
        final_days = 1
    else:
        # Custom day channel
        if days is None:
            final_days = 30
        else:
            if reseller_status:
                # Resellers can add for up to 365 days
                if days < 1 or days > 365:
                    embed = discord.Embed(
                        title="❌ Invalid Duration",
                        description="As a reseller, you can select a duration between 1 and 365 days.",
                        color=0xFF3366
                    )
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    return
                final_days = days
            else:
                # Normal users are restricted to 1-30 days
                if days < 1 or days > 30:
                    embed = discord.Embed(
                        title="❌ Invalid Duration",
                        description="You can only select a duration between 1 and 30 days.",
                        color=0xFF3366
                    )
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    return
                final_days = days

    # 7. Call the external API
    url = f"https://uid.syntaxcorporation.online/uid?add={uid_clean}&days={final_days}"
    headers = {"User-Agent": "Mozilla/5.0"}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                status = resp.status
                text = await resp.text()
                
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    data = None
                    
                if status == 200 and data and data.get("success"):
                    # Success response from API
                    
                    # Update user cooldown only if they are not reseller
                    if not reseller_status:
                        set_cooldown(user_id, datetime.now(timezone.utc).isoformat())
                        
                    embed = discord.Embed(
                        title="✅ UID Added Successfully",
                        color=0x00FF87
                    )
                    embed.add_field(name="👤 User ID", value=interaction.user.mention, inline=True)
                    embed.add_field(name="🆔 Added UID", value=f"`{uid_clean}`", inline=True)
                    embed.add_field(name="⏳ Duration", value=f"`{final_days} {'Day' if final_days == 1 else 'Days'}`", inline=True)
                    
                    if reseller_status:
                        embed.add_field(name="⭐ Reseller Mode", value="Unlimited (No Cooldown)", inline=False)
                        
                    embed.set_footer(text="Powered by Syntax Corporation")
                    embed.timestamp = discord.utils.utcnow()
                    
                    # Send public response to show success in the channel
                    await interaction.followup.send(embed=embed, ephemeral=False)
                else:
                    # Failure response from API
                    msg = data.get("message") if data else "The API server returned an error."
                    if not msg and data:
                        msg = data.get("reason", "Internal Server Error")
                        
                    embed = discord.Embed(
                        title="❌ Add Failed",
                        description=f"API Error Message: **{msg}**",
                        color=0xFF3366
                    )
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    
    except Exception as e:
        embed = discord.Embed(
            title="❌ Connection Error",
            description=f"Could not connect to the API server: `{str(e)}`",
            color=0xFF3366
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

# ==========================================
# ERROR HANDLERS
# ==========================================
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.errors.CheckFailure):
        # Gracefully handle the permission check failures
        try:
            await interaction.response.send_message("❌ You do not have permission to run this command.", ephemeral=True)
        except discord.InteractionResponded:
            await interaction.followup.send("❌ You do not have permission to run this command.", ephemeral=True)
        except Exception:
            pass
    else:
        print(f"Application command error occurred: {error}", file=sys.stderr)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ An unexpected error occurred.", ephemeral=True)
            else:
                await interaction.followup.send("❌ An unexpected error occurred.", ephemeral=True)
        except Exception:
            pass

# ==========================================
# KEEP ALIVE WEB SERVER (For Free Hosting)
# ==========================================
class KeepAliveHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(b"Discord Bot is online and running!")

    # Override log_message to prevent terminal spam
    def log_message(self, format, *args):
        pass

def run_keep_alive_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), KeepAliveHandler)
    print(f"Starting keep-alive server on port {port}...")
    server.serve_forever()

def keep_alive():
    t = Thread(target=run_keep_alive_server)
    t.daemon = True
    t.start()

# ==========================================
# ENTRY POINT
# ==========================================
def main():
    token = None
    
    # 1. Try Environment Variable
    if "DISCORD_BOT_TOKEN" in os.environ:
        token = os.environ["DISCORD_BOT_TOKEN"]
        
    # 2. Try config.json
    if not token and os.path.exists("config.json"):
        try:
            with open("config.json", "r") as f:
                data = json.load(f)
                token = data.get("token")
        except Exception as e:
            print(f"Error loading config.json: {e}")
            
    # Check if token is valid / loaded
    if not token or token == "MTQ0ODM0NzY3NTQ2MzM4OTI3NA.GdVv0y.lnhGpd71Raok1zuIe0zurJ9rFTDNVk2Ku3H8ks":
        print("[ERROR] Discord Bot Token not found!")
        print("Please set the DISCORD_BOT_TOKEN environment variable or fill the 'token' field in config.json")
        sys.exit(1)
        
    # Start Keep Alive Server (useful for free hosts like Render, Koyeb, Replit)
    keep_alive()
    
    bot.run(token)

if __name__ == "__main__":
    main()

