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

# ป้องกัน Anti-raid: เก็บเวลาการเข้าร่วมล่าสุดของแต่ละกิลด์ไว้ในแรม (ไม่ต้องกิน DB ทุก join)
_recent_joins: dict[int, list[datetime]] = {}

# Anti-spam: (guild_id, user_id) -> [(เวลา, message, เนื้อหา)]
_recent_msgs: dict[tuple[int, int], list[tuple[datetime, discord.Message, str]]] = {}

# Anti-raid (identity check): user_id -> เวลาที่เปลี่ยนชื่อ/avatar ล่าสุดแต่ละครั้ง (global, ไม่แยกกิลด์
# เพราะ discord.py ยิง on_user_update ระดับ user ไม่ใช่ระดับกิลด์)
_recent_identity_changes: dict[int, list[datetime]] = {}

async def check_spam(message: discord.Message, settings: dict) -> bool:
    """ตรวจจับคนยิงข้อความรัว/ข้อความซ้ำ/ยิงหลายห้อง คืน True ถ้าจัดการแล้ว"""
    member = message.author
    if not isinstance(member, discord.Member):
        return False
    perms = member.guild_permissions
    if perms.administrator or perms.manage_messages:
        return False
    limit = settings.get("spam_msg_limit") or 5
    window = settings.get("spam_window_sec") or 5
    now = datetime.now(timezone.utc)
    key = (message.guild.id, member.id)
    content = message.content.strip().lower()
    history = [e for e in _recent_msgs.get(key, []) if (now - e[0]).total_seconds() <= window]
    history.append((now, message, content))
    _recent_msgs[key] = history

    reason = None
    if len(history) >= limit:
        reason = f"ส่งข้อความรัว {len(history)} ข้อความใน {window} วินาที"
    elif content:
        same = [e for e in history if e[2] == content]
        if len(same) >= 3:
            reason = "ส่งข้อความซ้ำๆ"
            if len({e[1].channel.id for e in same}) >= 3:
                reason = "ส่งข้อความเดียวกันกระจายหลายห้อง"
    if not reason:
        return False

    _recent_msgs.pop(key, None)
    for _, m, _c in history:
        try:
            await m.delete()
        except Exception:
            pass
    action = settings.get("spam_action") or "timeout"
    result = "ลบข้อความ"
    try:
        if action == "timeout":
            minutes = settings.get("spam_timeout_min") or 10
            await member.timeout(timedelta(minutes=minutes), reason=f"Anti-spam: {reason}")
            result = f"ลบข้อความ + timeout {minutes} นาที"
        elif action == "kick":
            await member.kick(reason=f"Anti-spam: {reason}")
            result = "ลบข้อความ + เตะออก"
    except Exception:
        result += " (สั่งลงโทษไม่สำเร็จ ตรวจสอบสิทธิ์/ลำดับ role ของบอท)"
    try:
        pool = await db.get_pool()
        await pool.execute(
            "INSERT INTO warnings (guild_id, user_id, reason, moderator_id) VALUES ($1, $2, $3, $4)",
            message.guild.id, member.id, f"Anti-spam: {reason}", bot.user.id,
        )
    except Exception:
        pass
    try:
        await message.channel.send(f"{member.mention} ตรวจพบสแปม: {reason}", delete_after=8)
    except Exception:
        pass
    embed = discord.Embed(title="🚨 ตรวจพบคนยิงสแปม", color=0xED4245, timestamp=now)
    embed.add_field(name="ผู้ใช้", value=f"{member.mention} ({member.id})", inline=False)
    embed.add_field(name="สาเหตุ", value=reason, inline=False)
    embed.add_field(name="การจัดการ", value=result, inline=False)
    await send_log(message.guild, embed)
    return True

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
    if not check_board_schedules.is_running():
        check_board_schedules.start()
    try:
        await refresh_all_dashboard_boards()
    except Exception as e:
        log.error(f"Refresh dashboard boards failed: {e}")

async def _antiraid_punish(member: discord.Member, action: str, reason: str):
    """ลงโทษสมาชิกตามค่า antiraid_action ที่ตั้งไว้ (kick / ban / timeout)"""
    try:
        if action == "ban":
            await member.ban(reason=reason, delete_message_days=0)
        elif action == "timeout":
            until = datetime.now(timezone.utc) + timedelta(hours=1)
            await member.timeout(until, reason=reason)
        else:
            await member.kick(reason=reason)
    except Exception:
        pass


async def _handle_antiraid(member: discord.Member, settings: dict) -> bool:
    """
    ตรวจสมาชิกที่เพิ่งเข้า ตาม antiraid settings ของกิลด์
    คืนค่า True ถ้ามีการจัดการ (ลงโทษ/ถูกบล็อก) แล้ว ไม่ต้องทำ welcome message ต่อ
    """
    if not settings.get("antiraid_enabled"):
        return False

    guild = member.guild
    action = settings.get("antiraid_action") or "kick"

    # กิลด์กำลังอยู่ในสถานะ lockdown (ตั้งมือหรือเพิ่งตรวจพบ raid) -> บล็อกสมาชิกใหม่ทั้งหมด
    if settings.get("antiraid_lockdown"):
        await _antiraid_punish(member, action, "Anti-raid: เซิร์ฟเวอร์อยู่ในสถานะ lockdown")
        await send_log(guild, discord.Embed(
            title="🛡️ Anti-raid: บล็อกสมาชิกใหม่ (lockdown)",
            description=f"{member.mention} (`{member.id}`) ถูก{_action_th(action)} เพราะเซิร์ฟเวอร์ปิดรับสมาชิกใหม่ชั่วคราว",
            color=0xFF3B3B, timestamp=datetime.now(timezone.utc)
        ))
        return True

    # ตรวจอายุบัญชี Discord ของสมาชิก
    min_age_hours = settings.get("antiraid_min_account_age_hours") or 0
    if min_age_hours > 0:
        account_age = datetime.now(timezone.utc) - member.created_at
        if account_age < timedelta(hours=min_age_hours):
            await _antiraid_punish(member, action, f"Anti-raid: บัญชีอายุต่ำกว่า {min_age_hours} ชม.")
            await send_log(guild, discord.Embed(
                title="🛡️ Anti-raid: บัญชีใหม่เกินไป",
                description=(f"{member.mention} (`{member.id}`) ถูก{_action_th(action)}\n"
                              f"บัญชีสร้างเมื่อ {member.created_at.strftime('%Y-%m-%d %H:%M UTC')} "
                              f"(อายุ < {min_age_hours} ชม.)"),
                color=0xFF9F1C, timestamp=datetime.now(timezone.utc)
            ))
            return True

    # ตรวจสมาชิกที่เพิ่งเปลี่ยนชื่อ/avatar ถี่ผิดปกติก่อนเข้าเซิร์ฟเวอร์ (มักเป็นบอทที่สุ่มสร้างตัวตนใหม่ก่อนเข้ามา raid)
    if settings.get("antiraid_identity_check"):
        id_limit = settings.get("antiraid_identity_limit") or 2
        id_window = settings.get("antiraid_identity_window_sec") or 300
        now_id = datetime.now(timezone.utc)
        id_cutoff = now_id - timedelta(seconds=id_window)
        changes = [t for t in _recent_identity_changes.get(member.id, []) if t > id_cutoff]
        if len(changes) >= id_limit:
            await _antiraid_punish(member, action, f"Anti-raid: เปลี่ยนชื่อ/avatar {len(changes)} ครั้งใน {id_window} วินาทีก่อนเข้าเซิร์ฟเวอร์")
            await send_log(guild, discord.Embed(
                title="🛡️ Anti-raid: เปลี่ยนชื่อ/avatar ถี่ผิดปกติ",
                description=(f"{member.mention} (`{member.id}`) ถูก{_action_th(action)}\n"
                              f"เปลี่ยนชื่อ/avatar {len(changes)} ครั้ง ภายใน {id_window} วินาที ก่อนเข้าเซิร์ฟเวอร์"),
                color=0xFF9F1C, timestamp=now_id
            ))
            return True

    # ตรวจ join-burst: มีคนเข้าพร้อมกันจำนวนมากในช่วงเวลาสั้นๆ หรือไม่
    threshold = settings.get("antiraid_join_threshold") or 5
    window = settings.get("antiraid_join_window_seconds") or 10
    now = datetime.now(timezone.utc)
    joins = _recent_joins.setdefault(guild.id, [])
    joins.append(now)
    cutoff = now - timedelta(seconds=window)
    joins[:] = [t for t in joins if t > cutoff]

    if len(joins) >= threshold:
        pool = await db.get_pool()
        await pool.execute("""
            INSERT INTO settings (guild_id, antiraid_lockdown) VALUES ($1, TRUE)
            ON CONFLICT (guild_id) DO UPDATE SET antiraid_lockdown = TRUE
        """, guild.id)
        await send_log(guild, discord.Embed(
            title="🚨 Anti-raid: ตรวจพบการเข้าพร้อมกันจำนวนมาก (RAID)",
            description=(f"พบ {len(joins)} คนเข้าร่วมภายใน {window} วินาที "
                          f"(เกินเกณฑ์ {threshold}) — เปิด **lockdown** อัตโนมัติแล้ว\n"
                          f"ใช้ `/antiraid unlock` เมื่อแน่ใจว่าปลอดภัยแล้ว"),
            color=0xFF0000, timestamp=datetime.now(timezone.utc)
        ))
        return False  # ตัวที่ทำให้ครบ threshold เองไม่ลงโทษย้อนหลัง แต่คนต่อไปจะถูก lockdown บล็อก

    return False


def _action_th(action: str) -> str:
    return {"kick": "เตะ", "ban": "แบน", "timeout": "timeout"}.get(action, "เตะ")


@bot.event
async def on_member_join(member: discord.Member):
    settings = await get_settings(member.guild.id)

    handled = await _handle_antiraid(member, settings)
    if handled:
        return

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
async def on_user_update(before: discord.User, after: discord.User):
    """ดักทุกครั้งที่ username หรือ avatar ของใครก็ตามเปลี่ยน (ระดับ Discord user ไม่ใช่ระดับกิลด์)
    เก็บเวลาไว้ให้ _handle_antiraid เช็คตอนเข้าเซิร์ฟเวอร์ใหม่ว่าเปลี่ยนถี่ผิดปกติหรือไม่"""
    if before.name == after.name and before.avatar == after.avatar:
        return
    now = datetime.now(timezone.utc)
    history = _recent_identity_changes.setdefault(after.id, [])
    history.append(now)
    # กันแรมบวมสะสม: ตัดทิ้งเกิน 1 ชม. ไว้พอ (เกณฑ์การตรวจจริงใช้ antiraid_identity_window_sec ที่สั้นกว่านี้มาก)
    cutoff = now - timedelta(hours=1)
    history[:] = [t for t in history if t > cutoff]


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
        if settings.get("anti_spam"):
            if await check_spam(message, settings):
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

@automod_group.command(name="anti_spam", description="เปิด/ปิด ตรวจจับคนยิงสแปมข้อความ")
@has_mod_perms()
@app_commands.describe(
    limit="จำนวนข้อความที่ถือว่ายิงรัว (default 5)",
    window="ภายในกี่วินาที (default 5)",
    action="สิ่งที่ทำเมื่อจับได้",
    timeout_min="นาทีที่ timeout (ใช้เมื่อ action=timeout)",
)
@app_commands.choices(action=[
    app_commands.Choice(name="ลบข้อความอย่างเดียว", value="delete"),
    app_commands.Choice(name="ลบ + timeout", value="timeout"),
    app_commands.Choice(name="ลบ + เตะออก", value="kick"),
])
async def automod_anti_spam(
    interaction: discord.Interaction,
    limit: app_commands.Range[int, 3, 20] = 5,
    window: app_commands.Range[int, 2, 30] = 5,
    action: app_commands.Choice[str] = None,
    timeout_min: app_commands.Range[int, 1, 10080] = 10,
):
    pool = await db.get_pool()
    settings = await get_settings(interaction.guild.id)
    new_val = not settings.get("anti_spam", False)
    act = action.value if action else "timeout"
    await pool.execute("""
        INSERT INTO settings (guild_id, anti_spam, spam_msg_limit, spam_window_sec, spam_action, spam_timeout_min)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (guild_id) DO UPDATE SET anti_spam = $2, spam_msg_limit = $3,
            spam_window_sec = $4, spam_action = $5, spam_timeout_min = $6
    """, interaction.guild.id, new_val, limit, window, act, timeout_min)
    await interaction.response.send_message(
        f"Anti-spam {'เปิด' if new_val else 'ปิด'} ({limit} ข้อความ/{window} วิ, action={act})"
        "\nหมายเหตุ: ต้องเปิด `/automod toggle` ด้วย ระบบจึงทำงาน",
        ephemeral=True,
    )

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

# ป้องกัน Anti-raid
antiraid_group = app_commands.Group(name="antiraid", description="ระบบป้องกัน raid (บอทปลอม/สแปมเข้าเซิร์ฟเวอร์พร้อมกัน)")

@antiraid_group.command(name="toggle", description="เปิด/ปิดระบบป้องกัน raid")
@has_mod_perms()
async def antiraid_toggle(interaction: discord.Interaction):
    pool = await db.get_pool()
    settings = await get_settings(interaction.guild.id)
    new_val = not settings.get("antiraid_enabled", False)
    await pool.execute("""
        INSERT INTO settings (guild_id, antiraid_enabled) VALUES ($1, $2)
        ON CONFLICT (guild_id) DO UPDATE SET antiraid_enabled = $2
    """, interaction.guild.id, new_val)
    await interaction.response.send_message(f"ระบบป้องกัน raid {'เปิด' if new_val else 'ปิด'} แล้ว", ephemeral=True)

@antiraid_group.command(name="threshold", description="ตั้งจำนวนคนเข้าพร้อมกันที่ถือว่าเป็น raid")
@has_mod_perms()
@app_commands.describe(count="จำนวนคน", seconds="ภายในกี่วินาที")
async def antiraid_threshold(interaction: discord.Interaction, count: app_commands.Range[int, 2, 50] = 5, seconds: app_commands.Range[int, 3, 120] = 10):
    pool = await db.get_pool()
    await pool.execute("""
        INSERT INTO settings (guild_id, antiraid_join_threshold, antiraid_join_window_seconds) VALUES ($1, $2, $3)
        ON CONFLICT (guild_id) DO UPDATE SET antiraid_join_threshold = $2, antiraid_join_window_seconds = $3
    """, interaction.guild.id, count, seconds)
    await interaction.response.send_message(f"ตั้งเกณฑ์ raid เป็น {count} คน ภายใน {seconds} วินาทีแล้ว", ephemeral=True)

@antiraid_group.command(name="minage", description="ตั้งอายุบัญชี Discord ขั้นต่ำที่อนุญาตให้เข้าได้")
@has_mod_perms()
@app_commands.describe(hours="จำนวนชั่วโมง (0 = ไม่ตรวจอายุบัญชี)")
async def antiraid_minage(interaction: discord.Interaction, hours: app_commands.Range[int, 0, 8760] = 24):
    pool = await db.get_pool()
    await pool.execute("""
        INSERT INTO settings (guild_id, antiraid_min_account_age_hours) VALUES ($1, $2)
        ON CONFLICT (guild_id) DO UPDATE SET antiraid_min_account_age_hours = $2
    """, interaction.guild.id, hours)
    await interaction.response.send_message(f"ตั้งอายุบัญชีขั้นต่ำเป็น {hours} ชม. แล้ว", ephemeral=True)

@antiraid_group.command(name="identity", description="เปิด/ปิดการตรวจสมาชิกที่เพิ่งเปลี่ยนชื่อ/avatar ถี่ผิดปกติก่อนเข้าเซิร์ฟเวอร์")
@has_mod_perms()
@app_commands.describe(enabled="เปิดหรือปิด", count="เปลี่ยนกี่ครั้งถึงถือว่าผิดปกติ", seconds="ภายในกี่วินาทีก่อนเข้าเซิร์ฟเวอร์")
async def antiraid_identity(interaction: discord.Interaction, enabled: bool, count: app_commands.Range[int, 1, 20] = 2, seconds: app_commands.Range[int, 10, 3600] = 300):
    pool = await db.get_pool()
    await pool.execute("""
        INSERT INTO settings (guild_id, antiraid_identity_check, antiraid_identity_limit, antiraid_identity_window_sec)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (guild_id) DO UPDATE SET antiraid_identity_check = $2, antiraid_identity_limit = $3, antiraid_identity_window_sec = $4
    """, interaction.guild.id, enabled, count, seconds)
    status = "เปิด" if enabled else "ปิด"
    await interaction.response.send_message(f"การตรวจเปลี่ยนชื่อ/avatar ถี่ผิดปกติ {status} แล้ว (เกณฑ์ {count} ครั้ง / {seconds} วินาที)", ephemeral=True)

@antiraid_group.command(name="action", description="ตั้งการกระทำต่อสมาชิกที่ถูกจับว่าเป็น raid")
@has_mod_perms()
@app_commands.choices(action=[
    app_commands.Choice(name="เตะ (kick)", value="kick"),
    app_commands.Choice(name="แบน (ban)", value="ban"),
    app_commands.Choice(name="timeout 1 ชม.", value="timeout"),
])
async def antiraid_action(interaction: discord.Interaction, action: app_commands.Choice[str]):
    pool = await db.get_pool()
    await pool.execute("""
        INSERT INTO settings (guild_id, antiraid_action) VALUES ($1, $2)
        ON CONFLICT (guild_id) DO UPDATE SET antiraid_action = $2
    """, interaction.guild.id, action.value)
    await interaction.response.send_message(f"ตั้งการกระทำเป็น `{action.value}` แล้ว", ephemeral=True)

@antiraid_group.command(name="status", description="ดูสถานะระบบป้องกัน raid ปัจจุบัน")
@has_mod_perms()
async def antiraid_status(interaction: discord.Interaction):
    settings = await get_settings(interaction.guild.id)
    embed = discord.Embed(title="🛡️ สถานะ Anti-raid", color=0x3ED8C3, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="เปิดใช้งาน", value="✅ เปิด" if settings.get("antiraid_enabled") else "❌ ปิด", inline=True)
    embed.add_field(name="Lockdown", value="🔒 ล็อกอยู่" if settings.get("antiraid_lockdown") else "🔓 ปกติ", inline=True)
    embed.add_field(name="การกระทำ", value=_action_th(settings.get("antiraid_action") or "kick"), inline=True)
    embed.add_field(name="เกณฑ์ raid", value=f"{settings.get('antiraid_join_threshold') or 5} คน / {settings.get('antiraid_join_window_seconds') or 10} วินาที", inline=True)
    embed.add_field(name="อายุบัญชีขั้นต่ำ", value=f"{settings.get('antiraid_min_account_age_hours') or 0} ชม.", inline=True)
    id_status = "✅ เปิด" if settings.get("antiraid_identity_check") else "❌ ปิด"
    embed.add_field(
        name="ตรวจเปลี่ยนชื่อ/avatar ถี่",
        value=f"{id_status} ({settings.get('antiraid_identity_limit') or 2} ครั้ง / {settings.get('antiraid_identity_window_sec') or 300} วินาที)",
        inline=True,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@antiraid_group.command(name="lockdown", description="ปิดรับสมาชิกใหม่ทันที (บล็อกทุกคนที่เข้ามาจนกว่าจะ unlock)")
@has_mod_perms()
async def antiraid_lockdown(interaction: discord.Interaction):
    pool = await db.get_pool()
    await pool.execute("""
        INSERT INTO settings (guild_id, antiraid_lockdown) VALUES ($1, TRUE)
        ON CONFLICT (guild_id) DO UPDATE SET antiraid_lockdown = TRUE
    """, interaction.guild.id)
    await interaction.response.send_message("🔒 เปิด lockdown แล้ว — สมาชิกใหม่ทุกคนจะถูกบล็อกจนกว่าจะ `/antiraid unlock`", ephemeral=True)

@antiraid_group.command(name="unlock", description="ยกเลิก lockdown กลับมารับสมาชิกใหม่ตามปกติ")
@has_mod_perms()
async def antiraid_unlock(interaction: discord.Interaction):
    pool = await db.get_pool()
    await pool.execute("""
        INSERT INTO settings (guild_id, antiraid_lockdown) VALUES ($1, FALSE)
        ON CONFLICT (guild_id) DO UPDATE SET antiraid_lockdown = FALSE
    """, interaction.guild.id)
    _recent_joins.pop(interaction.guild.id, None)
    await interaction.response.send_message("🔓 ยกเลิก lockdown แล้ว", ephemeral=True)

tree.add_command(antiraid_group)

# Security: ตรวจสุขภาพเซิร์ฟเวอร์ + Anti-nuke
DANGEROUS_PERMS = [
    "administrator", "manage_guild", "manage_roles", "manage_channels",
    "manage_webhooks", "ban_members", "kick_members", "mention_everyone",
]
NUKE_ACTIONS = {
    discord.AuditLogAction.channel_delete: "ลบช่อง",
    discord.AuditLogAction.role_delete: "ลบ role",
    discord.AuditLogAction.ban: "แบนสมาชิก",
    discord.AuditLogAction.kick: "เตะสมาชิก",
    discord.AuditLogAction.webhook_create: "สร้าง webhook",
}
_nuke_events: dict[tuple[int, int], list[tuple[datetime, str]]] = {}

def _dangerous(perms: discord.Permissions) -> list[str]:
    return [p for p in DANGEROUS_PERMS if getattr(perms, p)]

@bot.event
async def on_audit_log_entry_create(entry: discord.AuditLogEntry):
    label = NUKE_ACTIONS.get(entry.action)
    guild = entry.guild
    actor = entry.user
    if not label or actor is None or actor.id in (bot.user.id, guild.owner_id):
        return
    settings = await get_settings(guild.id)
    if not settings.get("antinuke_enabled"):
        return
    pool = await db.get_pool()
    if await pool.fetchval("SELECT 1 FROM antinuke_whitelist WHERE guild_id=$1 AND user_id=$2", guild.id, actor.id):
        return
    limit = settings.get("antinuke_limit") or 3
    window = settings.get("antinuke_window_sec") or 30
    now = datetime.now(timezone.utc)
    key = (guild.id, actor.id)
    events = [e for e in _nuke_events.get(key, []) if (now - e[0]).total_seconds() <= window]
    events.append((now, label))
    _nuke_events[key] = events
    if len(events) < limit:
        return
    _nuke_events.pop(key, None)

    member = guild.get_member(actor.id)
    removed, failed = [], []
    if member:
        for role in member.roles:
            if role.is_default() or role.managed:
                continue
            if _dangerous(role.permissions):
                try:
                    await member.remove_roles(role, reason="Anti-nuke: พฤติกรรมทำลายเซิร์ฟเวอร์")
                    removed.append(role.name)
                except Exception:
                    failed.append(role.name)
    embed = discord.Embed(title="🛡️ Anti-nuke ทำงาน", color=0xED4245, timestamp=now)
    embed.add_field(name="ผู้กระทำ", value=f"{actor.mention} ({actor.id})", inline=False)
    embed.add_field(name="พฤติกรรม", value=", ".join(sorted({e[1] for e in events})) + f" ({len(events)} ครั้งใน {window} วิ)", inline=False)
    embed.add_field(name="ถอด role แล้ว", value=", ".join(removed) or "-", inline=False)
    if failed:
        embed.add_field(name="ถอดไม่สำเร็จ (role บอทต่ำกว่า/ไม่มีสิทธิ์)", value=", ".join(failed), inline=False)
    if not member:
        embed.add_field(name="หมายเหตุ", value="ไม่พบสมาชิกในเซิร์ฟเวอร์ (อาจออกไปแล้ว หรือเป็นบอท/แอปที่ถูกถอด)", inline=False)
    await send_log(guild, embed)

security_group = app_commands.Group(name="security", description="ตรวจสอบและป้องกันความปลอดภัยของเซิร์ฟเวอร์")

async def build_audit_embed(guild: discord.Guild, settings: dict) -> discord.Embed:
    high, mid, ok = [], [], []

    if guild.verification_level < discord.VerificationLevel.medium:
        mid.append(f"Verification level ต่ำ (`{guild.verification_level.name}`) แนะนำ medium ขึ้นไป")
    else:
        ok.append(f"Verification level: `{guild.verification_level.name}`")
    if guild.explicit_content_filter == discord.ContentFilter.disabled:
        mid.append("Explicit content filter ปิดอยู่")
    if guild.default_role.permissions.value:
        bad = _dangerous(guild.default_role.permissions)
        if bad:
            high.append(f"@everyone มีสิทธิ์อันตราย: {', '.join(bad)}")
    admin_roles = [r for r in guild.roles if r.permissions.administrator and not r.is_default() and not r.managed]
    for r in admin_roles:
        humans = [m for m in r.members if not m.bot and m.id != guild.owner_id]
        if humans:
            mid.append(f"role **{r.name}** มี Administrator ({len(humans)} คน)")
    for m in guild.members:
        if m.bot and m.guild_permissions.administrator and m.id != bot.user.id:
            mid.append(f"บอท {m.mention} มีสิทธิ์ Administrator")
    me = guild.me
    perms = me.guild_permissions
    if not (perms.administrator or perms.view_audit_log):
        mid.append("บอทไม่มีสิทธิ์ View Audit Log (Anti-nuke จะไม่ทำงาน)")
    if not (perms.administrator or (perms.moderate_members and perms.kick_members)):
        mid.append("บอทไม่มีสิทธิ์ Moderate Members/Kick Members (ลงโทษไม่ได้)")
    if me.top_role.position < max((r.position for r in guild.roles), default=0) - 3:
        mid.append(f"role ของบอทอยู่ต่ำ (ตำแหน่ง {me.top_role.position}) อาจลงโทษคนที่มี role สูงกว่าไม่ได้")
    if not settings.get("log_channel"):
        mid.append("ยังไม่ได้ตั้งห้อง log")
    for key, name in (("automod_enabled", "Automod"), ("antiraid_enabled", "Anti-raid"),
                      ("anti_spam", "Anti-spam"), ("antinuke_enabled", "Anti-nuke")):
        (ok if settings.get(key) else mid).append(f"{name}: {'เปิด' if settings.get(key) else 'ปิดอยู่'}")

    level = "🔴 เสี่ยงสูง" if high else ("🟠 ควรปรับปรุง" if len([x for x in mid if 'ปิด' in x or 'ต่ำ' in x or 'ไม่มี' in x]) else "🟢 ปลอดภัย")
    embed = discord.Embed(title=f"🔍 ผลตรวจความปลอดภัย — {guild.name}", description=f"ระดับ: **{level}**", color=0x5865F2)
    if high:
        embed.add_field(name="🔴 ความเสี่ยงสูง", value="\n".join(high)[:1024], inline=False)
    if mid:
        embed.add_field(name="🟠 ควรตรวจสอบ", value="\n".join(mid)[:1024], inline=False)
    if ok:
        embed.add_field(name="🟢 ปกติ", value="\n".join(ok)[:1024], inline=False)
    return embed

@security_group.command(name="audit", description="ตรวจสุขภาพความปลอดภัยของเซิร์ฟเวอร์ (อ่านอย่างเดียว)")
@has_mod_perms()
async def security_audit(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    settings = await get_settings(interaction.guild.id)
    await interaction.followup.send(embed=await build_audit_embed(interaction.guild, settings), ephemeral=True)

@security_group.command(name="antinuke", description="เปิด/ปิด Anti-nuke (ถอดสิทธิ์คนที่ลบช่อง/role หรือแบนคนจำนวนมาก)")
@has_mod_perms()
@app_commands.describe(limit="จำนวนครั้งที่ถือว่าผิดปกติ (default 3)", window="ภายในกี่วินาที (default 30)")
async def security_antinuke(
    interaction: discord.Interaction,
    limit: app_commands.Range[int, 2, 20] = 3,
    window: app_commands.Range[int, 5, 300] = 30,
):
    pool = await db.get_pool()
    settings = await get_settings(interaction.guild.id)
    new_val = not settings.get("antinuke_enabled", False)
    await pool.execute("""
        INSERT INTO settings (guild_id, antinuke_enabled, antinuke_limit, antinuke_window_sec)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (guild_id) DO UPDATE SET antinuke_enabled = $2, antinuke_limit = $3, antinuke_window_sec = $4
    """, interaction.guild.id, new_val, limit, window)
    await interaction.response.send_message(
        f"Anti-nuke {'เปิด' if new_val else 'ปิด'} ({limit} ครั้ง/{window} วิ)\n"
        "บอทต้องมีสิทธิ์ View Audit Log และ Manage Roles และ role บอทต้องอยู่สูงกว่าคนที่จะถูกถอดสิทธิ์",
        ephemeral=True,
    )

@security_group.command(name="whitelist", description="เพิ่ม/ลบผู้ใช้ที่ Anti-nuke จะไม่ตรวจ (เจ้าของเซิร์ฟเวอร์ถูกข้ามอยู่แล้ว)")
@has_mod_perms()
@app_commands.describe(user="ผู้ใช้", remove="ลบออกจาก whitelist")
async def security_whitelist(interaction: discord.Interaction, user: discord.Member, remove: bool = False):
    if interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("เฉพาะเจ้าของเซิร์ฟเวอร์เท่านั้นที่จัดการ whitelist ได้", ephemeral=True)
        return
    pool = await db.get_pool()
    if remove:
        await pool.execute("DELETE FROM antinuke_whitelist WHERE guild_id=$1 AND user_id=$2", interaction.guild.id, user.id)
        msg = f"ลบ {user.mention} ออกจาก whitelist แล้ว"
    else:
        await pool.execute("INSERT INTO antinuke_whitelist (guild_id, user_id) VALUES ($1,$2) ON CONFLICT DO NOTHING", interaction.guild.id, user.id)
        msg = f"เพิ่ม {user.mention} เข้า whitelist แล้ว"
    await interaction.response.send_message(msg, ephemeral=True)

# ป๊อบอัพแผงควบคุมความปลอดภัย: เลือกหมวดจาก dropdown + ปุ่มเปิด/ปิด
PANEL_CATEGORIES = {
    "overview": ("📋 ภาพรวม", None),
    "spam": ("💬 Anti-spam (ยิงข้อความรัว)", "anti_spam"),
    "raid": ("🚪 Anti-raid (คนเข้าพร้อมกัน)", "antiraid_enabled"),
    "nuke": ("💣 Anti-nuke (กันพังเซิร์ฟเวอร์)", "antinuke_enabled"),
    "automod": ("🧹 Automod (คำต้องห้าม/invite/mention)", "automod_enabled"),
    "audit": ("🔍 ตรวจสุขภาพเซิร์ฟเวอร์", None),
}
_ACTION_TH = {"delete": "ลบข้อความอย่างเดียว", "timeout": "ลบ + timeout", "kick": "ลบ + เตะออก"}

def _onoff(v) -> str:
    return "🟢 เปิด" if v else "🔴 ปิด"

class SecurityPanel(discord.ui.View):
    def __init__(self, guild: discord.Guild, user_id: int):
        super().__init__(timeout=300)
        self.guild = guild
        self.user_id = user_id
        self.category = "overview"
        select = discord.ui.Select(
            placeholder="เลือกหมวดการป้องกัน...",
            options=[discord.SelectOption(label=label, value=key, default=(key == "overview"))
                     for key, (label, _col) in PANEL_CATEGORIES.items()],
        )
        select.callback = self.on_select
        self.select = select
        self.add_item(select)
        self.toggle_btn = discord.ui.Button(label="เปิด/ปิด", style=discord.ButtonStyle.primary)
        self.toggle_btn.callback = self.on_toggle
        self.add_item(self.toggle_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("แผงนี้เป็นของคนที่เรียกใช้เท่านั้น", ephemeral=True)
            return False
        return True

    async def build(self) -> discord.Embed:
        s = await get_settings(self.guild.id)
        label, col = PANEL_CATEGORIES[self.category]
        if self.category == "audit":
            return await build_audit_embed(self.guild, s)
        embed = discord.Embed(title=f"🛡️ {label}", color=0x5865F2)
        if self.category == "overview":
            for key, (lbl, c) in PANEL_CATEGORIES.items():
                if c:
                    embed.add_field(name=lbl, value=_onoff(s.get(c)), inline=False)
            embed.set_footer(text="เลือกหมวดจากเมนูด้านล่างเพื่อดูรายละเอียดและเปิด/ปิด")
        elif self.category == "spam":
            embed.description = (
                f"สถานะ: {_onoff(s.get('anti_spam'))}\n"
                f"เกณฑ์: {s.get('spam_msg_limit') or 5} ข้อความ / {s.get('spam_window_sec') or 5} วินาที\n"
                f"เมื่อจับได้: {_ACTION_TH.get(s.get('spam_action') or 'timeout')}"
                f" ({s.get('spam_timeout_min') or 10} นาที)\n"
                "ปรับค่าด้วย `/automod anti_spam`\n⚠️ ต้องเปิด Automod ด้วยจึงทำงาน")
        elif self.category == "raid":
            embed.description = (
                f"สถานะ: {_onoff(s.get('antiraid_enabled'))}\n"
                f"เกณฑ์: {s.get('antiraid_join_threshold') or 5} คน / {s.get('antiraid_join_window_seconds') or 10} วินาที\n"
                f"อายุบัญชีขั้นต่ำ: {s.get('antiraid_min_account_age_hours') or 24} ชม.\n"
                f"Lockdown: {_onoff(s.get('antiraid_lockdown'))}\n"
                "ปรับค่าด้วย `/antiraid ...`")
        elif self.category == "nuke":
            embed.description = (
                f"สถานะ: {_onoff(s.get('antinuke_enabled'))}\n"
                f"เกณฑ์: {s.get('antinuke_limit') or 3} ครั้ง / {s.get('antinuke_window_sec') or 30} วินาที\n"
                "ตรวจ: ลบช่อง/role, แบน/เตะ, สร้าง webhook → ถอด role อันตราย\n"
                "ปรับค่าด้วย `/security antinuke`, จัดการ whitelist ด้วย `/security whitelist`")
        elif self.category == "automod":
            embed.description = (
                f"สถานะ: {_onoff(s.get('automod_enabled'))}\n"
                f"ห้าม invite: {_onoff(s.get('anti_invite'))}\n"
                f"Anti-mention: {_onoff(s.get('anti_mention_spam'))} (limit {s.get('mention_limit') or 5})\n"
                "ปรับค่าด้วย `/automod ...`")
        return embed

    async def refresh(self, interaction: discord.Interaction):
        _label, col = PANEL_CATEGORIES[self.category]
        self.toggle_btn.disabled = col is None
        if col:
            on = bool((await get_settings(self.guild.id)).get(col))
            self.toggle_btn.label = "ปิดระบบนี้" if on else "เปิดระบบนี้"
            self.toggle_btn.style = discord.ButtonStyle.danger if on else discord.ButtonStyle.success
        else:
            self.toggle_btn.label = "เปิด/ปิด"
            self.toggle_btn.style = discord.ButtonStyle.secondary
        for opt in self.select.options:
            opt.default = (opt.value == self.category)
        await interaction.response.edit_message(embed=await self.build(), view=self)

    async def on_select(self, interaction: discord.Interaction):
        self.category = self.select.values[0]
        await self.refresh(interaction)

    async def on_toggle(self, interaction: discord.Interaction):
        _label, col = PANEL_CATEGORIES[self.category]
        if col:
            pool = await db.get_pool()
            cur = bool((await get_settings(self.guild.id)).get(col))
            # col มาจากตารางคงที่ด้านบนเท่านั้น (ไม่รับจากผู้ใช้)
            await pool.execute(
                f"INSERT INTO settings (guild_id, {col}) VALUES ($1, $2) "
                f"ON CONFLICT (guild_id) DO UPDATE SET {col} = $2", self.guild.id, not cur)
        await self.refresh(interaction)

@security_group.command(name="panel", description="เปิดแผงควบคุมความปลอดภัย (เลือกหมวด + เปิด/ปิด)")
@has_mod_perms()
async def security_panel(interaction: discord.Interaction):
    view = SecurityPanel(interaction.guild, interaction.user.id)
    view.toggle_btn.disabled = True
    await interaction.response.send_message(embed=await view.build(), view=view, ephemeral=True)

tree.add_command(security_group)

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

# ── คิวแยกตามห้อง ─────────────────────────────
# กระดานอยู่ห้องไหน = คิวของห้องนั้น (แต่ละห้องที่มีกระดานมีคิวแยกของตัวเอง)
# ตารางคิวทุกตัว (queue / queue_skipped / queue_bookings / queue_history) ใช้คอลัมน์ guild_id เป็น "ขอบเขตคิว":
#   = channel.id → คิวของห้องที่มีกระดาน
#   = guild.id   → คิวรวมของเซิร์ฟ (ใช้กับคำสั่ง /queue ที่พิมพ์ในห้องที่ไม่มีกระดาน)
# snowflake ของห้องกับของเซิร์ฟไม่มีทางซ้ำกัน จึงไม่ต้องเพิ่มคอลัมน์ในตารางคิว
_queue_scope_cache: dict[int, int] = {}


async def queue_scope(interaction: discord.Interaction) -> int:
    """ขอบเขตคิวของห้องที่กด/พิมพ์คำสั่ง: ห้องที่มีกระดาน = channel.id, ห้องอื่น = guild.id"""
    channel_id = interaction.channel_id
    if channel_id in _queue_scope_cache:
        return _queue_scope_cache[channel_id]
    pool = await db.get_pool()
    has_board = await pool.fetchval(
        "SELECT 1 FROM queue_board WHERE guild_id = $1 AND channel_id = $2",
        interaction.guild.id, channel_id
    )
    scope = channel_id if has_board else interaction.guild.id
    _queue_scope_cache[channel_id] = scope
    return scope


def _guild_of_scope(scope: int) -> Optional[discord.Guild]:
    guild = bot.get_guild(scope)
    if guild:
        return guild
    channel = bot.get_channel(scope)
    return channel.guild if channel and hasattr(channel, "guild") else None


def _scope_label(guild: discord.Guild, scope: int) -> str:
    if scope == guild.id:
        return ""
    channel = guild.get_channel(scope)
    return f" — #{channel.name}" if channel else ""


async def build_queue_embed(guild: discord.Guild, scope: Optional[int] = None) -> discord.Embed:
    """สร้าง embed แสดงรายการคิวปัจจุบัน (scope = คิวของห้องที่แยกไว้ / ไม่ระบุ = คิวรวมของเซิร์ฟ)"""
    scope = scope or guild.id
    pool = await db.get_pool()
    rows = await pool.fetch(
        "SELECT user_id, position, called FROM queue WHERE guild_id = $1 ORDER BY position ASC LIMIT 30",
        scope
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
        title="📋 กระดานคิวสด" + _scope_label(guild, scope),
        description=description,
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text="เข้า/ออกคิว: ทุกคน • เรียก/ข้าม/จบ: แอดมินเท่านั้น • กดรีเฟรชเพื่ออัปเดต")
    return embed


async def refresh_all_boards(guild: discord.Guild, scope: Optional[int] = None):
    """
    อัปเดต embed ของกระดานคิวในกิลด์นี้ให้เป็นเรียลไทม์
    - ส่ง scope มา = อัปเดตเฉพาะกระดานของคิวนั้น (คิวรวม หรือคิวของห้องที่แยกไว้)
    - ไม่ส่ง = อัปเดตทุกกระดานในกิลด์
    กระดานแต่ละอันแสดงคิวของห้องที่กระดานอยู่
    """
    pool = await db.get_pool()
    boards = await pool.fetch(
        "SELECT channel_id, message_id FROM queue_board WHERE guild_id = $1",
        guild.id
    )
    if not boards:
        return

    view = QueueFullBoardView()
    embeds: dict[int, discord.Embed] = {}

    for board in boards:
        board_scope = board["channel_id"]
        if scope is not None and board_scope != scope:
            continue
        channel = guild.get_channel(board["channel_id"])
        if not channel:
            # ช่องถูกลบไปแล้ว เก็บกวาด record ทิ้ง
            await pool.execute(
                "DELETE FROM queue_board WHERE guild_id = $1 AND channel_id = $2",
                guild.id, board["channel_id"]
            )
            _queue_scope_cache.pop(board["channel_id"], None)
            continue
        if board_scope not in embeds:
            embeds[board_scope] = await build_queue_embed(guild, board_scope)
        try:
            msg = await channel.fetch_message(board["message_id"])
            await msg.edit(embed=embeds[board_scope], view=view)
        except discord.NotFound:
            # ข้อความกระดานถูกลบไปแล้ว เก็บกวาด record ทิ้ง
            await pool.execute(
                "DELETE FROM queue_board WHERE guild_id = $1 AND channel_id = $2",
                guild.id, board["channel_id"]
            )
            _queue_scope_cache.pop(board["channel_id"], None)
        except Exception:
            pass


WEEKDAY_TH = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]


def _weekday_label(weekday: int) -> str:
    return "ทุกวัน" if weekday < 0 else f"วัน{WEEKDAY_TH[weekday]}"


async def post_queue_board(channel: discord.TextChannel) -> discord.Message:
    """โพสต์กระดานคิวสดใหม่ในห้องนี้ (ลบข้อความกระดานเก่าของห้องนี้ทิ้ง) แล้วผูกเป็นกระดานหลักของห้อง"""
    guild = channel.guild
    pool = await db.get_pool()
    old_id = await pool.fetchval(
        "SELECT message_id FROM queue_board WHERE guild_id = $1 AND channel_id = $2",
        guild.id, channel.id
    )
    embed = await build_queue_embed(guild, channel.id)
    msg = await channel.send(embed=embed, view=QueueFullBoardView())
    await pool.execute(
        """
        INSERT INTO queue_board (guild_id, channel_id, message_id)
        VALUES ($1, $2, $3)
        ON CONFLICT (guild_id, channel_id) DO UPDATE SET message_id = EXCLUDED.message_id
        """,
        guild.id, channel.id, msg.id
    )
    _queue_scope_cache[channel.id] = channel.id
    if old_id and old_id != msg.id:
        try:
            old = await channel.fetch_message(old_id)
            await old.delete()
        except Exception:
            pass
    return msg


@tasks.loop(minutes=1)
async def check_board_schedules():
    """รันทุก 1 นาที: ถึงเวลาที่ตั้งไว้ (เวลาไทย) → โพสต์กระดานคิวในห้องที่กำหนด วันละครั้งต่อรายการ"""
    try:
        pool = await db.get_pool()
        now = datetime.now(BANGKOK_TZ)
        today = now.date()
        rows = await pool.fetch(
            """
            SELECT id, guild_id, channel_id, weekday, time_hm FROM queue_board_schedule
            WHERE (weekday = -1 OR weekday = $1)
              AND (last_run_date IS NULL OR last_run_date < $2)
            """,
            now.weekday(), today
        )
    except Exception:
        return

    for r in rows:
        try:
            hh, mm = map(int, r["time_hm"].split(":"))
        except Exception:
            continue
        due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        # ยิงเมื่อถึงเวลาแล้ว และยังไม่เลยมาเกิน 5 นาที (กันบอทรีสตาร์ทแล้วโพสต์ย้อนหลัง)
        if not (due <= now < due + timedelta(minutes=5)):
            continue
        await pool.execute(
            "UPDATE queue_board_schedule SET last_run_date = $1 WHERE id = $2", today, r["id"]
        )
        guild = bot.get_guild(r["guild_id"])
        channel = guild.get_channel(r["channel_id"]) if guild else None
        if not channel:
            continue
        try:
            await post_queue_board(channel)
        except Exception as e:
            log.error(f"Scheduled queue board failed ({r['id']}): {e}")


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
        guild = _guild_of_scope(b["guild_id"])
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
        guild = _guild_of_scope(b["guild_id"])
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

        await refresh_all_boards(guild, b["guild_id"])


@check_bookings.before_loop
async def before_check_bookings():
    await bot.wait_until_ready()


# timer dict: guild_id → asyncio.Task (1 นาที auto-skip หลังกด "เรียก")
_call_timers: dict[int, asyncio.Task] = {}  # key = queue scope (guild.id หรือ channel.id ของห้องที่แยกคิว)

# ประกาศ "ถึงคิวแล้ว" ล่าสุดของแต่ละขอบเขตคิว — ให้ค้างในห้องแค่อันเดียว
_call_announces: dict[int, discord.Message] = {}


async def _delete_call_announce(scope: int):
    """ลบประกาศเรียกคิวอันเก่าของขอบเขตคิวนี้ (ถ้ามี)"""
    old = _call_announces.pop(scope, None)
    if old:
        try:
            await old.delete()
        except Exception:
            pass


async def _send_call_announce(interaction: discord.Interaction, scope: int, embed: discord.Embed):
    """ส่งประกาศเรียกคิวใหม่ และลบอันเก่าทิ้ง"""
    await _delete_call_announce(scope)
    await interaction.response.send_message(embed=embed)
    try:
        _call_announces[scope] = await interaction.original_response()
    except Exception:
        pass


async def _auto_skip_after_timeout(guild: discord.Guild, called_user_id: int, channel_id: int, scope: Optional[int] = None):
    """รอ 1 นาที ถ้ายังไม่กด 'จบ' ให้ข้ามอัตโนมัติ"""
    scope = scope or guild.id
    await asyncio.sleep(60)  # 1 นาที
    try:
        pool = await db.get_pool()
        # ตรวจว่าคนนี้ยังอยู่หัวคิวและ called=TRUE อยู่ไหม
        row = await pool.fetchrow(
            "SELECT user_id FROM queue WHERE guild_id = $1 AND user_id = $2 AND called = TRUE",
            scope, called_user_id
        )
        if not row:
            return  # จบแล้ว หรือถูกเปลี่ยนไปแล้ว

        # ย้ายไปกระดานข้าม
        await pool.execute(
            """
            INSERT INTO queue_skipped (guild_id, user_id, skipped_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (guild_id, user_id) DO UPDATE SET skipped_at = NOW()
            """,
            scope, called_user_id
        )
        await pool.execute(
            "DELETE FROM queue WHERE guild_id = $1 AND user_id = $2",
            scope, called_user_id
        )
        await pool.execute(
            "INSERT INTO queue_history (guild_id, user_id, action) VALUES ($1, $2, 'auto_skip')",
            scope, called_user_id
        )
        await _renumber_queue(pool, scope)

        # คนถัดไปขึ้นมาหัวคิว แต่ยังไม่เรียก — รอแอดมินกด "เรียก" เอง (ไม่นับเวลาต่อ)
        next_row = await pool.fetchrow(
            "SELECT user_id FROM queue WHERE guild_id = $1 ORDER BY position ASC LIMIT 1",
            scope
        )

        channel = guild.get_channel(channel_id)
        if channel:
            skipped_mention = f"<@{called_user_id}>"
            if next_row:
                next_mention = f"<@{next_row['user_id']}>"
                embed = discord.Embed(
                    title="⏰ หมดเวลา — ข้ามอัตโนมัติ",
                    description=f"ข้าม {skipped_mention} (ไม่ตอบสนองภายใน 1 นาที)\n▶️ คิวถัดไป: {next_mention} — รอเรียก",
                    color=discord.Color.orange()
                )
            else:
                embed = discord.Embed(
                    title="⏰ หมดเวลา — ข้ามอัตโนมัติ",
                    description=f"ข้าม {skipped_mention} (ไม่ตอบสนองภายใน 1 นาที)\nไม่มีคนในคิวต่อแล้ว",
                    color=discord.Color.orange()
                )
            await _delete_call_announce(scope)
            _call_announces[scope] = await channel.send(embed=embed)

        await refresh_all_boards(guild, scope)
    except Exception:
        pass
    finally:
        _call_timers.pop(scope, None)


class ConfirmActionView(discord.ui.View):
    """หน้ายืนยัน/ยกเลิก ก่อนทำคำสั่งที่ลบข้อมูล (หมดเวลา 30 วิ = ยกเลิก)"""
    def __init__(self, owner_id: int, action):
        super().__init__(timeout=30)
        self.owner_id = owner_id
        self.action = action  # async (interaction) -> str ข้อความผลลัพธ์
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ ปุ่มนี้สำหรับคนที่กดคำสั่งเท่านั้น", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="ยืนยัน", style=discord.ButtonStyle.danger)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="⏳ กำลังดำเนินการ...", view=None)
        try:
            result = await self.action(interaction)
        except Exception as e:
            log.error(f"ConfirmActionView action failed: {e}")
            result = "❌ ทำรายการไม่สำเร็จ"
        await interaction.edit_original_response(content=result)

    @discord.ui.button(label="ยกเลิก", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="ยกเลิกแล้ว", view=None)


async def _clear_queue_scope(scope: int) -> int:
    """ลบคิวหลักทั้งหมดของขอบเขตคิวนี้ คืนจำนวนคนที่ลบ"""
    pool = await db.get_pool()
    rows = await pool.fetch("DELETE FROM queue WHERE guild_id = $1 RETURNING user_id", scope)
    return len(rows)


class QueueFullBoardView(discord.ui.View):
    """
    กระดานคิว:
    แถว 0 (ทุกคน): เข้าคิว / ออกจากคิว
    แถว 1 (แอดมิน): เรียก / จบ / ข้าม / เรียกคนถูกข้าม
    แถว 2 (แอดมิน): เรียกด่วน / ดูกระดานทั้งหมด / ดูประวัติ
    (ล้างคิว / ล้างกระดานข้าม ย้ายไปเป็นคำสั่ง /queue clear, /queue clearskipped)
    """
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        cid = interaction.data.get("custom_id") if interaction.data else None
        if cid in ("queue_join", "queue_leave"):
            return True
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message(
                "❌ คุณไม่มีสิทธิ์ใช้ปุ่มนี้ (ต้องมีสิทธิ์ Manage Messages)",
                ephemeral=True
            )
            return False
        return True

    # ── แถว 0: ทุกคน ────────────────────────────────────────────
    @discord.ui.button(label="เข้าคิว", style=discord.ButtonStyle.green, custom_id="queue_join", row=0)
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        qs = await queue_scope(interaction)
        pool = await db.get_pool()
        exists = await pool.fetchval(
            "SELECT 1 FROM queue WHERE guild_id = $1 AND user_id = $2",
            qs, interaction.user.id
        )
        if exists:
            return await interaction.response.send_message("คุณอยู่ในคิวแล้ว", ephemeral=True)
        max_pos = await pool.fetchval(
            "SELECT COALESCE(MAX(position), 0) FROM queue WHERE guild_id = $1", qs
        ) or 0
        await pool.execute(
            "INSERT INTO queue (guild_id, user_id, position) VALUES ($1, $2, $3)",
            qs, interaction.user.id, max_pos + 1
        )
        await interaction.response.send_message(f"เข้าคิวแล้ว ตำแหน่งที่ **{max_pos + 1}**", ephemeral=True)
        await refresh_all_boards(interaction.guild, qs)

    @discord.ui.button(label="ออกจากคิว", style=discord.ButtonStyle.red, custom_id="queue_leave", row=0)
    async def leave_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        qs = await queue_scope(interaction)
        pool = await db.get_pool()
        await pool.execute(
            "DELETE FROM queue WHERE guild_id = $1 AND user_id = $2",
            qs, interaction.user.id
        )
        await interaction.response.send_message("ออกจากคิวแล้ว", ephemeral=True)
        await refresh_all_boards(interaction.guild, qs)

    # ── แถว 1: แอดมิน ───────────────────────────────────────────
    @discord.ui.button(label="เรียก", style=discord.ButtonStyle.green, custom_id="queue_board_call", row=1)
    async def call_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        qs = await queue_scope(interaction)
        try:
            pool = await db.get_pool()
            row = await pool.fetchrow(
                "SELECT user_id, called FROM queue WHERE guild_id = $1 ORDER BY position ASC LIMIT 1",
                qs
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

        called_id = row["user_id"]
        member = interaction.guild.get_member(called_id)

        try:
            await pool.execute(
                "UPDATE queue SET called = TRUE WHERE guild_id = $1 AND user_id = $2",
                qs, called_id
            )
            await pool.execute(
                "INSERT INTO queue_history (guild_id, user_id, action, actioned_by) VALUES ($1, $2, 'called', $3)",
                qs, called_id, interaction.user.id
            )
        except Exception:
            return await interaction.response.send_message("❌ อัปเดตสถานะคิวไม่สำเร็จ", ephemeral=True)

        # ยกเลิก timer เก่า (ถ้ามี) แล้วเริ่มใหม่
        old_task = _call_timers.pop(qs, None)
        if old_task and not old_task.done():
            old_task.cancel()
        _call_timers[qs] = asyncio.create_task(
            _auto_skip_after_timeout(interaction.guild, called_id, interaction.channel.id, qs)
        )

        announce = discord.Embed(
            title="📢 ถึงคิวแล้ว!",
            description=f"{member.mention if member else f'<@{called_id}>'} กรุณาเข้ามาได้เลย\n⏰ มีเวลา 1 นาที",
            color=discord.Color.green()
        )
        await _send_call_announce(interaction, qs, announce)
        await refresh_all_boards(interaction.guild, qs)

    @discord.ui.button(label="จบ", style=discord.ButtonStyle.red, custom_id="queue_board_end", row=1)
    async def end_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        qs = await queue_scope(interaction)
        try:
            pool = await db.get_pool()
            row = await pool.fetchrow(
                "SELECT user_id FROM queue WHERE guild_id = $1 ORDER BY position ASC LIMIT 1",
                qs
            )
        except Exception:
            return await interaction.response.send_message("❌ เชื่อมต่อฐานข้อมูลไม่สำเร็จ", ephemeral=True)

        if not row:
            return await interaction.response.send_message("ตอนนี้ไม่มีคนในคิว", ephemeral=True)

        finished_id = row["user_id"]

        # ยกเลิก timer 1 นาที
        task = _call_timers.pop(qs, None)
        if task and not task.done():
            task.cancel()

        try:
            await pool.execute(
                "DELETE FROM queue WHERE guild_id = $1 AND user_id = $2",
                qs, finished_id
            )
            await pool.execute(
                "INSERT INTO queue_history (guild_id, user_id, action, actioned_by) VALUES ($1, $2, 'done', $3)",
                qs, finished_id, interaction.user.id
            )
            await _renumber_queue(pool, qs)
        except Exception:
            return await interaction.response.send_message("❌ อัปเดตคิวไม่สำเร็จ", ephemeral=True)

        member = interaction.guild.get_member(finished_id)
        name = member.display_name if member else f"<@{finished_id}>"
        await _delete_call_announce(qs)
        await interaction.response.send_message(f"✅ จบคิวของ **{name}** แล้ว", ephemeral=True)
        await refresh_all_boards(interaction.guild, qs)

    @discord.ui.button(label="ข้าม", style=discord.ButtonStyle.secondary, custom_id="queue_board_skip", row=1)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """ข้ามคนหัวคิว → กระดานถูกข้าม แล้วเรียกถัดไปอัตโนมัติ"""
        qs = await queue_scope(interaction)
        try:
            pool = await db.get_pool()
            row = await pool.fetchrow(
                "SELECT user_id FROM queue WHERE guild_id = $1 ORDER BY position ASC LIMIT 1",
                qs
            )
        except Exception:
            return await interaction.response.send_message("❌ เชื่อมต่อฐานข้อมูลไม่สำเร็จ", ephemeral=True)

        if not row:
            return await interaction.response.send_message("ตอนนี้ไม่มีคนในคิว", ephemeral=True)

        skipped_id = row["user_id"]

        # ยกเลิก timer เก่า
        task = _call_timers.pop(qs, None)
        if task and not task.done():
            task.cancel()

        try:
            await pool.execute(
                """
                INSERT INTO queue_skipped (guild_id, user_id, skipped_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (guild_id, user_id) DO UPDATE SET skipped_at = NOW()
                """,
                qs, skipped_id
            )
            await pool.execute(
                "DELETE FROM queue WHERE guild_id = $1 AND user_id = $2",
                qs, skipped_id
            )
            await pool.execute(
                "INSERT INTO queue_history (guild_id, user_id, action, actioned_by) VALUES ($1, $2, 'skipped', $3)",
                qs, skipped_id, interaction.user.id
            )
            await _renumber_queue(pool, qs)
            next_row = await pool.fetchrow(
                "SELECT user_id FROM queue WHERE guild_id = $1 ORDER BY position ASC LIMIT 1",
                qs
            )
            if next_row:
                await pool.execute(
                    "UPDATE queue SET called = TRUE WHERE guild_id = $1 AND user_id = $2",
                    qs, next_row["user_id"]
                )
                await pool.execute(
                    "INSERT INTO queue_history (guild_id, user_id, action, actioned_by) VALUES ($1, $2, 'called', $3)",
                    qs, next_row["user_id"], interaction.user.id
                )
                # เริ่ม timer ใหม่สำหรับคนถัดไป
                _call_timers[qs] = asyncio.create_task(
                    _auto_skip_after_timeout(interaction.guild, next_row["user_id"], interaction.channel.id, qs)
                )
        except Exception:
            return await interaction.response.send_message("❌ อัปเดตคิวไม่สำเร็จ", ephemeral=True)

        skipped_mention = f"<@{skipped_id}>"
        if next_row:
            next_mention = f"<@{next_row['user_id']}>"
            announce = discord.Embed(
                title="⏭️ ข้ามคิวแล้ว — เรียกคิวถัดไปอัตโนมัติ",
                description=f"ข้าม {skipped_mention}\n📢 ถึงคิวแล้ว: {next_mention} กรุณาเข้ามาได้เลย\n⏰ มีเวลา 1 นาที",
                color=discord.Color.green()
            )
        else:
            announce = discord.Embed(
                title="⏭️ ข้ามคิวแล้ว",
                description=f"ข้าม {skipped_mention}\nไม่มีคนในคิวต่อแล้ว",
                color=discord.Color.gold()
            )
        await _send_call_announce(interaction, qs, announce)
        await refresh_all_boards(interaction.guild, qs)

    @discord.ui.button(label="เรียกคนถูกข้าม", style=discord.ButtonStyle.blurple, custom_id="queue_board_call_skipped", row=1)
    async def call_skipped_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """ดึงคนแรกจากกระดานถูกข้าม → position 1 ในคิวหลัก + เรียก"""
        qs = await queue_scope(interaction)
        pool = await db.get_pool()
        skipped_row = await pool.fetchrow(
            "SELECT user_id FROM queue_skipped WHERE guild_id = $1 ORDER BY skipped_at ASC LIMIT 1",
            qs
        )
        if not skipped_row:
            return await interaction.response.send_message("ไม่มีใครในกระดานถูกข้าม", ephemeral=True)

        await interaction.response.send_message(
            "เลือกสมาชิกจากกระดานถูกข้ามที่ต้องการเรียก:",
            view=QueueSkippedSelectView(),
            ephemeral=True
        )

    # ── แถว 2: แอดมิน ───────────────────────────────────────────
    @discord.ui.button(label="เรียกด่วน", style=discord.ButtonStyle.blurple, custom_id="queue_board_urgent_call", row=2)
    async def urgent_call_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """UserSelect จากคนในคิว → ย้ายมา position 1 + called"""
        await interaction.response.send_message(
            "เลือกสมาชิกในคิวที่จะเรียกด่วน (ย้ายมาอันดับ 1):",
            view=QueueUrgentCallSelectView(),
            ephemeral=True
        )

    @discord.ui.button(label="ดูกระดานทั้งหมด", style=discord.ButtonStyle.secondary, custom_id="queue_board_view_all", row=2)
    async def view_all_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """แสดงทั้งคิวหลักและกระดานถูกข้าม"""
        qs = await queue_scope(interaction)
        pool = await db.get_pool()
        queue_rows = await pool.fetch(
            "SELECT user_id, position, called FROM queue WHERE guild_id = $1 ORDER BY position ASC",
            qs
        )
        skipped_rows = await pool.fetch(
            "SELECT user_id, skipped_at FROM queue_skipped WHERE guild_id = $1 ORDER BY skipped_at ASC",
            qs
        )

        embed = discord.Embed(title="📋 กระดานคิวทั้งหมด", color=discord.Color.blurple())

        if queue_rows:
            lines = []
            for r in queue_rows:
                m = interaction.guild.get_member(r["user_id"])
                name = m.display_name if m else f"<@{r['user_id']}>"
                status = " 📢" if r["called"] else ""
                lines.append(f"`{r['position']}.` {name}{status}")
            embed.add_field(name="🟢 คิวหลัก", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="🟢 คิวหลัก", value="ว่าง", inline=False)

        if skipped_rows:
            lines = []
            for i, r in enumerate(skipped_rows, 1):
                m = interaction.guild.get_member(r["user_id"])
                name = m.display_name if m else f"<@{r['user_id']}>"
                lines.append(f"`{i}.` {name}")
            embed.add_field(name="⏭️ กระดานถูกข้าม", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="⏭️ กระดานถูกข้าม", value="ว่าง", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="ดูประวัติ", style=discord.ButtonStyle.secondary, custom_id="queue_board_history", row=2)
    async def history_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """ดูประวัติ 10 รายการล่าสุดจาก queue_history"""
        qs = await queue_scope(interaction)
        pool = await db.get_pool()
        rows = await pool.fetch(
            "SELECT user_id, action, actioned_by, created_at FROM queue_history WHERE guild_id = $1 ORDER BY created_at DESC LIMIT 10",
            qs
        )
        if not rows:
            return await interaction.response.send_message("ยังไม่มีประวัติในระบบ", ephemeral=True)

        action_label = {
            "called": "📢 เรียก",
            "done": "✅ จบ",
            "skipped": "⏭️ ข้าม",
            "auto_skip": "⏰ ข้ามอัตโนมัติ",
            "urgent_call": "🚀 เรียกด่วน",
            "skipped_call": "🔁 เรียกคนถูกข้าม",
            "cleared": "🗑️ ล้างกระดาน",
        }
        lines = []
        for r in rows:
            m = interaction.guild.get_member(r["user_id"])
            name = m.display_name if m else f"<@{r['user_id']}>"
            label = action_label.get(r["action"], r["action"])
            by = ""
            if r["actioned_by"]:
                bm = interaction.guild.get_member(r["actioned_by"])
                by_id = r["actioned_by"]
                by = f" โดย {bm.display_name if bm else f'<@{by_id}>'}"
            ts = r["created_at"].strftime("%H:%M") if r["created_at"] else ""
            lines.append(f"`{ts}` {label} **{name}**{by}")

        embed = discord.Embed(
            title="📜 ประวัติคิว (10 ล่าสุด)",
            description="\n".join(lines),
            color=discord.Color.greyple()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def _renumber_queue(pool, guild_id: int):
    """จัดลำดับ position ในคิวใหม่ให้เรียงต่อเนื่อง 1,2,3,... ตามลำดับเดิม"""
    await pool.execute("""
        WITH ordered AS (
            SELECT user_id, ROW_NUMBER() OVER (ORDER BY position) AS new_pos
            FROM queue WHERE guild_id = $1
        )
        UPDATE queue q SET position = o.new_pos
        FROM ordered o
        WHERE q.guild_id = $1 AND q.user_id = o.user_id
    """, guild_id)


async def _priority_call(interaction: discord.Interaction, target: discord.Member):
    """
    เรียกคนที่ถูกข้ามคิว (อยู่ในกระดานคิวที่ถูกข้าม) ให้เข้ามาเรียกทันที
    ดึงออกจากกระดานที่ถูกข้าม แล้วใส่ไว้บนสุดของคิวหลักพร้อมมาร์คว่าเรียกแล้ว — ไม่แตะคนอื่นในคิวหลักเลย
    """
    qs = await queue_scope(interaction)
    pool = await db.get_pool()
    existed = await pool.fetchval(
        "SELECT 1 FROM queue_skipped WHERE guild_id = $1 AND user_id = $2",
        qs, target.id
    )
    if not existed:
        return await interaction.response.send_message(
            f"❌ {target.mention} ไม่ได้อยู่ในกระดานคิวที่ถูกข้าม", ephemeral=True
        )

    await pool.execute(
        "DELETE FROM queue_skipped WHERE guild_id = $1 AND user_id = $2",
        qs, target.id
    )

    min_pos = await pool.fetchval(
        "SELECT COALESCE(MIN(position), 1) FROM queue WHERE guild_id = $1", qs
    )
    already_in_queue = await pool.fetchval(
        "SELECT 1 FROM queue WHERE guild_id = $1 AND user_id = $2",
        qs, target.id
    )
    if already_in_queue:
        await pool.execute(
            "UPDATE queue SET position = $3, called = TRUE WHERE guild_id = $1 AND user_id = $2",
            qs, target.id, min_pos - 1
        )
    else:
        await pool.execute(
            "INSERT INTO queue (guild_id, user_id, position, called) VALUES ($1, $2, $3, TRUE)",
            qs, target.id, min_pos - 1
        )
    await _renumber_queue(pool, qs)

    announce = discord.Embed(
        title="📢 ถึงคิวแล้ว! (เรียกจากกระดานคิวที่ถูกข้าม)",
        description=f"{target.mention} กรุณาเข้ามาได้เลย",
        color=discord.Color.green()
    )
    await _send_call_announce(interaction, qs, announce)
    await refresh_all_boards(interaction.guild, qs)


async def _do_book(interaction: discord.Interaction, date: str, time: str):
    """จองคิวล่วงหน้า — ใช้ร่วมกันโดย /queue book และปุ่ม 'จองคิว' บนกระดาน"""
    qs = await queue_scope(interaction)
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
        qs, interaction.user.id
    )
    if existing:
        return await interaction.response.send_message(
            "คุณมีการจองที่ยังไม่ถึงเวลาอยู่แล้ว ใช้ `/queue unbook` ก่อนถ้าต้องการจองใหม่",
            ephemeral=True
        )

    await pool.execute(
        "INSERT INTO queue_bookings (guild_id, user_id, slot_time) VALUES ($1, $2, $3)",
        qs, interaction.user.id, slot_time.astimezone(timezone.utc)
    )

    thai_str = slot_time.strftime("%d/%m/%Y เวลา %H:%M น.")
    await interaction.response.send_message(
        f"📅 จองคิวเรียบร้อย: **{thai_str}** (เวลาไทย)\n"
        f"ระบบจะ DM แจ้งเตือนล่วงหน้า 10 นาที และเพิ่มเข้าคิวให้อัตโนมัติเมื่อถึงเวลา (เปิดรับ DM จากสมาชิกเซิร์ฟเวอร์ไว้ด้วยนะครับ)",
        ephemeral=True
    )


async def _do_unbook(interaction: discord.Interaction):
    """ยกเลิกการจองคิวล่วงหน้าของตัวเอง — ใช้ร่วมกันโดย /queue unbook และปุ่ม 'ยกเลิกจอง' บนกระดาน"""
    qs = await queue_scope(interaction)
    pool = await db.get_pool()
    result = await pool.execute(
        "DELETE FROM queue_bookings WHERE guild_id = $1 AND user_id = $2 AND activated = FALSE",
        qs, interaction.user.id
    )
    if result == "DELETE 0":
        return await interaction.response.send_message("คุณไม่มีการจองที่รอดำเนินการอยู่", ephemeral=True)
    await interaction.response.send_message("ยกเลิกการจองแล้ว", ephemeral=True)


async def _build_bookings_embed(guild: discord.Guild, scope: Optional[int] = None) -> Optional[discord.Embed]:
    """สร้าง embed รายการจองคิวล่วงหน้า — คืนค่า None ถ้ายังไม่มีรายการจอง"""
    pool = await db.get_pool()
    rows = await pool.fetch(
        "SELECT user_id, slot_time FROM queue_bookings WHERE guild_id = $1 AND activated = FALSE ORDER BY slot_time ASC LIMIT 20",
        scope or guild.id
    )
    if not rows:
        return None
    lines = []
    for r in rows:
        local_time = r["slot_time"].astimezone(BANGKOK_TZ)
        member = guild.get_member(r["user_id"])
        name = member.display_name if member else f"Unknown ({r['user_id']})"
        lines.append(f"🗓️ {local_time.strftime('%d/%m %H:%M')} น. — {name}")
    return discord.Embed(title="📅 รายการจองคิวล่วงหน้า", description="\n".join(lines), color=0x5865F2)


async def _build_skipped_embed(guild: discord.Guild, scope: Optional[int] = None) -> Optional[discord.Embed]:
    """สร้าง embed รายชื่อคนที่ถูกข้ามคิว — คืนค่า None ถ้ายังไม่มีใครถูกข้าม"""
    pool = await db.get_pool()
    rows = await pool.fetch(
        "SELECT user_id, skipped_at FROM queue_skipped WHERE guild_id = $1 ORDER BY skipped_at ASC LIMIT 30",
        scope or guild.id
    )
    if not rows:
        return None
    lines = [f"`{i}.` <@{r['user_id']}>" for i, r in enumerate(rows, 1)]
    embed = discord.Embed(
        title="⏭️ กระดานคิวที่ถูกข้าม",
        description="\n".join(lines),
        color=0xFEE75C
    )
    embed.set_footer(text="ใช้ /queue requeue user: เพื่อนำกลับเข้าคิวหลัก (ต่อท้ายคิว)")
    return embed


async def _do_requeue(interaction: discord.Interaction, user: discord.Member):
    """นำคนที่ถูกข้ามกลับเข้าคิวหลัก — ใช้ร่วมกันโดย /queue requeue และปุ่ม 'นำกลับเข้าคิว' บนกระดาน"""
    qs = await queue_scope(interaction)
    pool = await db.get_pool()
    existed = await pool.fetchval(
        "SELECT 1 FROM queue_skipped WHERE guild_id = $1 AND user_id = $2",
        qs, user.id
    )
    if not existed:
        return await interaction.response.send_message(
            f"❌ {user.mention} ไม่ได้อยู่ในกระดานคิวที่ถูกข้าม", ephemeral=True
        )

    already_in_queue = await pool.fetchval(
        "SELECT 1 FROM queue WHERE guild_id = $1 AND user_id = $2",
        qs, user.id
    )
    if not already_in_queue:
        max_pos = await pool.fetchval(
            "SELECT COALESCE(MAX(position), 0) FROM queue WHERE guild_id = $1", qs
        ) or 0
        await pool.execute(
            "INSERT INTO queue (guild_id, user_id, position) VALUES ($1, $2, $3)",
            qs, user.id, max_pos + 1
        )

    await pool.execute(
        "DELETE FROM queue_skipped WHERE guild_id = $1 AND user_id = $2",
        qs, user.id
    )
    await interaction.response.send_message(f"✅ นำ {user.mention} กลับเข้าคิวหลักแล้ว (ต่อท้ายคิว)", ephemeral=True)
    await refresh_all_boards(interaction.guild, qs)


async def _confirm_clear_skipped(interaction: discord.Interaction):
    """ล้างกระดานถูกข้ามทั้งหมด (ไม่แตะคิวหลัก) — ต้องกดยืนยันก่อน"""
    qs = await queue_scope(interaction)
    pool = await db.get_pool()
    count = await pool.fetchval("SELECT COUNT(*) FROM queue_skipped WHERE guild_id = $1", qs)
    if not count:
        return await interaction.response.send_message("กระดานถูกข้ามว่างอยู่แล้ว", ephemeral=True)

    async def do_remove(i: discord.Interaction) -> str:
        deleted_rows = await pool.fetch(
            "DELETE FROM queue_skipped WHERE guild_id = $1 RETURNING user_id", qs
        )
        if not deleted_rows:
            return "กระดานถูกข้ามว่างอยู่แล้ว"
        await pool.executemany(
            "INSERT INTO queue_history (guild_id, user_id, action, actioned_by) VALUES ($1, $2, 'removed_from_skipped', $3)",
            [(qs, r["user_id"], i.user.id) for r in deleted_rows]
        )
        await refresh_all_boards(i.guild, qs)
        return f"✅ ล้างกระดานถูกข้ามทั้งหมดแล้ว ({len(deleted_rows)} คน)"

    await interaction.response.send_message(
        f"⚠️ ยืนยันลบกระดานถูกข้ามทั้งหมด **{count} คน** ใช่ไหม?",
        view=ConfirmActionView(interaction.user.id, do_remove),
        ephemeral=True
    )


async def _confirm_clear_queue(interaction: discord.Interaction):
    """ล้างคิวหลักทั้งหมด — ต้องกดยืนยันก่อน"""
    qs = await queue_scope(interaction)
    pool = await db.get_pool()
    count = await pool.fetchval("SELECT COUNT(*) FROM queue WHERE guild_id = $1", qs)
    if not count:
        return await interaction.response.send_message("ตอนนี้ไม่มีใครอยู่ในคิว", ephemeral=True)

    async def do_clear(i: discord.Interaction) -> str:
        # ยกเลิก timer
        task = _call_timers.pop(qs, None)
        if task and not task.done():
            task.cancel()
        await _delete_call_announce(qs)
        n = await _clear_queue_scope(qs)
        await refresh_all_boards(i.guild, qs)
        return f"✅ ล้างคิวทั้งหมดแล้ว ({n} คน)" if n else "ตอนนี้ไม่มีใครอยู่ในคิว"

    await interaction.response.send_message(
        f"⚠️ ยืนยันล้างกระดานคิว **{count} คน** ใช่ไหม?",
        view=ConfirmActionView(interaction.user.id, do_clear),
        ephemeral=True
    )


class QueueBookModal(discord.ui.Modal, title="จองคิวล่วงหน้า"):
    date_input = discord.ui.TextInput(
        label="วันที่ (YYYY-MM-DD)",
        placeholder="เช่น 2026-08-10",
        max_length=10
    )
    time_input = discord.ui.TextInput(
        label="เวลา (HH:MM 24 ชม.)",
        placeholder="เช่น 14:30",
        max_length=5
    )

    async def on_submit(self, interaction: discord.Interaction):
        await _do_book(interaction, self.date_input.value.strip(), self.time_input.value.strip())


class QueueSkippedSelectView(discord.ui.View):
    """UserSelect สำหรับ 'เรียกคนถูกข้าม' — ดึงจากกระดานถูกข้าม → position 1 + called"""
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="เลือกสมาชิกจากกระดานถูกข้าม")
    async def select_user(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        target = select.values[0]
        if not isinstance(target, discord.Member):
            target = interaction.guild.get_member(target.id)
        if target is None:
            return await interaction.response.send_message("❌ ไม่พบสมาชิกในเซิร์ฟเวอร์", ephemeral=True)
        await _priority_call(interaction, target)


class QueueUrgentCallSelectView(discord.ui.View):
    """UserSelect สำหรับ 'เรียกด่วน' — ย้ายคนในคิวมา position 1 + called"""
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="เลือกสมาชิกในคิวที่จะเรียกด่วน")
    async def select_user(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        qs = await queue_scope(interaction)
        target = select.values[0]
        if not isinstance(target, discord.Member):
            target = interaction.guild.get_member(target.id)
        if target is None:
            return await interaction.response.send_message("❌ ไม่พบสมาชิกในเซิร์ฟเวอร์", ephemeral=True)

        pool = await db.get_pool()
        in_queue = await pool.fetchval(
            "SELECT 1 FROM queue WHERE guild_id = $1 AND user_id = $2",
            qs, target.id
        )
        if not in_queue:
            return await interaction.response.send_message(
                f"❌ {target.mention} ไม่ได้อยู่ในคิว", ephemeral=True
            )

        # ยกเลิก timer เก่า
        task = _call_timers.pop(qs, None)
        if task and not task.done():
            task.cancel()

        # ย้ายมา position 0 ก่อน renumber → จะได้เป็น 1
        min_pos = await pool.fetchval(
            "SELECT COALESCE(MIN(position), 1) FROM queue WHERE guild_id = $1", qs
        )
        await pool.execute(
            "UPDATE queue SET position = $3, called = TRUE WHERE guild_id = $1 AND user_id = $2",
            qs, target.id, min_pos - 1
        )
        await _renumber_queue(pool, qs)
        await pool.execute(
            "INSERT INTO queue_history (guild_id, user_id, action, actioned_by) VALUES ($1, $2, 'urgent_call', $3)",
            qs, target.id, interaction.user.id
        )

        # เริ่ม timer 1 นาที
        _call_timers[qs] = asyncio.create_task(
            _auto_skip_after_timeout(interaction.guild, target.id, interaction.channel.id, qs)
        )

        announce = discord.Embed(
            title="🚀 เรียกด่วน!",
            description=f"{target.mention} กรุณาเข้ามาได้เลย\n⏰ มีเวลา 1 นาที",
            color=discord.Color.gold()
        )
        await _send_call_announce(interaction, qs, announce)
        await refresh_all_boards(interaction.guild, qs)


queue_group = app_commands.Group(name="queue", description="ระบบคิว")

@queue_group.command(name="join", description="เข้าคิว")
async def queue_join(interaction: discord.Interaction):
    qs = await queue_scope(interaction)
    pool = await db.get_pool()
    exists = await pool.fetchval("SELECT 1 FROM queue WHERE guild_id = $1 AND user_id = $2", qs, interaction.user.id)
    if exists:
        return await interaction.response.send_message("คุณอยู่ในคิวแล้ว", ephemeral=True)
    max_pos = await pool.fetchval("SELECT COALESCE(MAX(position), 0) FROM queue WHERE guild_id = $1", qs) or 0
    await pool.execute(
        "INSERT INTO queue (guild_id, user_id, position) VALUES ($1, $2, $3)",
        qs, interaction.user.id, max_pos + 1
    )
    await interaction.response.send_message(f"เข้าคิวแล้ว ตำแหน่งที่ **{max_pos + 1}**", ephemeral=True)
    await refresh_all_boards(interaction.guild, qs)

@queue_group.command(name="leave", description="ออกจากคิว")
async def queue_leave(interaction: discord.Interaction):
    qs = await queue_scope(interaction)
    pool = await db.get_pool()
    await pool.execute("DELETE FROM queue WHERE guild_id = $1 AND user_id = $2", qs, interaction.user.id)
    await interaction.response.send_message("ออกจากคิวแล้ว", ephemeral=True)
    await refresh_all_boards(interaction.guild, qs)

@queue_group.command(name="list", description="ดูรายการคิว")
async def queue_list(interaction: discord.Interaction):
    qs = await queue_scope(interaction)
    embed = await build_queue_embed(interaction.guild, qs)
    await interaction.response.send_message(embed=embed)

@queue_group.command(name="book", description="จองคิวล่วงหน้า (ระบุวันและเวลาไทย)")
@app_commands.describe(date="วันที่ รูปแบบ YYYY-MM-DD เช่น 2026-08-10", time="เวลา รูปแบบ HH:MM (24 ชม.) เช่น 14:30")
async def queue_book(interaction: discord.Interaction, date: str, time: str):
    await _do_book(interaction, date, time)

@queue_group.command(name="unbook", description="ยกเลิกการจองคิวล่วงหน้าของตัวเอง")
async def queue_unbook(interaction: discord.Interaction):
    await _do_unbook(interaction)

@queue_group.command(name="bookings", description="ดูรายการจองคิวล่วงหน้าที่รอดำเนินการ (แอดมิน)")
@has_mod_perms()
async def queue_bookings_cmd(interaction: discord.Interaction):
    qs = await queue_scope(interaction)
    embed = await _build_bookings_embed(interaction.guild, qs)
    if embed is None:
        return await interaction.response.send_message("ยังไม่มีการจองคิวล่วงหน้า", ephemeral=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@queue_group.command(name="call", description="เรียกคิว (ไม่ระบุ user = เรียกคิวถัดไปตามปกติ / ระบุ user = เรียกคนนั้นจากกระดานคิวที่ถูกข้ามเข้ามา)")
@has_mod_perms()
@app_commands.describe(user="ระบุถ้าต้องการเรียกคนนี้จากกระดานคิวที่ถูกข้ามเข้ามาทันที (ต้องเป็นคนที่อยู่ในกระดานคิวที่ถูกข้ามเท่านั้น)")
async def queue_call(interaction: discord.Interaction, user: discord.Member = None):
    qs = await queue_scope(interaction)
    if user is not None:
        return await _priority_call(interaction, user)

    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT user_id, called FROM queue WHERE guild_id = $1 ORDER BY position ASC LIMIT 1",
        qs
    )
    if not row:
        return await interaction.response.send_message("ตอนนี้ไม่มีคนในคิว", ephemeral=True)
    if row.get("called"):
        return await interaction.response.send_message(
            "⚠️ คิวนี้ถูกเรียกไปแล้ว กด **จบ** หรือใช้ `/queue call user:` ก่อนถ้าต้องการเรียกคนอื่น",
            ephemeral=True
        )

    member = interaction.guild.get_member(row["user_id"])
    mention = member.mention if member else f"<@{row['user_id']}>"
    await pool.execute(
        "UPDATE queue SET called = TRUE WHERE guild_id = $1 AND user_id = $2",
        qs, row["user_id"]
    )
    announce = discord.Embed(
        title="📢 ถึงคิวแล้ว!",
        description=f"{mention} กรุณาเข้ามาได้เลย",
        color=discord.Color.green()
    )
    await _send_call_announce(interaction, qs, announce)
    await refresh_all_boards(interaction.guild, qs)

@queue_group.command(name="skipped", description="ดูรายชื่อคนที่ถูกข้ามคิว (กระดานคิวที่ถูกข้าม)")
@has_mod_perms()
async def queue_skipped(interaction: discord.Interaction):
    qs = await queue_scope(interaction)
    embed = await _build_skipped_embed(interaction.guild, qs)
    if embed is None:
        return await interaction.response.send_message("ยังไม่มีใครถูกข้ามคิว", ephemeral=True)
    await interaction.response.send_message(embed=embed)

@queue_group.command(name="requeue", description="นำคนที่ถูกข้ามกลับเข้าคิวหลัก (ต่อท้ายคิว)")
@has_mod_perms()
@app_commands.describe(user="คนที่จะนำกลับเข้าคิว")
async def queue_requeue(interaction: discord.Interaction, user: discord.Member):
    await _do_requeue(interaction, user)

@queue_group.command(name="clear", description="ล้างคิวทั้งหมด (ไม่ลบกระดาน)")
@has_mod_perms()
async def queue_clear(interaction: discord.Interaction):
    await _confirm_clear_queue(interaction)

@queue_group.command(name="clearskipped", description="ลบกระดานถูกข้ามทั้งหมด (ไม่แตะคิวหลัก)")
@has_mod_perms()
async def queue_clear_skipped(interaction: discord.Interaction):
    await _confirm_clear_skipped(interaction)

@queue_group.command(name="refresh", description="รีเฟรชกระดานคิวทั้งหมดในเซิร์ฟเวอร์นี้ให้เป็นข้อมูลล่าสุด")
async def queue_refresh(interaction: discord.Interaction):
    await refresh_all_boards(interaction.guild)
    await interaction.response.send_message("🔄 รีเฟรชกระดานคิวทั้งหมดแล้ว", ephemeral=True)

@queue_group.command(name="board", description="โพสต์กระดานคิวสดของห้องนี้ (แต่ละห้องที่มีกระดาน มีคิวแยกของตัวเอง)")
@has_mod_perms()
async def queue_board(interaction: discord.Interaction):
    scope = interaction.channel.id
    embed = await build_queue_embed(interaction.guild, scope)
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
    _queue_scope_cache[interaction.channel.id] = scope


class ScheduleDaysView(discord.ui.View):
    """เลือกวันได้หลายวัน สำหรับ /queue schedule"""
    def __init__(self, owner_id: int, channel: discord.TextChannel, time_hm: str):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.channel = channel
        self.time_hm = time_hm
        options = [discord.SelectOption(label="ทุกวัน", value="-1")] + [
            discord.SelectOption(label=f"วัน{d}", value=str(i)) for i, d in enumerate(WEEKDAY_TH)
        ]
        select = discord.ui.Select(
            placeholder="เลือกวัน (เลือกได้หลายวัน)", min_values=1, max_values=len(options), options=options
        )
        select.callback = self.on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ เมนูนี้สำหรับคนที่กดคำสั่งเท่านั้น", ephemeral=True)
            return False
        return True

    async def on_select(self, interaction: discord.Interaction):
        self.stop()
        values = sorted({int(v) for v in interaction.data.get("values", [])})
        days = [-1] if -1 in values else values
        now = datetime.now(BANGKOK_TZ)
        # ถ้าเวลาวันนี้ผ่านไปแล้ว ไม่ต้องโพสต์ย้อนหลังวันนี้
        skip_today = now.date() if now.strftime("%H:%M") > self.time_hm else None
        pool = await db.get_pool()
        ids = []
        for d in days:
            ids.append(await pool.fetchval(
                """
                INSERT INTO queue_board_schedule (guild_id, channel_id, weekday, time_hm, created_by, last_run_date)
                VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
                """,
                interaction.guild.id, self.channel.id, d, self.time_hm, interaction.user.id, skip_today
            ))
        labels = ", ".join(_weekday_label(d) for d in days)
        await interaction.response.edit_message(
            content=f"✅ ตั้งเวลาแล้ว ({', '.join(f'#{i}' for i in ids)}): โชว์กระดานคิว **{labels} เวลา {self.time_hm}** ที่ {self.channel.mention}",
            view=None
        )


@queue_group.command(name="schedule", description="ตั้งเวลาโชว์กระดานคิวอัตโนมัติ (เวลาไทย) เลือกได้หลายวัน")
@has_mod_perms()
@app_commands.describe(เวลา="เวลา HH:MM 24 ชม. เช่น 19:30", ห้อง="ห้องที่จะโพสต์กระดาน (ไม่ใส่ = ห้องนี้)")
async def queue_schedule(
    interaction: discord.Interaction,
    เวลา: str,
    ห้อง: Optional[discord.TextChannel] = None,
):
    try:
        t = datetime.strptime(เวลา.strip(), "%H:%M")
    except ValueError:
        return await interaction.response.send_message("❌ รูปแบบเวลาผิด ใช้ HH:MM เช่น 19:30", ephemeral=True)
    channel = ห้อง or interaction.channel
    time_hm = t.strftime("%H:%M")
    await interaction.response.send_message(
        f"เลือกวันที่จะโชว์กระดานคิว เวลา **{time_hm}** ที่ {channel.mention}:",
        view=ScheduleDaysView(interaction.user.id, channel, time_hm),
        ephemeral=True
    )


@queue_group.command(name="schedulelist", description="ดูรายการตั้งเวลาโชว์กระดานคิว")
@has_mod_perms()
async def queue_schedule_list(interaction: discord.Interaction):
    pool = await db.get_pool()
    rows = await pool.fetch(
        "SELECT id, channel_id, weekday, time_hm FROM queue_board_schedule WHERE guild_id = $1 ORDER BY weekday, time_hm",
        interaction.guild.id
    )
    if not rows:
        return await interaction.response.send_message("ยังไม่มีการตั้งเวลา", ephemeral=True)
    lines = [f"`#{r['id']}` {_weekday_label(r['weekday'])} {r['time_hm']} → <#{r['channel_id']}>" for r in rows]
    embed = discord.Embed(title="⏰ ตั้งเวลาโชว์กระดานคิว", description="\n".join(lines), color=0x5865F2)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@queue_group.command(name="scheduledel", description="ลบการตั้งเวลาโชว์กระดานคิว")
@has_mod_perms()
@app_commands.describe(รหัส="เลข # จาก /queue schedulelist")
async def queue_schedule_del(interaction: discord.Interaction, รหัส: int):
    pool = await db.get_pool()
    deleted = await pool.fetchval(
        "DELETE FROM queue_board_schedule WHERE guild_id = $1 AND id = $2 RETURNING id",
        interaction.guild.id, รหัส
    )
    msg = f"✅ ลบการตั้งเวลา #{รหัส} แล้ว" if deleted else f"❌ ไม่พบการตั้งเวลา #{รหัส}"
    await interaction.response.send_message(msg, ephemeral=True)

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
    embed.add_field(name="/automod", value="toggle • anti_invite • anti_mention_spam • anti_spam • addword • removeword • listwords", inline=False)
    embed.add_field(name="/security", value="panel • audit • antinuke • whitelist", inline=False)
    embed.add_field(name="/customcommand", value="add • remove • list • prefix", inline=False)
    embed.add_field(name="/reactionrole", value="add • remove", inline=False)
    embed.add_field(name="/queue", value="join • leave • list • book • unbook • bookings • clear • refresh • board", inline=False)
    embed.add_field(name="/queue call [user]", value="เรียกคิวปกติ / ระบุ user เพื่อเรียกคนนั้นจากกระดานคิวที่ถูกข้ามเข้ามาทันที", inline=False)
    embed.add_field(name="/queue skipped • /queue requeue", value="ดูรายชื่อคนที่ถูกข้ามคิว • นำกลับเข้าคิวหลัก", inline=False)
    embed.add_field(name="กระดานคิว (/queue board)", value="มีปุ่มครบทุกคำสั่งของระบบคิวในที่เดียว: เข้าคิว/ออกจากคิว/จองคิว/ยกเลิกจอง/เรียก/จบ/ข้าม/รีเฟรช/ดูรายการจอง/เรียกก่อนคิว/ดูคนถูกข้าม/นำกลับเข้าคิว/ล้างคิว", inline=False)
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
            "/queue — join, leave, list, book, unbook, bookings, clear, refresh, board",
            "/queue call [user] — เรียกคิวปกติ / ระบุ user เพื่อเรียกจากกระดานคิวที่ถูกข้ามเข้ามาทันที",
            "/queue skipped, /queue requeue — ดู/นำกลับคนที่ถูกข้ามคิว",
            "กระดานคิวมีปุ่มครบทุกคำสั่งของระบบคิวในที่เดียว",
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
            "/setup-introduction, /setup-jobs, /setup-playerboard, /my-profile, /edit-profile",
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
