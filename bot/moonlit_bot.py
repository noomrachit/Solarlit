import os
import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from aiohttp import web

import database as db

BANGKOK_TZ = ZoneInfo("Asia/Bangkok")

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("moonlit-bot")

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN is required")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# Helpers
async def get_settings(guild_id: int) -> dict:
    pool = await db.get_pool()
    row = await pool.fetchrow("SELECT * FROM settings WHERE guild_id = $1", guild_id)
    if not row:
        await pool.execute("INSERT INTO settings (guild_id) VALUES ($1) ON CONFLICT DO NOTHING", guild_id)
        row = await pool.fetchrow("SELECT * FROM settings WHERE guild_id = $1", guild_id)
    return dict(row) if row else {}

async def send_log(guild: discord.Guild, embed: discord.Embed):
    settings = await get_settings(guild.id)
    channel_id = settings.get("log_channel")
    if channel_id:
        channel = guild.get_channel(channel_id)
        if channel:
            try:
                await channel.send(embed=embed)
            except Exception:
                pass

def has_mod_perms():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False

        # ดึง Member จาก guild โดยตรง เพื่อหลีกเลี่ยง NoneType
        member = interaction.guild.get_member(interaction.user.id)
        if member is None:
            # ถ้าหา Member ไม่เจอใน cache ให้ลองใช้ interaction.user ถ้าเป็น Member
            if isinstance(interaction.user, discord.Member):
                member = interaction.user
            else:
                return False

        try:
            perms = member.guild_permissions
        except AttributeError:
            return False

        return (
            perms.kick_members
            or perms.ban_members
            or perms.moderate_members
            or perms.manage_messages
            or perms.administrator
        )
    return app_commands.check(predicate)

# ─────────────────────────────────────────────
# Automatic Error Handling
# ─────────────────────────────────────────────

@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandInvokeError):
        error = error.original
    if isinstance(error, app_commands.CheckFailure):
        msg = "❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้"
    elif isinstance(error, app_commands.MissingPermissions):
        missing = ", ".join(error.missing_permissions)
        msg = f"❌ คุณขาดสิทธิ์: `{missing}`"
    elif isinstance(error, app_commands.BotMissingPermissions):
        missing = ", ".join(error.missing_permissions)
        msg = f"❌ บอทขาดสิทธิ์: `{missing}` — กรุณาให้สิทธิ์บอทใน Server Settings"
    elif isinstance(error, app_commands.CommandOnCooldown):
        msg = f"⏳ คำสั่งติดคูลดาวน์ รออีก {error.retry_after:.1f} วินาที"
    elif isinstance(error, app_commands.TransformerError):
        msg = f"❌ ค่าที่ใส่ไม่ถูกต้อง: {error}"
    elif isinstance(error, discord.Forbidden):
        msg = "❌ บอทไม่มีสิทธิ์ทำรายการนี้ (ตรวจ Role Hierarchy / Channel Permissions)"
    elif isinstance(error, discord.NotFound):
        msg = "❌ ไม่พบเป้าหมาย (สมาชิก/ช่อง/ข้อความถูกลบไปแล้ว)"
    elif isinstance(error, discord.HTTPException):
        msg = f"❌ Discord API Error: {error.status} — {error.text or 'Unknown'}"
    else:
        log.exception(f"Unhandled command error in /{getattr(interaction.command, 'qualified_name', '?')}: {error}")
        msg = "❌ เกิดข้อผิดพลาดภายใน ทีมงานได้รับแจ้งแล้ว"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass

@bot.event
async def on_error(event: str, *args, **kwargs):
    log.exception(f"Unhandled error in event `{event}`")

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    log.exception(f"Prefix command error: {error}")

# Events
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        await db.init_db()
    except Exception as e:
        log.error(f"DB init failed: {e}")
    try:
        synced = await tree.sync()
        log.info(f"Synced {len(synced)} commands")
    except Exception as e:
        log.error(f"Sync failed: {e}")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="SoLARLIT | /help"))
    asyncio.create_task(start_health_server())
    if not check_bookings.is_running():
        check_bookings.start()
    try:
        await refresh_all_dashboard_boards()
    except Exception as e:
        log.error(f"Refresh dashboard boards failed: {e}")

@bot.event
async def on_member_join(member: discord.Member):
    settings = await get_settings(member.guild.id)
    channel_id = settings.get("welcome_channel")
    if not channel_id:
        return
    channel = member.guild.get_channel(channel_id)
    if not channel:
        return
    msg = settings.get("welcome_message") or "ยินดีต้อนรับ {mention} เข้าสู่ {server}!"
    msg = (msg
           .replace("{mention}", member.mention)
           .replace("{user}", str(member))
           .replace("{name}", member.display_name)
           .replace("{server}", member.guild.name)
           .replace("{count}", str(member.guild.member_count)))
    try:
        await channel.send(msg)
    except Exception:
        pass

@bot.event
async def on_member_remove(member: discord.Member):
    settings = await get_settings(member.guild.id)
    channel_id = settings.get("welcome_channel")
    if not channel_id:
        return
    channel = member.guild.get_channel(channel_id)
    if not channel:
        return
    leave_msg = settings.get("leave_message")
    if not leave_msg:
        return
    leave_msg = (leave_msg
                 .replace("{mention}", member.mention)
                 .replace("{user}", str(member))
                 .replace("{name}", member.display_name)
                 .replace("{server}", member.guild.name)
                 .replace("{count}", str(member.guild.member_count)))
    try:
        await channel.send(leave_msg)
    except Exception:
        pass

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    settings = await get_settings(message.guild.id)
    if settings.get("automod_enabled"):
        content_lower = message.content.lower()
        pool = await db.get_pool()
        banned = await pool.fetch("SELECT word FROM banned_words WHERE guild_id = $1", message.guild.id)
        for row in banned:
            if row["word"].lower() in content_lower:
                try:
                    await message.delete()
                    await message.channel.send(f"{message.author.mention} ห้ามใช้คำต้องห้าม", delete_after=5)
                except Exception:
                    pass
                return
        if settings.get("anti_invite"):
            if "discord.gg/" in content_lower or "discord.com/invite/" in content_lower:
                try:
                    await message.delete()
                    await message.channel.send(f"{message.author.mention} ห้ามโพสต์ invite", delete_after=5)
                except Exception:
                    pass
                return
        if settings.get("anti_mention_spam"):
            limit = settings.get("mention_limit") or 5
            if len(message.mentions) >= limit:
                try:
                    await message.delete()
                    await message.channel.send(f"{message.author.mention} mention เยอะเกินไป", delete_after=5)
                except Exception:
                    pass
                return
    prefix = settings.get("prefix") or "!"
    if message.content.startswith(prefix):
        cmd_name = message.content[len(prefix):].split()[0].lower()
        pool = await db.get_pool()
        row = await pool.fetchrow(
            "SELECT response FROM custom_commands WHERE guild_id = $1 AND name = $2",
            message.guild.id, cmd_name
        )
        if row:
            await message.channel.send(row["response"])
            return
    await bot.process_commands(message)

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    pool = await db.get_pool()
    emoji = str(payload.emoji)
    row = await pool.fetchrow(
        "SELECT role_id FROM reaction_roles WHERE message_id = $1 AND emoji = $2",
        payload.message_id, emoji
    )
    if not row:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    member = guild.get_member(payload.user_id)
    role = guild.get_role(row["role_id"])
    if member and role:
        try:
            await member.add_roles(role, reason="Reaction role")
        except Exception:
            pass

@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    pool = await db.get_pool()
    emoji = str(payload.emoji)
    row = await pool.fetchrow(
        "SELECT role_id FROM reaction_roles WHERE message_id = $1 AND emoji = $2",
        payload.message_id, emoji
    )
    if not row:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    member = guild.get_member(payload.user_id)
    role = guild.get_role(row["role_id"])
    if member and role:
        try:
            await member.remove_roles(role, reason="Reaction role remove")
        except Exception:
            pass

@bot.event
async def on_presence_update(before: discord.Member, after: discord.Member):
    if before.status == after.status:
        return
    pool = await db.get_pool()
    await pool.execute("""
        INSERT INTO presence_logs (guild_id, user_id, status, last_seen)
        VALUES ($1, $2, $3, NOW())
        ON CONFLICT (guild_id, user_id)
        DO UPDATE SET status = $3, last_seen = NOW()
    """, after.guild.id, after.id, str(after.status))

# Health
async def health_handler(request):
    return web.Response(text="OK", status=200)

async def start_health_server():
    app = web.Application()
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("BOT_HEALTH_PORT", 8099))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"Health server running on port {port}")

# Moderation
mod_group = app_commands.Group(name="mod", description="คำสั่ง moderation")

@mod_group.command(name="kick", description="เตะสมาชิกออกจากเซิร์ฟเวอร์")
@has_mod_perms()
@app_commands.describe(member="สมาชิกที่ต้องการเตะ", reason="เหตุผล")
async def mod_kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason"):
    if member.top_role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
        return await interaction.response.send_message("คุณไม่มีสิทธิ์เตะคนนี้", ephemeral=True)
    try:
        await member.kick(reason=reason)
        embed = discord.Embed(title="👢 Kick", color=0xFFAA00, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="User", value=f"{member} (`{member.id}`)")
        embed.add_field(name="Moderator", value=interaction.user.mention)
        embed.add_field(name="Reason", value=reason, inline=False)
        await interaction.response.send_message(embed=embed)
        await send_log(interaction.guild, embed)
    except Exception as e:
        await interaction.response.send_message(f"ล้มเหลว: {e}", ephemeral=True)

@mod_group.command(name="ban", description="แบนสมาชิก")
@has_mod_perms()
@app_commands.describe(member="สมาชิกที่ต้องการแบน", reason="เหตุผล", delete_days="ลบข้อความย้อนหลังกี่วัน (0-7)")
async def mod_ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason", delete_days: app_commands.Range[int, 0, 7] = 0):
    if member.top_role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
        return await interaction.response.send_message("คุณไม่มีสิทธิ์แบนคนนี้", ephemeral=True)
    try:
        # หมายเหตุ: discord.py บางเวอร์ชันใช้ delete_message_seconds แทน delete_message_days
        # ถ้าเจอ TypeError ให้เปลี่ยนเป็น delete_message_seconds=delete_days * 86400
        await member.ban(reason=reason, delete_message_days=delete_days)
        embed = discord.Embed(title="🔨 Ban", color=0xFF0000, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="User", value=f"{member} (`{member.id}`)")
        embed.add_field(name="Moderator", value=interaction.user.mention)
        embed.add_field(name="Reason", value=reason, inline=False)
        await interaction.response.send_message(embed=embed)
        await send_log(interaction.guild, embed)
    except Exception as e:
        await interaction.response.send_message(f"ล้มเหลว: {e}", ephemeral=True)

@mod_group.command(name="timeout", description="Timeout สมาชิก")
@has_mod_perms()
@app_commands.describe(member="สมาชิก", minutes="ระยะเวลา (นาที)", reason="เหตุผล")
async def mod_timeout(interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], reason: str = "No reason"):
    if member.top_role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
        return await interaction.response.send_message("คุณไม่มีสิทธิ์ timeout คนนี้", ephemeral=True)
    try:
        until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        await member.timeout(until, reason=reason)
        embed = discord.Embed(title="⏱️ Timeout", color=0xFFAA00, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="User", value=f"{member} (`{member.id}`)")
        embed.add_field(name="Duration", value=f"{minutes} นาที")
        embed.add_field(name="Moderator", value=interaction.user.mention)
        embed.add_field(name="Reason", value=reason, inline=False)
        await interaction.response.send_message(embed=embed)
        await send_log(interaction.guild, embed)
    except Exception as e:
        await interaction.response.send_message(f"ล้มเหลว: {e}", ephemeral=True)

@mod_group.command(name="warn", description="เตือนสมาชิก")
@has_mod_perms()
@app_commands.describe(member="สมาชิก", reason="เหตุผล")
async def mod_warn(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason"):
    pool = await db.get_pool()
    await pool.execute(
        "INSERT INTO warnings (guild_id, user_id, reason, moderator_id) VALUES ($1, $2, $3, $4)",
        interaction.guild.id, member.id, reason, interaction.user.id
    )
    count = await pool.fetchval("SELECT COUNT(*) FROM warnings WHERE guild_id = $1 AND user_id = $2", interaction.guild.id, member.id)
    embed = discord.Embed(title="⚠️ Warning", color=0xFFAA00, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="User", value=f"{member} (`{member.id}`)")
    embed.add_field(name="Total Warnings", value=str(count))
    embed.add_field(name="Moderator", value=interaction.user.mention)
    embed.add_field(name="Reason", value=reason, inline=False)
    await interaction.response.send_message(embed=embed)
    await send_log(interaction.guild, embed)

@mod_group.command(name="warnings", description="ดูประวัติการเตือน")
@has_mod_perms()
@app_commands.describe(member="สมาชิก")
async def mod_warnings(interaction: discord.Interaction, member: discord.Member):
    pool = await db.get_pool()
    rows = await pool.fetch(
        "SELECT id, reason, moderator_id, created_at FROM warnings WHERE guild_id = $1 AND user_id = $2 ORDER BY created_at DESC LIMIT 15",
        interaction.guild.id, member.id
    )
    if not rows:
        return await interaction.response.send_message(f"{member.mention} ยังไม่มี warning", ephemeral=True)
    embed = discord.Embed(title=f"Warnings ของ {member}", color=0xFFAA00)
    for r in rows:
        embed.add_field(
            name=f"#{r['id']} • {r['created_at'].strftime('%Y-%m-%d %H:%M')}",
            value=f"{r['reason'] or 'No reason'} (โดย <@{r['moderator_id']}>)",
            inline=False
        )
    await interaction.response.send_message(embed=embed)

@mod_group.command(name="clear", description="ลบข้อความจำนวนมาก")
@has_mod_perms()
@app_commands.describe(amount="จำนวนข้อความ (1-100)")
async def mod_clear(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"ลบแล้ว {len(deleted)} ข้อความ", ephemeral=True)

tree.add_command(mod_group)

# Welcome
welcome_group = app_commands.Group(name="welcome", description="ตั้งค่าข้อความต้อนรับ")

@welcome_group.command(name="channel", description="ตั้งช่อง welcome")
@has_mod_perms()
@app_commands.describe(channel="ช่องที่จะส่งข้อความต้อนรับ")
async def welcome_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    pool = await db.get_pool()
    await pool.execute("""
        INSERT INTO settings (guild_id, welcome_channel) VALUES ($1, $2)
        ON CONFLICT (guild_id) DO UPDATE SET welcome_channel = $2
    """, interaction.guild.id, channel.id)
    await interaction.response.send_message(f"ตั้ง welcome channel เป็น {channel.mention} แล้ว", ephemeral=True)

@welcome_group.command(name="message", description="ตั้งข้อความต้อนรับ")
@has_mod_perms()
@app_commands.describe(message="ข้อความต้อนรับ")
async def welcome_message(interaction: discord.Interaction, message: str):
    pool = await db.get_pool()
    await pool.execute("""
        INSERT INTO settings (guild_id, welcome_message) VALUES ($1, $2)
        ON CONFLICT (guild_id) DO UPDATE SET welcome_message = $2
    """, interaction.guild.id, message)
    await interaction.response.send_message("ตั้ง welcome message แล้ว", ephemeral=True)

@welcome_group.command(name="leave", description="ตั้งข้อความเมื่อสมาชิกออก")
@has_mod_perms()
@app_commands.describe(message="ข้อความ leave (ว่าง = ปิด)")
async def welcome_leave(interaction: discord.Interaction, message: str = ""):
    pool = await db.get_pool()
    await pool.execute("""
        INSERT INTO settings (guild_id, leave_message) VALUES ($1, $2)
        ON CONFLICT (guild_id) DO UPDATE SET leave_message = $2
    """, interaction.guild.id, message or None)
    await interaction.response.send_message("ตั้ง leave message แล้ว" if message else "ปิด leave message แล้ว", ephemeral=True)

tree.add_command(welcome_group)

# Automod
automod_group = app_commands.Group(name="automod", description="ระบบ auto moderation")

@automod_group.command(name="toggle", description="เปิด/ปิด automod")
@has_mod_perms()
async def automod_toggle(interaction: discord.Interaction):
    pool = await db.get_pool()
    settings = await get_settings(interaction.guild.id)
    new_val = not settings.get("automod_enabled", False)
    await pool.execute("""
        INSERT INTO settings (guild_id, automod_enabled) VALUES ($1, $2)
        ON CONFLICT (guild_id) DO UPDATE SET automod_enabled = $2
    """, interaction.guild.id, new_val)
    await interaction.response.send_message(f"Automod {'เปิด' if new_val else 'ปิด'} แล้ว", ephemeral=True)

@automod_group.command(name="anti_invite", description="เปิด/ปิด ห้ามโพสต์ Discord invite")
@has_mod_perms()
async def automod_anti_invite(interaction: discord.Interaction):
    pool = await db.get_pool()
    settings = await get_settings(interaction.guild.id)
    new_val = not settings.get("anti_invite", False)
    await pool.execute("""
        INSERT INTO settings (guild_id, anti_invite) VALUES ($1, $2)
        ON CONFLICT (guild_id) DO UPDATE SET anti_invite = $2
    """, interaction.guild.id, new_val)
    await interaction.response.send_message(f"Anti-invite {'เปิด' if new_val else 'ปิด'} แล้ว", ephemeral=True)

@automod_group.command(name="anti_mention_spam", description="เปิด/ปิด ป้องกัน mention spam")
@has_mod_perms()
@app_commands.describe(limit="จำนวน mention ที่ถือว่า spam (default 5)")
async def automod_anti_mention(interaction: discord.Interaction, limit: app_commands.Range[int, 2, 20] = 5):
    pool = await db.get_pool()
    settings = await get_settings(interaction.guild.id)
    new_val = not settings.get("anti_mention_spam", False)
    await pool.execute("""
        INSERT INTO settings (guild_id, anti_mention_spam, mention_limit) VALUES ($1, $2, $3)
        ON CONFLICT (guild_id) DO UPDATE SET anti_mention_spam = $2, mention_limit = $3
    """, interaction.guild.id, new_val, limit)
    await interaction.response.send_message(f"Anti-mention-spam {'เปิด' if new_val else 'ปิด'} (limit={limit})", ephemeral=True)

@automod_group.command(name="addword", description="เพิ่มคำต้องห้าม")
@has_mod_perms()
@app_commands.describe(word="คำที่ต้องการแบน")
async def automod_addword(interaction: discord.Interaction, word: str):
    pool = await db.get_pool()
    await pool.execute(
        "INSERT INTO banned_words (guild_id, word) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        interaction.guild.id, word.lower()
    )
    await interaction.response.send_message(f"เพิ่มคำ `{word}` แล้ว", ephemeral=True)

@automod_group.command(name="removeword", description="ลบคำต้องห้าม")
@has_mod_perms()
@app_commands.describe(word="คำที่ต้องการลบ")
async def automod_removeword(interaction: discord.Interaction, word: str):
    pool = await db.get_pool()
    await pool.execute("DELETE FROM banned_words WHERE guild_id = $1 AND word = $2", interaction.guild.id, word.lower())
    await interaction.response.send_message(f"ลบคำ `{word}` แล้ว", ephemeral=True)

@automod_group.command(name="listwords", description="ดูรายการคำต้องห้าม")
@has_mod_perms()
async def automod_listwords(interaction: discord.Interaction):
    pool = await db.get_pool()
    rows = await pool.fetch("SELECT word FROM banned_words WHERE guild_id = $1 ORDER BY word", interaction.guild.id)
    if not rows:
        return await interaction.response.send_message("ยังไม่มีคำต้องห้าม", ephemeral=True)
    words = ", ".join(f"`{r['word']}`" for r in rows)
    await interaction.response.send_message(f"**Banned words:**\n{words}", ephemeral=True)

tree.add_command(automod_group)

# Custom Commands
cc_group = app_commands.Group(name="customcommand", description="จัดการ custom commands")

@cc_group.command(name="add", description="เพิ่ม custom command")
@has_mod_perms()
@app_commands.describe(name="ชื่อคำสั่ง (ไม่ต้องใส่ prefix)", response="ข้อความตอบกลับ")
async def cc_add(interaction: discord.Interaction, name: str, response: str):
    name = name.lower().strip()
    pool = await db.get_pool()
    await pool.execute("""
        INSERT INTO custom_commands (guild_id, name, response) VALUES ($1, $2, $3)
        ON CONFLICT (guild_id, name) DO UPDATE SET response = $3
    """, interaction.guild.id, name, response)
    settings = await get_settings(interaction.guild.id)
    prefix = settings.get("prefix") or "!"
    await interaction.response.send_message(f"เพิ่ม `{prefix}{name}` แล้ว", ephemeral=True)

@cc_group.command(name="remove", description="ลบ custom command")
@has_mod_perms()
@app_commands.describe(name="ชื่อคำสั่ง")
async def cc_remove(interaction: discord.Interaction, name: str):
    pool = await db.get_pool()
    await pool.execute("DELETE FROM custom_commands WHERE guild_id = $1 AND name = $2", interaction.guild.id, name.lower())
    await interaction.response.send_message(f"ลบ `{name}` แล้ว", ephemeral=True)

@cc_group.command(name="list", description="ดูรายการ custom commands")
async def cc_list(interaction: discord.Interaction):
    pool = await db.get_pool()
    rows = await pool.fetch("SELECT name, response FROM custom_commands WHERE guild_id = $1 ORDER BY name", interaction.guild.id)
    settings = await get_settings(interaction.guild.id)
    prefix = settings.get("prefix") or "!"
    if not rows:
        return await interaction.response.send_message("ยังไม่มี custom command", ephemeral=True)
    text = "\n".join(f"`{prefix}{r['name']}` → {r['response'][:60]}..." if len(r['response']) > 60 else f"`{prefix}{r['name']}` → {r['response']}" for r in rows)
    await interaction.response.send_message(f"**Custom Commands**\n{text}", ephemeral=True)

@cc_group.command(name="prefix", description="เปลี่ยน prefix ของ custom commands")
@has_mod_perms()
@app_commands.describe(prefix="prefix ใหม่ (เช่น ! หรือ ?)")
async def cc_prefix(interaction: discord.Interaction, prefix: str):
    if len(prefix) > 5:
        return await interaction.response.send_message("prefix ยาวเกินไป", ephemeral=True)
    pool = await db.get_pool()
    await pool.execute("""
        INSERT INTO settings (guild_id, prefix) VALUES ($1, $2)
        ON CONFLICT (guild_id) DO UPDATE SET prefix = $2
    """, interaction.guild.id, prefix)
    await interaction.response.send_message(f"ตั้ง prefix เป็น `{prefix}` แล้ว", ephemeral=True)

tree.add_command(cc_group)

# Reaction Roles
rr_group = app_commands.Group(name="reactionrole", description="ระบบ reaction roles")

@rr_group.command(name="add", description="เพิ่ม reaction role บนข้อความที่มีอยู่")
@has_mod_perms()
@app_commands.describe(message_id="ID ของข้อความ", emoji="อีโมจิ", role="ยศที่จะให้")
async def rr_add(interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role):
    try:
        mid = int(message_id)
    except ValueError:
        return await interaction.response.send_message("message_id ต้องเป็นตัวเลข", ephemeral=True)
    try:
        channel = interaction.channel
        msg = await channel.fetch_message(mid)
        await msg.add_reaction(emoji)
    except Exception as e:
        return await interaction.response.send_message(f"ไม่สามารถเพิ่ม reaction ได้: {e}", ephemeral=True)
    pool = await db.get_pool()
    await pool.execute("""
        INSERT INTO reaction_roles (message_id, emoji, role_id, guild_id)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (message_id, emoji) DO UPDATE SET role_id = $3
    """, mid, emoji, role.id, interaction.guild.id)
    await interaction.response.send_message(f"เพิ่ม reaction role {emoji} → {role.mention} แล้ว", ephemeral=True)

@rr_group.command(name="remove", description="ลบ reaction role")
@has_mod_perms()
@app_commands.describe(message_id="ID ของข้อความ", emoji="อีโมจิ")
async def rr_remove(interaction: discord.Interaction, message_id: str, emoji: str):
    try:
        mid = int(message_id)
    except ValueError:
        return await interaction.response.send_message("message_id ต้องเป็นตัวเลข", ephemeral=True)
    pool = await db.get_pool()
    await pool.execute("DELETE FROM reaction_roles WHERE message_id = $1 AND emoji = $2", mid, emoji)
    await interaction.response.send_message("ลบ reaction role แล้ว", ephemeral=True)

tree.add_command(rr_group)

# ─────────────────────────────────────────────
# Queue System (Fixed)
# ─────────────────────────────────────────────

async def build_queue_embed(guild: discord.Guild) -> discord.Embed:
    """สร้าง embed แสดงรายการคิวปัจจุบัน"""
    pool = await db.get_pool()
    rows = await pool.fetch(
        "SELECT user_id, position, called FROM queue WHERE guild_id = $1 ORDER BY position ASC LIMIT 30",
        guild.id
    )
    if not rows:
        description = "*ยังไม่มีคนในคิว*"
    else:
        lines = []
        for i, r in enumerate(rows, 1):
            if i == 1:
                prefix = "🔔 " if r.get("called") else "▶️ "
            else:
                prefix = f"`{i}.` "
            lines.append(f"{prefix}<@{r['user_id']}>")
        description = "\n".join(lines)

    embed = discord.Embed(
        title="📋 กระดานคิวสด",
        description=description,
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text="เข้า/ออกคิว: ทุกคน • เรียก/จบ: แอดมินเท่านั้น • กดรีเฟรชเพื่ออัปเดต")
    return embed


async def refresh_all_boards(guild: discord.Guild):
    """
    อัปเดต embed ของกระดานคิวทุกอันที่เคยโพสต์ไว้ในกิลด์นี้ ให้เป็นเรียลไทม์
    เรียกใช้หลังทุกจุดที่ทำให้คิวเปลี่ยนแปลง (เข้า/ออก/เรียก/จบ/ล้างคิว)
    ไม่ว่าจะเปลี่ยนผ่านปุ่มบนกระดาน หรือผ่านสแลชคอมมานด์ก็ตาม
    """
    pool = await db.get_pool()
    boards = await pool.fetch(
        "SELECT channel_id, message_id FROM queue_board WHERE guild_id = $1",
        guild.id
    )
    if not boards:
        return

    embed = await build_queue_embed(guild)
    view = QueueFullBoardView()

    for board in boards:
        channel = guild.get_channel(board["channel_id"])
        if not channel:
            # ช่องถูกลบไปแล้ว เก็บกวาด record ทิ้ง
            await pool.execute(
                "DELETE FROM queue_board WHERE guild_id = $1 AND channel_id = $2",
                guild.id, board["channel_id"]
            )
            continue
        try:
            msg = await channel.fetch_message(board["message_id"])
            await msg.edit(embed=embed, view=view)
        except discord.NotFound:
            # ข้อความกระดานถูกลบไปแล้ว เก็บกวาด record ทิ้ง
            await pool.execute(
                "DELETE FROM queue_board WHERE guild_id = $1 AND channel_id = $2",
                guild.id, board["channel_id"]
            )
        except Exception:
            pass


@tasks.loop(minutes=1)
async def check_bookings():
    """
    รันทุก 1 นาที ตรวจสอบการจองคิวล่วงหน้า (queue_bookings):
    1) ถ้าใกล้ถึงเวลาจอง (ภายใน 10 นาที) และยังไม่เคยเตือน -> ส่ง DM เตือน
    2) ถ้าถึงเวลาจองแล้ว -> เพิ่มเข้าคิวจริงอัตโนมัติ + DM แจ้งว่าเข้าคิวแล้ว + อัปเดตกระดาน
    """
    try:
        pool = await db.get_pool()
    except Exception:
        return

    now = datetime.now(timezone.utc)
    reminder_window = now + timedelta(minutes=10)

    # 1) แจ้งเตือนล่วงหน้า
    to_remind = await pool.fetch(
        """
        SELECT id, guild_id, user_id FROM queue_bookings
        WHERE activated = FALSE AND reminded = FALSE AND slot_time <= $1 AND slot_time > $2
        """,
        reminder_window, now
    )
    for b in to_remind:
        guild = bot.get_guild(b["guild_id"])
        member = guild.get_member(b["user_id"]) if guild else None
        if member:
            try:
                await member.send(f"⏰ อีกไม่เกิน 10 นาทีจะถึงคิวที่คุณจองไว้ใน **{guild.name}** เตรียมตัวได้เลยครับ")
            except Exception:
                pass  # ปิด DM ไว้ หรือส่งไม่สำเร็จ ไม่เป็นไร ข้ามไป
        await pool.execute("UPDATE queue_bookings SET reminded = TRUE WHERE id = $1", b["id"])

    # 2) ถึงเวลาจองแล้ว -> เพิ่มเข้าคิวจริง
    due = await pool.fetch(
        """
        SELECT id, guild_id, user_id FROM queue_bookings
        WHERE activated = FALSE AND slot_time <= $1
        """,
        now
    )
    for b in due:
        guild = bot.get_guild(b["guild_id"])
        if not guild:
            await pool.execute("UPDATE queue_bookings SET activated = TRUE WHERE id = $1", b["id"])
            continue

        exists = await pool.fetchval(
            "SELECT 1 FROM queue WHERE guild_id = $1 AND user_id = $2",
            b["guild_id"], b["user_id"]
        )
        if not exists:
            max_pos = await pool.fetchval(
                "SELECT COALESCE(MAX(position), 0) FROM queue WHERE guild_id = $1", b["guild_id"]
            ) or 0
            await pool.execute(
                "INSERT INTO queue (guild_id, user_id, position) VALUES ($1, $2, $3)",
                b["guild_id"], b["user_id"], max_pos + 1
            )

        await pool.execute("UPDATE queue_bookings SET activated = TRUE WHERE id = $1", b["id"])

        member = guild.get_member(b["user_id"])
        if member:
            try:
                await member.send(f"✅ ถึงเวลาจองของคุณแล้ว! ระบบเพิ่มคุณเข้าคิวใน **{guild.name}** ให้อัตโนมัติแล้วครับ")
            except Exception:
                pass

        await refresh_all_boards(guild)


@check_bookings.before_loop
async def before_check_bookings():
    await bot.wait_until_ready()


class QueueFullBoardView(discord.ui.View):
    """
    กระดานคิวรวม 5 ปุ่มในข้อความเดียว:
    แถวบน (ทุกคนกดได้): เข้าคิว / ออกจากคิว
    แถวล่าง (แอดมินเท่านั้น ยกเว้นรีเฟรช): เรียก / จบ / รีเฟรช
    """
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        cid = interaction.data.get("custom_id") if interaction.data else None
        # ปุ่มที่ทุกคนกดได้: เข้าคิว, ออกจากคิว, รีเฟรช
        if cid in ("queue_join", "queue_leave", "queue_board_refresh"):
            return True
        # ปุ่มที่เหลือ (เรียก, จบ) ต้องมีสิทธิ์ manage_messages
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message(
                "❌ คุณไม่มีสิทธิ์ใช้ปุ่มนี้ (ต้องมีสิทธิ์ Manage Messages)",
                ephemeral=True
            )
            return False
        return True

    # ── แถวเข้า/ออกคิว (ทุกคน) ────────────────────────────────
    @discord.ui.button(label="เข้าคิว", style=discord.ButtonStyle.green, custom_id="queue_join", row=0)
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        pool = await db.get_pool()
        exists = await pool.fetchval("SELECT 1 FROM queue WHERE guild_id = $1 AND user_id = $2", interaction.guild.id, interaction.user.id)
        if exists:
            return await interaction.response.send_message("คุณอยู่ในคิวแล้ว", ephemeral=True)
        max_pos = await pool.fetchval("SELECT COALESCE(MAX(position), 0) FROM queue WHERE guild_id = $1", interaction.guild.id) or 0
        await pool.execute(
            "INSERT INTO queue (guild_id, user_id, position) VALUES ($1, $2, $3)",
            interaction.guild.id, interaction.user.id, max_pos + 1
        )
        await interaction.response.send_message(f"เข้าคิวแล้ว ตำแหน่งที่ **{max_pos + 1}**", ephemeral=True)
        await refresh_all_boards(interaction.guild)

    @discord.ui.button(label="ออกจากคิว", style=discord.ButtonStyle.red, custom_id="queue_leave", row=0)
    async def leave_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        pool = await db.get_pool()
        await pool.execute("DELETE FROM queue WHERE guild_id = $1 AND user_id = $2", interaction.guild.id, interaction.user.id)
        await interaction.response.send_message("ออกจากคิวแล้ว", ephemeral=True)
        await refresh_all_boards(interaction.guild)

    # ── แถวแอดมิน (เรียก / จบ / รีเฟรช) ───────────────────────
    @discord.ui.button(label="เรียก", style=discord.ButtonStyle.green, custom_id="queue_board_call", row=1)
    async def call_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            pool = await db.get_pool()
            row = await pool.fetchrow(
                "SELECT user_id, called FROM queue WHERE guild_id = $1 ORDER BY position ASC LIMIT 1",
                interaction.guild.id
            )
        except Exception:
            return await interaction.response.send_message("❌ เชื่อมต่อฐานข้อมูลไม่สำเร็จ", ephemeral=True)

        if not row:
            return await interaction.response.send_message("ตอนนี้ไม่มีคนในคิว", ephemeral=True)

        if row.get("called"):
            return await interaction.response.send_message(
                "⚠️ คิวนี้ถูกเรียกไปแล้ว กด **จบ** ก่อนถ้าต้องการเรียกคนถัดไป",
                ephemeral=True
            )

        member = interaction.guild.get_member(row["user_id"])
        name = member.display_name if member else f"Unknown ({row['user_id']})"

        try:
            await pool.execute(
                "UPDATE queue SET called = TRUE WHERE guild_id = $1 AND user_id = $2",
                interaction.guild.id, row["user_id"]
            )
        except Exception:
            return await interaction.response.send_message("❌ อัปเดตสถานะคิวไม่สำเร็จ", ephemeral=True)

        announce = discord.Embed(
            title="📢 ถึงคิวแล้ว!",
            description=f"{member.mention if member else name} กรุณาเข้ามาได้เลย",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=announce)
        await refresh_all_boards(interaction.guild)

    @discord.ui.button(label="จบ", style=discord.ButtonStyle.red, custom_id="queue_board_end", row=1)
    async def end_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            pool = await db.get_pool()
            row = await pool.fetchrow(
                "SELECT user_id FROM queue WHERE guild_id = $1 ORDER BY position ASC LIMIT 1",
                interaction.guild.id
            )
        except Exception:
            return await interaction.response.send_message("❌ เชื่อมต่อฐานข้อมูลไม่สำเร็จ", ephemeral=True)

        if not row:
            return await interaction.response.send_message("ตอนนี้ไม่มีคนในคิว", ephemeral=True)

        finished_id = row["user_id"]

        try:
            await pool.execute(
                "DELETE FROM queue WHERE guild_id = $1 AND user_id = $2",
                interaction.guild.id, finished_id
            )
            # จัดลำดับใหม่
            await pool.execute("""
                WITH ordered AS (
                    SELECT user_id, ROW_NUMBER() OVER (ORDER BY position) AS new_pos
                    FROM queue WHERE guild_id = $1
                )
                UPDATE queue q SET position = o.new_pos
                FROM ordered o
                WHERE q.guild_id = $1 AND q.user_id = o.user_id
            """, interaction.guild.id)
        except Exception:
            return await interaction.response.send_message("❌ อัปเดตคิวไม่สำเร็จ", ephemeral=True)

        member = interaction.guild.get_member(finished_id)
        name = member.display_name if member else "สมาชิก"

        await interaction.response.send_message(f"✅ จบคิวของ **{name}** แล้ว", ephemeral=True)
        await refresh_all_boards(interaction.guild)

    @discord.ui.button(label="รีเฟรช", style=discord.ButtonStyle.secondary, custom_id="queue_board_refresh", row=1)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            embed = await build_queue_embed(interaction.guild)
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception:
            await interaction.response.send_message("❌ รีเฟรชไม่สำเร็จ", ephemeral=True)
            return
        await refresh_all_boards(interaction.guild)


queue_group = app_commands.Group(name="queue", description="ระบบคิว")

@queue_group.command(name="join", description="เข้าคิว")
async def queue_join(interaction: discord.Interaction):
    pool = await db.get_pool()
    exists = await pool.fetchval("SELECT 1 FROM queue WHERE guild_id = $1 AND user_id = $2", interaction.guild.id, interaction.user.id)
    if exists:
        return await interaction.response.send_message("คุณอยู่ในคิวแล้ว", ephemeral=True)
    max_pos = await pool.fetchval("SELECT COALESCE(MAX(position), 0) FROM queue WHERE guild_id = $1", interaction.guild.id) or 0
    await pool.execute(
        "INSERT INTO queue (guild_id, user_id, position) VALUES ($1, $2, $3)",
        interaction.guild.id, interaction.user.id, max_pos + 1
    )
    await interaction.response.send_message(f"เข้าคิวแล้ว ตำแหน่งที่ **{max_pos + 1}**", ephemeral=True)
    await refresh_all_boards(interaction.guild)

@queue_group.command(name="leave", description="ออกจากคิว")
async def queue_leave(interaction: discord.Interaction):
    pool = await db.get_pool()
    await pool.execute("DELETE FROM queue WHERE guild_id = $1 AND user_id = $2", interaction.guild.id, interaction.user.id)
    await interaction.response.send_message("ออกจากคิวแล้ว", ephemeral=True)
    await refresh_all_boards(interaction.guild)

@queue_group.command(name="list", description="ดูรายการคิว")
async def queue_list(interaction: discord.Interaction):
    embed = await build_queue_embed(interaction.guild)
    await interaction.response.send_message(embed=embed)

@queue_group.command(name="book", description="จองคิวล่วงหน้า (ระบุวันและเวลาไทย)")
@app_commands.describe(date="วันที่ รูปแบบ YYYY-MM-DD เช่น 2026-08-10", time="เวลา รูปแบบ HH:MM (24 ชม.) เช่น 14:30")
async def queue_book(interaction: discord.Interaction, date: str, time: str):
    try:
        naive = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
        slot_time = naive.replace(tzinfo=BANGKOK_TZ)
    except ValueError:
        return await interaction.response.send_message(
            "❌ รูปแบบวันที่/เวลาไม่ถูกต้อง ใช้ `YYYY-MM-DD` และ `HH:MM` เช่น `2026-08-10` และ `14:30`",
            ephemeral=True
        )

    now = datetime.now(BANGKOK_TZ)
    if slot_time <= now:
        return await interaction.response.send_message("❌ ต้องจองเวลาที่ยังไม่ถึงเท่านั้น", ephemeral=True)

    pool = await db.get_pool()

    # กันจอง 2 คิวซ้อนกัน: 1 คนมีได้แค่ 1 การจองที่ยังไม่ถึงเวลาต่อกิลด์
    existing = await pool.fetchval(
        "SELECT 1 FROM queue_bookings WHERE guild_id = $1 AND user_id = $2 AND activated = FALSE",
        interaction.guild.id, interaction.user.id
    )
    if existing:
        return await interaction.response.send_message(
            "คุณมีการจองที่ยังไม่ถึงเวลาอยู่แล้ว ใช้ `/queue unbook` ก่อนถ้าต้องการจองใหม่",
            ephemeral=True
        )

    await pool.execute(
        "INSERT INTO queue_bookings (guild_id, user_id, slot_time) VALUES ($1, $2, $3)",
        interaction.guild.id, interaction.user.id, slot_time.astimezone(timezone.utc)
    )

    thai_str = slot_time.strftime("%d/%m/%Y เวลา %H:%M น.")
    await interaction.response.send_message(
        f"📅 จองคิวเรียบร้อย: **{thai_str}** (เวลาไทย)\n"
        f"ระบบจะ DM แจ้งเตือนล่วงหน้า 10 นาที และเพิ่มเข้าคิวให้อัตโนมัติเมื่อถึงเวลา (เปิดรับ DM จากสมาชิกเซิร์ฟเวอร์ไว้ด้วยนะครับ)",
        ephemeral=True
    )

@queue_group.command(name="unbook", description="ยกเลิกการจองคิวล่วงหน้าของตัวเอง")
async def queue_unbook(interaction: discord.Interaction):
    pool = await db.get_pool()
    result = await pool.execute(
        "DELETE FROM queue_bookings WHERE guild_id = $1 AND user_id = $2 AND activated = FALSE",
        interaction.guild.id, interaction.user.id
    )
    if result == "DELETE 0":
        return await interaction.response.send_message("คุณไม่มีการจองที่รอดำเนินการอยู่", ephemeral=True)
    await interaction.response.send_message("ยกเลิกการจองแล้ว", ephemeral=True)

@queue_group.command(name="bookings", description="ดูรายการจองคิวล่วงหน้าที่รอดำเนินการ (แอดมิน)")
@has_mod_perms()
async def queue_bookings_cmd(interaction: discord.Interaction):
    pool = await db.get_pool()
    rows = await pool.fetch(
        "SELECT user_id, slot_time FROM queue_bookings WHERE guild_id = $1 AND activated = FALSE ORDER BY slot_time ASC LIMIT 20",
        interaction.guild.id
    )
    if not rows:
        return await interaction.response.send_message("ยังไม่มีการจองคิวล่วงหน้า", ephemeral=True)
    lines = []
    for r in rows:
        local_time = r["slot_time"].astimezone(BANGKOK_TZ)
        member = interaction.guild.get_member(r["user_id"])
        name = member.display_name if member else f"Unknown ({r['user_id']})"
        lines.append(f"🗓️ {local_time.strftime('%d/%m %H:%M')} น. — {name}")
    embed = discord.Embed(title="📅 รายการจองคิวล่วงหน้า", description="\n".join(lines), color=0x5865F2)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@queue_group.command(name="clear", description="ล้างคิวทั้งหมด (ไม่ลบกระดาน)")
@has_mod_perms()
async def queue_clear(interaction: discord.Interaction):
    """
    ล้างคนในคิวทั้งหมดของ guild และแจ้งจำนวนคนที่ถูกล้างในข้อความตอบกลับ
    หลังล้างแล้วจะอัปเดต embed ของทุกกระดานคิวที่โพสต์ไว้ในกิลด์นี้ให้อัตโนมัติ (real-time)
    """
    pool = await db.get_pool()

    count = await pool.fetchval(
        "SELECT COUNT(*) FROM queue WHERE guild_id = $1",
        interaction.guild.id
    )

    if not count:
        return await interaction.response.send_message("ตอนนี้ไม่มีใครอยู่ในคิว", ephemeral=True)

    await pool.execute("DELETE FROM queue WHERE guild_id = $1", interaction.guild.id)

    await interaction.response.send_message(
        f"✅ ล้างคิวทั้งหมดแล้ว ({count} คน)",
        ephemeral=True
    )
    await refresh_all_boards(interaction.guild)

@queue_group.command(name="board", description="โพสต์กระดานคิวสด (รวมปุ่มเข้า/ออกคิว + เรียก/จบ/รีเฟรช ในที่เดียว)")
@has_mod_perms()
async def queue_board(interaction: discord.Interaction):
    embed = await build_queue_embed(interaction.guild)
    view = QueueFullBoardView()
    await interaction.response.send_message(embed=embed, view=view)
    msg = await interaction.original_response()

    pool = await db.get_pool()
    await pool.execute(
        """
        INSERT INTO queue_board (guild_id, channel_id, message_id)
        VALUES ($1, $2, $3)
        ON CONFLICT (guild_id, channel_id) DO UPDATE SET message_id = EXCLUDED.message_id
        """,
        interaction.guild.id, interaction.channel.id, msg.id
    )

tree.add_command(queue_group)

# Support
async def _notify_support_request(interaction: discord.Interaction, *, emoji: str, action_text: str):
    """
    แจ้งเตือนเมื่อมีคนกดปุ่มใน Support Panel — ระบุคน (ผู้กด) + ห้อง (ที่กด) เสมอ
    และปิง role แอดมิน (ถ้าตั้งไว้ด้วย /settings supportrole) ทั้งในห้องที่กด และห้อง log
    (ถ้าตั้งไว้ด้วย /settings logchannel) เผื่อแอดมินไม่ได้อยู่ในห้องนั้น
    """
    settings = await get_settings(interaction.guild.id)
    role_id = settings.get("support_role")
    role_mention = f"<@&{role_id}>" if role_id else ""

    origin_text = (
        f"{emoji} {role_mention} {interaction.user.mention} {action_text}ที่ห้อง "
        f"{interaction.channel.mention} — รอสักครู่"
    ).replace("  ", " ")
    await interaction.response.send_message(origin_text, ephemeral=False)

    log_channel_id = settings.get("log_channel")
    if log_channel_id and log_channel_id != interaction.channel.id:
        log_channel = interaction.guild.get_channel(log_channel_id)
        if log_channel:
            # หมายเหตุ: ข้อความธรรมดาใน Discord ไม่ render markdown link แบบ [text](url)
            # (ใช้ได้แค่ใน embed) เลยต้องแปะ URL ตรงๆ ให้ Discord auto-link ให้เอง
            jump_url = f"https://discord.com/channels/{interaction.guild.id}/{interaction.channel.id}"
            log_text = (
                f"{emoji} {role_mention} {interaction.user.mention} {action_text}ที่ห้อง "
                f"{interaction.channel.mention} — {jump_url}"
            ).replace("  ", " ")
            try:
                await log_channel.send(log_text)
            except Exception:
                pass


class SupportView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="เรียกแอดมิน", style=discord.ButtonStyle.danger, custom_id="support_admin")
    async def call_admin(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _notify_support_request(interaction, emoji="🔔", action_text="เรียกแอดมินแล้ว")

    @discord.ui.button(label="ขอความช่วยเหลือ", style=discord.ButtonStyle.primary, custom_id="support_help")
    async def request_help(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _notify_support_request(interaction, emoji="📩", action_text="ขอความช่วยเหลือแล้ว")

support_group = app_commands.Group(name="supportpanel", description="แผงช่วยเหลือ")

@support_group.command(name="panel", description="โพสต์แผง support")
@has_mod_perms()
async def support_panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🆘 Support Panel",
        description="ต้องการความช่วยเหลือ? กดปุ่มด้านล่าง",
        color=0x57F287
    )
    view = SupportView()
    await interaction.response.send_message(embed=embed, view=view)

tree.add_command(support_group)

# Settings
settings_group = app_commands.Group(name="settings", description="ตั้งค่าบอท")

@settings_group.command(name="logchannel", description="ตั้งช่อง log สำหรับ moderation")
@has_mod_perms()
@app_commands.describe(channel="ช่อง log")
async def settings_logchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    pool = await db.get_pool()
    await pool.execute("""
        INSERT INTO settings (guild_id, log_channel) VALUES ($1, $2)
        ON CONFLICT (guild_id) DO UPDATE SET log_channel = $2
    """, interaction.guild.id, channel.id)
    await interaction.response.send_message(f"ตั้ง log channel เป็น {channel.mention} แล้ว", ephemeral=True)

@settings_group.command(name="supportrole", description="ตั้ง role ที่จะถูกปิงเมื่อมีคนกดปุ่มใน Support Panel")
@has_mod_perms()
@app_commands.describe(role="Role ของแอดมิน/ทีมงานที่จะถูกปิง")
async def settings_supportrole(interaction: discord.Interaction, role: discord.Role):
    pool = await db.get_pool()
    await pool.execute("""
        INSERT INTO settings (guild_id, support_role) VALUES ($1, $2)
        ON CONFLICT (guild_id) DO UPDATE SET support_role = $2
    """, interaction.guild.id, role.id)
    await interaction.response.send_message(f"ตั้ง role ที่ปิงตอนมีคนขอความช่วยเหลือเป็น {role.mention} แล้ว", ephemeral=True)

@settings_group.command(name="dashboardchannel", description="ตั้งช่องปักหมุดกระดานรวมคำสั่งบอททุกตัว (สำหรับแอดมิน/คนใหม่)")
@has_mod_perms()
@app_commands.describe(channel="ช่องที่จะโพสต์/ปักหมุดกระดานรวมบอท")
async def settings_dashboardchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    pool = await db.get_pool()
    await pool.execute("""
        INSERT INTO settings (guild_id, dashboard_channel) VALUES ($1, $2)
        ON CONFLICT (guild_id) DO UPDATE SET dashboard_channel = $2
    """, interaction.guild.id, channel.id)
    await interaction.response.defer(ephemeral=True)
    await refresh_dashboard_board(interaction.guild, channel.id)
    await interaction.followup.send(f"ตั้งช่องกระดานรวมบอทเป็น {channel.mention} และปักหมุดให้เรียบร้อย", ephemeral=True)

tree.add_command(settings_group)

# ─── Voice Channel commands ──────────────────────────────────────────────────

voice_group = app_commands.Group(name="voice", description="จัดการห้องเสียง (Voice Channel)")

@voice_group.command(name="create", description="สร้าง Voice Channel")
@has_mod_perms()
@app_commands.describe(
    name="ชื่อห้องเสียง",
    user_limit="จำนวนคนสูงสุด (0 = ไม่จำกัด)",
    category="หมวดหมู่ที่จะใส่ห้อง (ไม่บังคับ)"
)
async def voice_create(
    interaction: discord.Interaction,
    name: str,
    user_limit: app_commands.Range[int, 0, 99] = 0,
    category: discord.CategoryChannel | None = None
):
    try:
        channel = await interaction.guild.create_voice_channel(
            name=name,
            user_limit=user_limit if user_limit > 0 else 0,
            category=category,
            reason=f"สร้างโดย {interaction.user}"
        )
        embed = discord.Embed(
            title="🔊 สร้าง Voice Channel แล้ว",
            color=0x57F287,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="ชื่อ", value=channel.mention)
        embed.add_field(name="จำกัดคน", value=str(user_limit) if user_limit else "ไม่จำกัด")
        if category:
            embed.add_field(name="หมวดหมู่", value=category.name)
        embed.set_footer(text=f"โดย {interaction.user}")
        await interaction.response.send_message(embed=embed)
        await send_log(interaction.guild, embed)
    except discord.Forbidden:
        await interaction.response.send_message("❌ บอทไม่มีสิทธิ์สร้างห้องเสียง (ต้องการ Manage Channels)", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ ล้มเหลว: {e}", ephemeral=True)

@voice_group.command(name="delete", description="ลบ Voice Channel")
@has_mod_perms()
@app_commands.describe(channel="ห้องเสียงที่ต้องการลบ")
async def voice_delete(interaction: discord.Interaction, channel: discord.VoiceChannel):
    name = channel.name
    try:
        await channel.delete(reason=f"ลบโดย {interaction.user}")
        embed = discord.Embed(
            title="🗑️ ลบ Voice Channel แล้ว",
            description=f"ลบห้อง `{name}` เรียบร้อย",
            color=0xED4245,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text=f"โดย {interaction.user}")
        await interaction.response.send_message(embed=embed)
        await send_log(interaction.guild, embed)
    except discord.Forbidden:
        await interaction.response.send_message("❌ บอทไม่มีสิทธิ์ลบห้องนี้", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ ล้มเหลว: {e}", ephemeral=True)

@voice_group.command(name="limit", description="ตั้งจำนวนคนสูงสุดใน Voice Channel")
@has_mod_perms()
@app_commands.describe(channel="ห้องเสียง", user_limit="จำนวนคนสูงสุด (0 = ไม่จำกัด)")
async def voice_limit(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel,
    user_limit: app_commands.Range[int, 0, 99]
):
    try:
        await channel.edit(user_limit=user_limit if user_limit > 0 else 0, reason=f"โดย {interaction.user}")
        limit_text = str(user_limit) if user_limit else "ไม่จำกัด"
        await interaction.response.send_message(
            f"✅ ตั้ง `{channel.name}` จำกัด **{limit_text}** คนแล้ว",
            ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(f"❌ ล้มเหลว: {e}", ephemeral=True)

@voice_group.command(name="rename", description="เปลี่ยนชื่อ Voice Channel")
@has_mod_perms()
@app_commands.describe(channel="ห้องเสียง", new_name="ชื่อใหม่")
async def voice_rename(interaction: discord.Interaction, channel: discord.VoiceChannel, new_name: str):
    old = channel.name
    try:
        await channel.edit(name=new_name, reason=f"โดย {interaction.user}")
        await interaction.response.send_message(
            f"✅ เปลี่ยนชื่อ `{old}` → `{new_name}` แล้ว",
            ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(f"❌ ล้มเหลว: {e}", ephemeral=True)

tree.add_command(voice_group)

# ─── Breakout Rooms ──────────────────────────────────────────────────────────

breakout_group = app_commands.Group(name="breakout", description="แยกห้องเสียงเป็นห้องย่อย (breakout rooms)")

@breakout_group.command(name="start", description="แยกคนในห้องเสียงออกเป็นห้องย่อยหลายห้องอัตโนมัติ")
@has_mod_perms()
@app_commands.describe(source="ห้องเสียงหลักที่จะแยกคนออก", rooms="จำนวนห้องย่อยที่ต้องการ (2-10)")
async def breakout_start(interaction: discord.Interaction, source: discord.VoiceChannel, rooms: app_commands.Range[int, 2, 10]):
    pool = await db.get_pool()

    existing = await pool.fetchrow(
        "SELECT source_channel_id FROM breakout_sessions WHERE guild_id = $1",
        interaction.guild.id
    )
    if existing:
        return await interaction.response.send_message(
            "⚠️ มี breakout session ที่ยังไม่จบอยู่แล้ว ใช้ `/breakout recall` ก่อนเริ่มใหม่",
            ephemeral=True
        )

    members = [m for m in source.members if not m.bot]
    if not members:
        return await interaction.response.send_message(f"ไม่มีคนอยู่ในห้อง {source.mention}", ephemeral=True)

    await interaction.response.defer()

    random.shuffle(members)
    groups = [members[i::rooms] for i in range(rooms)]

    created_channels = []
    try:
        for i in range(rooms):
            ch = await interaction.guild.create_voice_channel(
                name=f"{source.name} - กลุ่ม {i + 1}",
                category=source.category,
                reason=f"Breakout room สร้างโดย {interaction.user}"
            )
            created_channels.append(ch)
    except discord.Forbidden:
        for ch in created_channels:
            try:
                await ch.delete()
            except Exception:
                pass
        return await interaction.followup.send("❌ บอทไม่มีสิทธิ์สร้างห้องเสียง (ต้องการ Manage Channels)")

    moved_summary = []
    owner_ids = []
    for i, group in enumerate(groups):
        moved_names = []
        for member in group:
            try:
                await member.move_to(created_channels[i], reason="Breakout room")
                moved_names.append(member.display_name)
            except Exception:
                pass  # ย้ายไม่สำเร็จ (เช่นออกจากห้องไปแล้ว) ข้ามไป

        owner = group[0] if group else None
        owner_ids.append(owner.id if owner else 0)
        owner_tag = f" 👑 หัวหน้าห้อง: {owner.display_name}" if owner else ""
        moved_summary.append(
            f"**{created_channels[i].mention}**: {', '.join(moved_names) if moved_names else '(ว่าง)'}{owner_tag}"
        )

    await pool.execute(
        """
        INSERT INTO breakout_sessions (guild_id, source_channel_id, room_channel_ids, owner_ids)
        VALUES ($1, $2, $3, $4)
        """,
        interaction.guild.id, source.id, [ch.id for ch in created_channels], owner_ids
    )

    embed = discord.Embed(
        title="🔀 แยกห้องย่อยเรียบร้อย",
        description="\n".join(moved_summary),
        color=0x57F287
    )
    embed.set_footer(text="👑 หัวหน้าห้องจะได้สิทธิ์พูดในห้องใหญ่อัตโนมัติตอน /breakout recall • เปลี่ยนหัวหน้าได้ด้วย /breakout setowner")
    await interaction.followup.send(embed=embed)


@breakout_group.command(name="recall", description="ดึงทุกคนกลับห้องหลัก และลบห้องย่อยทิ้ง")
@has_mod_perms()
async def breakout_recall(interaction: discord.Interaction):
    pool = await db.get_pool()
    session = await pool.fetchrow(
        "SELECT source_channel_id, room_channel_ids, owner_ids FROM breakout_sessions WHERE guild_id = $1",
        interaction.guild.id
    )
    if not session:
        return await interaction.response.send_message("ไม่มี breakout session ที่กำลังทำงานอยู่", ephemeral=True)

    await interaction.response.defer()

    source_channel = interaction.guild.get_channel(session["source_channel_id"])
    room_ids = session["room_channel_ids"]
    owner_ids = list(session["owner_ids"] or [])
    moved_count = 0
    speak_granted = []

    for idx, room_id in enumerate(room_ids):
        room = interaction.guild.get_channel(room_id)
        if not room:
            continue
        for member in list(room.members):
            if source_channel:
                try:
                    await member.move_to(source_channel, reason="Breakout recall")
                    moved_count += 1
                except Exception:
                    pass
        try:
            await room.delete(reason="Breakout session สิ้นสุด")
        except Exception:
            pass

    # มอบสิทธิ์พูดคืนให้หัวหน้าห้องแต่ละห้อง ในห้องใหญ่ (เผื่อห้องใหญ่ถูกตั้งเป็นฟังอย่างเดียวไว้)
    if source_channel:
        for idx in range(len(room_ids)):
            owner_id = owner_ids[idx] if idx < len(owner_ids) else 0
            if not owner_id:
                continue
            owner_member = interaction.guild.get_member(owner_id)
            if not owner_member:
                continue
            try:
                overwrite = source_channel.overwrites_for(owner_member)
                overwrite.connect = True
                overwrite.speak = True
                await source_channel.set_permissions(
                    owner_member, overwrite=overwrite,
                    reason="Breakout owner ได้สิทธิ์พูดกลับห้องใหญ่"
                )
                speak_granted.append(owner_member.display_name)
            except Exception:
                pass

    await pool.execute("DELETE FROM breakout_sessions WHERE guild_id = $1", interaction.guild.id)

    if source_channel:
        msg = f"✅ ดึงกลับแล้ว {moved_count} คน มายัง {source_channel.mention} และลบห้องย่อยทิ้งหมดแล้ว"
        if speak_granted:
            msg += f"\n🎙️ ให้สิทธิ์พูดในห้องใหญ่แก่หัวหน้าห้อง: {', '.join(speak_granted)}"
        await interaction.followup.send(msg)
    else:
        await interaction.followup.send("✅ ลบห้องย่อยทิ้งหมดแล้ว (ห้องหลักเดิมถูกลบไปแล้ว จึงย้ายคนกลับไม่ได้)")


@breakout_group.command(name="move", description="ย้ายสมาชิกคนใดคนหนึ่งไปห้องย่อยที่ระบุ (หรือห้องเสียงใดก็ได้)")
@has_mod_perms()
@app_commands.describe(
    member="สมาชิกที่ต้องการย้าย",
    room="หมายเลขห้องย่อย (1, 2, 3, ...) จาก breakout session ที่กำลังทำงานอยู่",
    channel="หรือระบุห้องเสียงปลายทางตรงๆ (ใช้แทน room ก็ได้ถ้าไม่ได้ใช้ breakout session)"
)
async def breakout_move(
    interaction: discord.Interaction,
    member: discord.Member,
    room: Optional[int] = None,
    channel: Optional[discord.VoiceChannel] = None
):
    if room is None and channel is None:
        return await interaction.response.send_message("❌ ต้องระบุ `room` หรือ `channel` อย่างใดอย่างหนึ่ง", ephemeral=True)

    target_channel = channel
    if room is not None:
        pool = await db.get_pool()
        session = await pool.fetchrow(
            "SELECT room_channel_ids FROM breakout_sessions WHERE guild_id = $1",
            interaction.guild.id
        )
        if not session:
            return await interaction.response.send_message(
                "❌ ไม่มี breakout session ที่กำลังทำงานอยู่ ใช้ `/breakout start` ก่อน หรือระบุ `channel` แทน",
                ephemeral=True
            )
        room_ids = session["room_channel_ids"]
        if room < 1 or room > len(room_ids):
            return await interaction.response.send_message(f"❌ ห้องย่อยมีแค่หมายเลข 1-{len(room_ids)}", ephemeral=True)
        target_channel = interaction.guild.get_channel(room_ids[room - 1])
        if not target_channel:
            return await interaction.response.send_message("❌ ห้องย่อยนี้ถูกลบไปแล้ว", ephemeral=True)

    if member.voice is None or member.voice.channel is None:
        return await interaction.response.send_message(f"❌ {member.mention} ไม่ได้อยู่ในห้องเสียงใดเลย ย้ายไม่ได้", ephemeral=True)

    try:
        await member.move_to(target_channel, reason=f"Breakout manual move by {interaction.user}")
    except discord.Forbidden:
        return await interaction.response.send_message("❌ บอทไม่มีสิทธิ์ย้ายสมาชิก", ephemeral=True)
    except Exception as e:
        return await interaction.response.send_message(f"❌ ล้มเหลว: {e}", ephemeral=True)

    await interaction.response.send_message(f"✅ ย้าย {member.mention} ไป {target_channel.mention} แล้ว", ephemeral=True)


@breakout_group.command(name="setowner", description="กำหนด/เปลี่ยนหัวหน้าห้องย่อย (จะได้สิทธิ์พูดกลับห้องใหญ่ตอน recall)")
@has_mod_perms()
@app_commands.describe(member="สมาชิกที่จะตั้งเป็นหัวหน้าห้อง", room="หมายเลขห้องย่อย")
async def breakout_setowner(interaction: discord.Interaction, member: discord.Member, room: int):
    pool = await db.get_pool()
    session = await pool.fetchrow(
        "SELECT room_channel_ids, owner_ids FROM breakout_sessions WHERE guild_id = $1",
        interaction.guild.id
    )
    if not session:
        return await interaction.response.send_message("❌ ไม่มี breakout session ที่กำลังทำงานอยู่", ephemeral=True)

    room_ids = session["room_channel_ids"]
    if room < 1 or room > len(room_ids):
        return await interaction.response.send_message(f"❌ ห้องย่อยมีแค่หมายเลข 1-{len(room_ids)}", ephemeral=True)

    owners = list(session["owner_ids"] or [])
    while len(owners) < len(room_ids):
        owners.append(0)
    owners[room - 1] = member.id

    await pool.execute(
        "UPDATE breakout_sessions SET owner_ids = $1 WHERE guild_id = $2",
        owners, interaction.guild.id
    )
    await interaction.response.send_message(f"👑 ตั้ง {member.mention} เป็นหัวหน้าห้องย่อยหมายเลข {room} แล้ว", ephemeral=True)


@breakout_group.command(name="setspeakers", description="ตั้งค่าห้อง: เฉพาะบทบาทที่ระบุพูดได้ คนอื่นเข้าฟังอย่างเดียว")
@has_mod_perms()
@app_commands.describe(channel="ห้องเสียงที่ต้องการตั้งค่า (เช่นห้องประชุมใหญ่)", role="บทบาทที่อนุญาตให้พูดได้")
async def breakout_setspeakers(interaction: discord.Interaction, channel: discord.VoiceChannel, role: discord.Role):
    try:
        everyone_ow = channel.overwrites_for(interaction.guild.default_role)
        everyone_ow.connect = True
        everyone_ow.speak = False
        await channel.set_permissions(
            interaction.guild.default_role, overwrite=everyone_ow,
            reason=f"ตั้งสิทธิ์พูดตามบทบาทโดย {interaction.user}"
        )

        role_ow = channel.overwrites_for(role)
        role_ow.connect = True
        role_ow.speak = True
        await channel.set_permissions(
            role, overwrite=role_ow,
            reason=f"ตั้งสิทธิ์พูดตามบทบาทโดย {interaction.user}"
        )
    except discord.Forbidden:
        return await interaction.response.send_message(
            "❌ บอทไม่มีสิทธิ์แก้ไข permission ห้องนี้ (ต้องการ Manage Roles/Manage Channels)",
            ephemeral=True
        )

    await interaction.response.send_message(
        f"🔇 ตั้งค่า {channel.mention} แล้ว: เฉพาะ {role.mention} พูดได้ คนอื่นเข้าฟังได้อย่างเดียว",
        ephemeral=True
    )


@breakout_group.command(name="unsetspeakers", description="ยกเลิกการจำกัดสิทธิ์พูดของห้อง คืนค่า @everyone เป็นปกติ")
@has_mod_perms()
@app_commands.describe(channel="ห้องเสียงที่ต้องการยกเลิกการจำกัด")
async def breakout_unsetspeakers(interaction: discord.Interaction, channel: discord.VoiceChannel):
    try:
        await channel.set_permissions(
            interaction.guild.default_role, overwrite=None,
            reason=f"ยกเลิกจำกัดสิทธิ์พูดโดย {interaction.user}"
        )
    except discord.Forbidden:
        return await interaction.response.send_message("❌ บอทไม่มีสิทธิ์แก้ไข permission ห้องนี้", ephemeral=True)

    await interaction.response.send_message(
        f"🔊 ยกเลิกการจำกัดสิทธิ์พูดของ {channel.mention} แล้ว (บทบาทที่เคยได้รับสิทธิ์พูดเฉพาะยังคงอยู่ ลบเองผ่าน Discord ได้ถ้าไม่ต้องการแล้ว)",
        ephemeral=True
    )


tree.add_command(breakout_group)

# Stage Channels
stage_group = app_commands.Group(name="stage", description="จัดการ Stage Channel (ห้องกระจายเสียง)")

@stage_group.command(name="create", description="สร้าง Stage Channel (ห้องกระจายเสียง)")
@has_mod_perms()
@app_commands.describe(
    name="ชื่อห้องกระจายเสียง",
    topic="หัวข้อเวที (ไม่บังคับ)",
    category="หมวดหมู่ที่จะใส่ห้อง (ไม่บังคับ)"
)
async def stage_create(
    interaction: discord.Interaction,
    name: str,
    topic: str = None,
    category: discord.CategoryChannel | None = None
):
    try:
        channel = await interaction.guild.create_stage_channel(
            name=name,
            category=category,
            reason=f"สร้างโดย {interaction.user}"
        )
        if topic:
            try:
                await channel.edit(topic=topic, reason=f"ตั้งหัวข้อโดย {interaction.user}")
            except Exception:
                pass
        embed = discord.Embed(
            title="📢 สร้าง Stage Channel แล้ว",
            color=0xFEE75C,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="ชื่อ", value=channel.mention)
        if topic:
            embed.add_field(name="หัวข้อ", value=topic, inline=False)
        if category:
            embed.add_field(name="หมวดหมู่", value=category.name)
        embed.set_footer(text=f"โดย {interaction.user}")
        await interaction.response.send_message(embed=embed)
        await send_log(interaction.guild, embed)
    except discord.Forbidden:
        await interaction.response.send_message("❌ บอทไม่มีสิทธิ์สร้าง Stage Channel (ต้องการ Manage Channels)", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ ล้มเหลว: {e}", ephemeral=True)

@stage_group.command(name="delete", description="ลบ Stage Channel")
@has_mod_perms()
@app_commands.describe(channel="ห้อง Stage ที่ต้องการลบ")
async def stage_delete(interaction: discord.Interaction, channel: discord.StageChannel):
    name = channel.name
    try:
        await channel.delete(reason=f"ลบโดย {interaction.user}")
        embed = discord.Embed(
            title="🗑️ ลบ Stage Channel แล้ว",
            description=f"ลบห้อง `{name}` เรียบร้อย",
            color=0xED4245,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text=f"โดย {interaction.user}")
        await interaction.response.send_message(embed=embed)
        await send_log(interaction.guild, embed)
    except discord.Forbidden:
        await interaction.response.send_message("❌ บอทไม่มีสิทธิ์ลบห้องนี้", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ ล้มเหลว: {e}", ephemeral=True)

@stage_group.command(name="topic", description="ตั้งหัวข้อเวทีของ Stage Channel")
@has_mod_perms()
@app_commands.describe(channel="ห้อง Stage", topic="หัวข้อใหม่")
async def stage_topic(interaction: discord.Interaction, channel: discord.StageChannel, topic: str):
    try:
        await channel.edit(topic=topic, reason=f"โดย {interaction.user}")
        await interaction.response.send_message(
            f"✅ ตั้งหัวข้อ `{channel.name}` เป็น **{topic}** แล้ว",
            ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(f"❌ ล้มเหลว: {e}", ephemeral=True)

@stage_group.command(name="rename", description="เปลี่ยนชื่อ Stage Channel")
@has_mod_perms()
@app_commands.describe(channel="ห้อง Stage", new_name="ชื่อใหม่")
async def stage_rename(interaction: discord.Interaction, channel: discord.StageChannel, new_name: str):
    old = channel.name
    try:
        await channel.edit(name=new_name, reason=f"โดย {interaction.user}")
        await interaction.response.send_message(
            f"✅ เปลี่ยนชื่อ `{old}` → `{new_name}` แล้ว",
            ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(f"❌ ล้มเหลว: {e}", ephemeral=True)

tree.add_command(stage_group)

# Invite
@tree.command(name="invite", description="รับลิงก์เชิญบอทเข้าเซิร์ฟเวอร์ (พร้อม permission ครบ)")
async def invite_cmd(interaction: discord.Interaction):
    perms = discord.Permissions(
        kick_members=True,
        ban_members=True,
        manage_channels=True,
        manage_roles=True,
        moderate_members=True,
        view_channel=True,
        send_messages=True,
        manage_messages=True,
        embed_links=True,
        attach_files=True,
        read_message_history=True,
        add_reactions=True,
        use_external_emojis=True,
    )
    url = discord.utils.oauth_url(
        client_id=str(bot.user.id),
        permissions=perms,
        scopes=("bot", "applications.commands"),
    )
    embed = discord.Embed(title="เชิญ SoLARLIT เข้าเซิร์ฟเวอร์", color=0x5865F2)
    embed.description = f"[คลิกที่นี่เพื่อเชิญบอท]({url})"
    embed.add_field(name="Permissions ที่ขอ", value=(
        "Kick/Ban Members • Manage Channels\n"
        "Manage Roles • Moderate Members\n"
        "Send Messages • Manage Messages\n"
        "Embed Links • Read History • Add Reactions"
    ), inline=False)
    embed.set_footer(text="ต้องการ Manage Channels สำหรับคำสั่ง /voice และ /stage")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Ping
@tree.command(name="ping", description="ตรวจสอบสถานะและความหน่วงของบอท")
async def ping_cmd(interaction: discord.Interaction):
    latency_ms = round(bot.latency * 1000)
    embed = discord.Embed(title="🏓 Pong!", color=0x5865F2)
    embed.add_field(name="ความหน่วง (Latency)", value=f"`{latency_ms} ms`", inline=True)
    embed.add_field(name="สถานะ", value="`ออนไลน์ ✅`", inline=True)
    embed.set_footer(text="SoLARLIT Bot")
    await interaction.response.send_message(embed=embed)

# Help
@tree.command(name="help", description="ดูคำสั่งทั้งหมด")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="SoLARLIT Bot — คำสั่งทั้งหมด", color=0x5865F2)
    embed.add_field(name="/ping", value="ตรวจสอบสถานะและความหน่วงของบอท", inline=False)
    embed.add_field(name="/invite", value="รับลิงก์เชิญบอทพร้อม permission ครบ", inline=False)
    embed.add_field(name="/mod", value="kick • ban • timeout • warn • warnings • clear", inline=False)
    embed.add_field(name="/welcome", value="channel • message • leave", inline=False)
    embed.add_field(name="/automod", value="toggle • anti_invite • anti_mention_spam • addword • removeword • listwords", inline=False)
    embed.add_field(name="/customcommand", value="add • remove • list • prefix", inline=False)
    embed.add_field(name="/reactionrole", value="add • remove", inline=False)
    embed.add_field(name="/queue", value="join • leave • list • book • unbook • bookings • clear • board", inline=False)
    embed.add_field(name="/voice", value="create • delete • limit • rename — จัดการห้องเสียง", inline=False)
    embed.add_field(name="/breakout", value="start • move • recall • setowner • setspeakers • unsetspeakers", inline=False)
    embed.add_field(name="/stage", value="create • delete • topic • rename — จัดการห้องกระจายเสียง", inline=False)
    embed.add_field(name="/supportpanel panel", value="โพสต์แผงช่วยเหลือ", inline=False)
    embed.add_field(name="/settings logchannel", value="ตั้งช่อง log", inline=False)
    embed.add_field(name="/settings supportrole", value="ตั้ง role ที่ปิงตอนมีคนขอความช่วยเหลือใน Support Panel", inline=False)
    embed.add_field(name="/settings dashboardchannel", value="ตั้งช่องปักหมุดกระดานรวมคำสั่งบอททุกตัว (สำหรับคนใหม่)", inline=False)
    embed.add_field(name="/dashboard", value="แสดงกระดานรวมบอททั้งหมด พร้อมคำสั่งเริ่มต้นง่ายๆ", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────
# /dashboard — กระดานรวมบอททั้งหมดในเซิร์ฟเวอร์ พร้อมรูปและคำสั่งของแต่ละตัว
# ─────────────────────────────────────────────

BOT_DASHBOARD = [
    {
        "name_match": "ใจปู",
        "title": "🎫 ใจปู — ระบบคิว, Moderation, Breakout Rooms",
        "color": 0x5865F2,
        "commands": [
            "/queue — join, leave, list, book, unbook, bookings, clear, board",
            "/breakout — start, move, recall, setowner, setspeakers, unsetspeakers",
            "/voice — create, delete, limit, rename",
            "/mod — kick, ban, timeout, warn, warnings, clear",
            "/welcome, /automod, /customcommand, /reactionrole",
            "/stage, /supportpanel, /settings, /invite, /ping, /help",
        ],
    },
    {
        "name_match": "ยาม",
        "title": "🎙️ ยาม — ติดตามห้องเสียง",
        "color": 0x57F287,
        "commands": [
            "/track — add, remove, list",
            "/voice — now, stats, export, graph",
            "/setup-introduction, /setup-jobs, /my-profile, /edit-profile",
            "/player-search, /player-list, /player-remove",
            "/ping, /help",
        ],
    },
    {
        "name_match": "หัวหน้า",
        "title": "🎧 หัวหน้า — Voice Relay (บอทฟัง)",
        "color": 0xFEE75C,
        "commands": [
            "/relay — start, addtarget, removetarget, stop, status",
            "/relay — bindlistener, bindspeaker, unbind",
        ],
    },
]

SPEAKER_BOT_NAMES = ["ลูกน้อง", "ลูกน้อง 1", "ลูกน้อง 2", "ลูกน้อง 3", "ลูกน้อง 4"]

# คำสั่งที่ใช้บ่อยที่สุด อธิบายเป็นภาษาพูดง่ายๆ สำหรับคนที่ไม่เคยใช้บอทมาก่อน
# (ต่างจาก BOT_DASHBOARD ด้านบนที่เป็น syntax ดิบสำหรับคนที่คุ้นแล้ว)
BEGINNER_COMMANDS = [
    ("/help", "ดูคำสั่งทั้งหมดของบอทแบบละเอียด"),
    ("/ping", "เช็คว่าบอทออนไลน์อยู่ไหม"),
    ("/queue join", "เข้าคิว"),
    ("/queue list", "ดูว่าตอนนี้คิวถึงใครแล้ว"),
    ("ปุ่มในห้อง Support Panel", "ต้องการความช่วยเหลือ/เรียกแอดมิน กดปุ่มได้เลยไม่ต้องพิมพ์คำสั่ง"),
]


def build_dashboard_embeds(guild: discord.Guild) -> list[discord.Embed]:
    """
    สร้างชุด embed กระดานรวมบอททั้งหมด — ใช้ร่วมกันทั้งจาก /dashboard (เรียกเองครั้งเดียว)
    และ refresh_dashboard_board (auto-refresh ตอนบอท ready ถ้าตั้งช่องปักหมุดไว้)
    """
    embeds = []

    beginner_embed = discord.Embed(
        title="👋 เริ่มต้นใช้งานง่ายๆ (สำหรับคนใหม่)",
        description=(
            "\n".join(f"`{cmd}` — {desc}" for cmd, desc in BEGINNER_COMMANDS)
            + "\n\nดูคำสั่งแบบเต็มของแต่ละบอทได้ด้านล่าง 👇"
        ),
        color=0x2ECC71,
    )
    embeds.append(beginner_embed)

    for entry in BOT_DASHBOARD:
        member = discord.utils.find(
            lambda m: m.bot and m.display_name == entry["name_match"],
            guild.members
        )
        embed = discord.Embed(
            title=entry["title"],
            description="\n".join(f"`{c}`" for c in entry["commands"]),
            color=entry["color"]
        )
        if member:
            embed.set_thumbnail(url=member.display_avatar.url)
            status = "🟢 ออนไลน์" if member.status != discord.Status.offline else "🔴 ออฟไลน์"
            embed.set_footer(text=f"{member.display_name} • {status}")
        else:
            embed.set_footer(text="⚠️ ยังไม่พบบอทตัวนี้ในเซิร์ฟเวอร์ (invite แล้วหรือยัง?)")
        embeds.append(embed)

    # ทีมบอทพูด (speaker bots) รวมเป็น embed เดียว — คุมผ่าน /relay ของบอท "หัวหน้า" เท่านั้น ไม่มีคำสั่งของตัวเอง
    speaker_lines = []
    first_speaker_avatar = None
    for name in SPEAKER_BOT_NAMES:
        m = discord.utils.find(lambda mm: mm.bot and mm.display_name == name, guild.members)
        if m:
            status = "🟢" if m.status != discord.Status.offline else "🔴"
            speaker_lines.append(f"{status} {m.mention}")
            if first_speaker_avatar is None:
                first_speaker_avatar = m.display_avatar.url
        else:
            speaker_lines.append(f"⚫ {name} (ยังไม่ได้ invite)")

    speaker_embed = discord.Embed(
        title="🔊 ทีมบอทพูด — Voice Relay Speakers",
        description="\n".join(speaker_lines) + "\n\nควบคุมผ่านคำสั่ง `/relay` ของบอท **หัวหน้า** เท่านั้น ไม่มีคำสั่งของตัวเอง",
        color=0xEB459E
    )
    if first_speaker_avatar:
        speaker_embed.set_thumbnail(url=first_speaker_avatar)
    embeds.append(speaker_embed)

    return embeds[:10]  # Discord จำกัดจำนวน embed ต่อข้อความไว้ที่ 10


async def refresh_dashboard_board(guild: discord.Guild, channel_id: int):
    """
    โพสต์/อัปเดตกระดานรวมบอทในช่องที่ตั้งไว้ผ่าน /settings dashboardchannel
    แก้ข้อความเดิมถ้ามีอยู่แล้ว (ไม่โพสต์ใหม่ทุกครั้งที่บอท restart กันห้องรก) ปักหมุดให้ตอนโพสต์ครั้งแรก
    """
    channel = guild.get_channel(channel_id)
    if not channel:
        return
    embeds = build_dashboard_embeds(guild)
    pool = await db.get_pool()

    row = await pool.fetchrow("SELECT message_id FROM dashboard_board WHERE guild_id = $1", guild.id)
    if row:
        try:
            msg = await channel.fetch_message(row["message_id"])
            await msg.edit(embeds=embeds)
            return
        except discord.NotFound:
            pass  # ข้อความเดิมถูกลบไปแล้ว โพสต์ใหม่ด้านล่างแทน
        except Exception as e:
            log.error(f"แก้ไขกระดานรวมบอทเดิมไม่สำเร็จ: {e}")
            return

    try:
        msg = await channel.send(embeds=embeds)
        try:
            await msg.pin()
        except Exception:
            pass  # ไม่มีสิทธิ์ปักหมุดก็ไม่เป็นไร โพสต์ไว้เฉยๆ ยังใช้ได้
    except Exception as e:
        log.error(f"โพสต์กระดานรวมบอทใหม่ไม่สำเร็จ: {e}")
        return

    await pool.execute(
        """
        INSERT INTO dashboard_board (guild_id, channel_id, message_id)
        VALUES ($1, $2, $3)
        ON CONFLICT (guild_id) DO UPDATE SET channel_id = $2, message_id = $3
        """,
        guild.id, channel.id, msg.id
    )


async def refresh_all_dashboard_boards():
    """เรียกตอนบอท ready — รีเฟรชกระดานรวมบอทของทุกเซิร์ฟเวอร์ที่ตั้งช่องไว้"""
    pool = await db.get_pool()
    rows = await pool.fetch("SELECT guild_id, dashboard_channel FROM settings WHERE dashboard_channel IS NOT NULL")
    for row in rows:
        guild = bot.get_guild(row["guild_id"])
        if guild:
            await refresh_dashboard_board(guild, row["dashboard_channel"])


@tree.command(name="dashboard", description="แสดงกระดานรวมบอททั้งหมดในเซิร์ฟเวอร์ พร้อมรูปและคำสั่งของแต่ละตัว")
async def dashboard_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    embeds = build_dashboard_embeds(interaction.guild)
    await interaction.followup.send(embeds=embeds)

@bot.event
async def setup_hook():
    bot.add_view(QueueFullBoardView())
    bot.add_view(SupportView())

async def main():
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
