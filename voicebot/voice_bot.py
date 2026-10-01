import os
import re
import asyncio
import logging
import csv
import io
import difflib
from datetime import datetime, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from aiohttp import web

import pytesseract
from PIL import Image, ImageOps

import party_ocr

import matplotlib
matplotlib.use("Agg")  # ไม่ต้องใช้ GUI backend เพราะรันบนเซิร์ฟเวอร์
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.font_manager as fm

import database as db
# หมายเหตุ: import access as billing_access ถูกลบออก — ไม่มีไฟล์ access.py อยู่จริงใน repo นี้เลย
# (ไม่มีทั้งใน voicebot/, bot/, voicerelay/, website/) ทำให้ deploy พังด้วย ModuleNotFoundError
# ถ้าต้องการเช็คสิทธิ์สมาชิกก่อนใช้คำสั่ง ต้องสร้าง access.py จริงก่อน แล้วค่อยเปิดใช้ global_billing_check ใหม่

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("voice-tracker-bot")

BANGKOK_TZ = ZoneInfo("Asia/Bangkok")


BUNDLED_THAI_FONT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "NotoSansThai.ttf")


def _configure_thai_font():
    """
    ฟอนต์ default ของ matplotlib (DejaVu Sans) ไม่มี glyph ภาษาไทย — ข้อความไทยในกราฟ (/voice graph)
    และรูปกระดานรายชื่อ (/setup-playerboard) จะขึ้นเป็นกล่องว่างถ้าไม่สลับฟอนต์ก่อน
    ยืนยันแล้วว่า Railway container ที่ deploy จริงไม่มีฟอนต์ไทยติดตั้งไว้เลย (เช็คจาก log ตอน deploy)
    เลยแนบฟอนต์ Noto Sans Thai (OFL license, ดู fonts/OFL.txt) มากับ repo เองแทนการพึ่งฟอนต์ระบบที่คุมไม่ได้
    ถ้าหาไฟล์ที่แนบมาไม่เจอ (เผื่อย้าย/ลบไฟล์ไปในอนาคต) ค่อย fallback ไปหาฟอนต์ไทยที่ระบบอาจมีอยู่แล้วแทน
    """
    if os.path.isfile(BUNDLED_THAI_FONT):
        try:
            fm.fontManager.addfont(BUNDLED_THAI_FONT)
            font_name = fm.FontProperties(fname=BUNDLED_THAI_FONT).get_name()
            plt.rcParams["font.family"] = font_name
            log.info(f"ใช้ฟอนต์ไทยที่แนบมากับ repo ('{font_name}') สำหรับข้อความไทยในกราฟ/รูปภาพ")
            return
        except Exception as e:
            log.warning(f"โหลดฟอนต์ไทยที่แนบมากับ repo ไม่สำเร็จ: {e} — ลองหาฟอนต์ไทยในระบบแทน")

    thai_font_names = ["TH Sarabun New", "Noto Sans Thai", "Garuda", "Loma", "Waree", "Norasi", "Kinnari", "Sawasdee", "Purisa", "Umpush"]
    available = {f.name for f in fm.fontManager.ttflist}
    for name in thai_font_names:
        if name in available:
            plt.rcParams["font.family"] = name
            log.info(f"ใช้ฟอนต์ '{name}' สำหรับข้อความไทยในกราฟ/รูปภาพ")
            return
    log.warning("ไม่พบฟอนต์ที่รองรับภาษาไทยในระบบ — ข้อความไทยในกราฟ/รูปภาพ (/voice graph, /setup-playerboard) อาจขึ้นเป็นกล่องว่าง")


_configure_thai_font()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN is required")

intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True   # จำเป็นสำหรับ on_voice_state_update
intents.members = True        # ใช้แสดงชื่อสมาชิกให้ถูกต้อง (ต้องเปิดใน Discord Developer Portal ด้วย)

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


@bot.event
async def setup_hook():
    bot.add_view(IntroductionBoardView())
    bot.add_view(PartyLeaveBoardView())

# ห้องที่บอทยามติดตามได้ ต้องรวม Stage Channel ด้วย ไม่ใช่แค่ Voice Channel ธรรมดา
# เพราะห้องถ่ายทอดสด/ห้องหลักของ Voice Relay มักตั้งเป็น Stage Channel (ตามคำแนะนำใน docs.html)
# เดิม type hint จำกัดแค่ discord.VoiceChannel ทำให้ Discord ไม่ให้เลือก Stage Channel ใน /track add เลย
TrackableChannel = Union[discord.VoiceChannel, discord.StageChannel]

# กัน on_ready รันงาน setup ซ้ำ — discord.py จะยิง on_ready ใหม่ทุกครั้งที่ reconnect
# ไม่ใช่แค่ตอน start ครั้งแรก ถ้าไม่กันไว้ health server จะพยายาม bind พอร์ตซ้ำ (address already in use)
# และ tree.sync() จะถูกยิงถี่ๆ จนเสี่ยงโดน Discord rate-limit
_ready_once = False


def has_mod_perms():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        member = interaction.guild.get_member(interaction.user.id)
        if member is None:
            if isinstance(interaction.user, discord.Member):
                member = interaction.user
            else:
                return False
        try:
            perms = member.guild_permissions
        except AttributeError:
            return False
        return perms.manage_channels or perms.manage_guild or perms.administrator
    return app_commands.check(predicate)


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandInvokeError):
        error = error.original
    if isinstance(error, app_commands.CheckFailure):
        msg = "❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้ (ต้องมีสิทธิ์ Manage Channels หรือ Manage Server)"
    elif isinstance(error, discord.Forbidden):
        msg = "❌ บอทไม่มีสิทธิ์ทำรายการนี้"
    elif isinstance(error, discord.HTTPException):
        msg = f"❌ Discord API Error: {error.status}"
    else:
        log.exception(f"Unhandled command error in /{getattr(interaction.command, 'qualified_name', '?')}: {error}")
        msg = "❌ เกิดข้อผิดพลาดภายใน"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


# ─────────────────────────────────────────────
# Voice tracking core logic
# ─────────────────────────────────────────────

async def get_tracked_channel_ids(guild_id: int) -> set:
    pool = await db.get_pool()
    rows = await pool.fetch("SELECT channel_id FROM tracked_channels WHERE guild_id = $1", guild_id)
    return {r["channel_id"] for r in rows}


async def open_session(guild_id: int, channel_id: int, user_id: int):
    pool = await db.get_pool()
    existing = await pool.fetchval(
        "SELECT 1 FROM voice_sessions WHERE guild_id = $1 AND channel_id = $2 AND user_id = $3 AND left_at IS NULL",
        guild_id, channel_id, user_id
    )
    if existing:
        return  # กันเปิด session ซ้ำ (เช่นตอน reconcile ตอนบอท restart)
    await pool.execute(
        "INSERT INTO voice_sessions (guild_id, channel_id, user_id, joined_at) VALUES ($1, $2, $3, $4)",
        guild_id, channel_id, user_id, datetime.now(timezone.utc)
    )


async def close_open_session(guild_id: int, channel_id: int, user_id: int):
    pool = await db.get_pool()
    row = await pool.fetchrow(
        """
        SELECT id, joined_at FROM voice_sessions
        WHERE guild_id = $1 AND channel_id = $2 AND user_id = $3 AND left_at IS NULL
        ORDER BY joined_at DESC LIMIT 1
        """,
        guild_id, channel_id, user_id
    )
    if not row:
        return
    now = datetime.now(timezone.utc)
    duration = int((now - row["joined_at"]).total_seconds())
    await pool.execute(
        "UPDATE voice_sessions SET left_at = $1, duration_seconds = $2 WHERE id = $3",
        now, duration, row["id"]
    )


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return  # ไม่นับบอทตัวอื่นๆ ที่เข้าห้องเสียง (เช่นบอทเพลง)

    before_id = before.channel.id if before.channel else None
    after_id = after.channel.id if after.channel else None
    if before_id == after_id:
        return  # ไม่ได้เปลี่ยนห้อง (แค่ mute/deafen/สลับสถานะ) ไม่ต้องบันทึก

    tracked = await get_tracked_channel_ids(member.guild.id)

    if before_id in tracked:
        await close_open_session(member.guild.id, before_id, member.id)
    if after_id in tracked:
        await open_session(member.guild.id, after_id, member.id)


async def reconcile_open_sessions():
    """
    เผื่อบอท restart ระหว่างที่มีคนอยู่ในห้องอยู่แล้ว — เปิด session ให้คนที่อยู่ในห้อง
    ติดตามอยู่ตอนนี้แต่ยังไม่มี session เปิดค้างอยู่ใน DB (เช่น join ตอนบอทดับ)
    เรียกครั้งเดียวตอน on_ready
    """
    for guild in bot.guilds:
        tracked = await get_tracked_channel_ids(guild.id)
        for channel_id in tracked:
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                continue
            for m in channel.members:
                if not m.bot:
                    await open_session(guild.id, channel_id, m.id)


# Health
async def health_handler(request):
    return web.Response(text="OK", status=200)


async def start_health_server():
    app = web.Application()
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("BOT_HEALTH_PORT", 8100))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"Health server running on port {port}")


@bot.event
async def on_ready():
    global _ready_once
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

    if _ready_once:
        # reconnect ครั้งถัดไป: อัปเดตแค่ presence และ reconcile session ที่ค้าง (idempotent) พอ
        # ไม่ต้อง init DB / sync คำสั่ง / เปิด health server ซ้ำ
        try:
            await reconcile_open_sessions()
        except Exception as e:
            log.error(f"Reconcile failed: {e}")
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="ห้องเสียง | /voice now"))
        return

    _ready_once = True

    try:
        await db.init_db()
    except Exception as e:
        log.error(f"DB init failed: {e}")
    try:
        await reconcile_open_sessions()
    except Exception as e:
        log.error(f"Reconcile failed: {e}")
    try:
        synced = await tree.sync()
        log.info(f"Synced {len(synced)} commands")
    except Exception as e:
        log.error(f"Sync failed: {e}")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="ห้องเสียง | /voice now"))
    asyncio.create_task(start_health_server())


# ─────────────────────────────────────────────
# Slash Commands: /track (จัดการห้องที่ติดตาม)
# ─────────────────────────────────────────────

track_group = app_commands.Group(name="track", description="จัดการห้องเสียงที่ต้องการติดตาม")


@track_group.command(name="add", description="เริ่มติดตามห้องเสียง")
@has_mod_perms()
@app_commands.describe(channel="ห้องเสียงที่ต้องการติดตาม")
async def track_add(interaction: discord.Interaction, channel: TrackableChannel):
    pool = await db.get_pool()
    await pool.execute(
        """
        INSERT INTO tracked_channels (guild_id, channel_id, added_by)
        VALUES ($1, $2, $3)
        ON CONFLICT (guild_id, channel_id) DO NOTHING
        """,
        interaction.guild.id, channel.id, interaction.user.id
    )
    # เผื่อมีคนอยู่ในห้องอยู่แล้วตอนเริ่มติดตาม เปิด session ให้ทันที
    for m in channel.members:
        if not m.bot:
            await open_session(interaction.guild.id, channel.id, m.id)
    await interaction.response.send_message(f"✅ เริ่มติดตามห้อง {channel.mention} แล้ว", ephemeral=True)


@track_group.command(name="remove", description="เลิกติดตามห้องเสียง")
@has_mod_perms()
@app_commands.describe(channel="ห้องเสียงที่ต้องการเลิกติดตาม")
async def track_remove(interaction: discord.Interaction, channel: TrackableChannel):
    pool = await db.get_pool()
    # ปิด session ค้างของห้องนี้ทั้งหมดก่อนเลิกติดตาม เพื่อให้สถิติล่าสุดถูกต้อง
    open_rows = await pool.fetch(
        "SELECT id, joined_at FROM voice_sessions WHERE guild_id = $1 AND channel_id = $2 AND left_at IS NULL",
        interaction.guild.id, channel.id
    )
    now = datetime.now(timezone.utc)
    for r in open_rows:
        duration = int((now - r["joined_at"]).total_seconds())
        await pool.execute(
            "UPDATE voice_sessions SET left_at = $1, duration_seconds = $2 WHERE id = $3",
            now, duration, r["id"]
        )
    await pool.execute(
        "DELETE FROM tracked_channels WHERE guild_id = $1 AND channel_id = $2",
        interaction.guild.id, channel.id
    )
    await interaction.response.send_message(f"🛑 เลิกติดตามห้อง {channel.mention} แล้ว", ephemeral=True)


@track_group.command(name="list", description="ดูรายการห้องเสียงที่ติดตามอยู่")
async def track_list(interaction: discord.Interaction):
    pool = await db.get_pool()
    rows = await pool.fetch(
        "SELECT channel_id FROM tracked_channels WHERE guild_id = $1",
        interaction.guild.id
    )
    if not rows:
        return await interaction.response.send_message("ยังไม่มีห้องเสียงที่ติดตามอยู่ ใช้ `/track add` เพื่อเริ่ม", ephemeral=True)
    lines = []
    for r in rows:
        ch = interaction.guild.get_channel(r["channel_id"])
        lines.append(f"- {ch.mention}" if ch else f"- `{r['channel_id']}` (ห้องถูกลบไปแล้ว)")
    await interaction.response.send_message("**ห้องเสียงที่ติดตามอยู่:**\n" + "\n".join(lines), ephemeral=True)


tree.add_command(track_group)


# ─────────────────────────────────────────────
# Slash Commands: /voice (ดูข้อมูล/สถิติ)
# ─────────────────────────────────────────────

voice_group = app_commands.Group(name="voice", description="ดูข้อมูลและสถิติห้องเสียง")


@voice_group.command(name="now", description="ดูว่าใครอยู่ในห้องตอนนี้ และอยู่มานานเท่าไหร่")
@app_commands.describe(channel="ห้องเสียงที่ต้องการดู")
async def voice_now(interaction: discord.Interaction, channel: TrackableChannel):
    pool = await db.get_pool()
    members_in = [m for m in channel.members if not m.bot]

    if not members_in:
        embed = discord.Embed(title=f"🔊 {channel.name}", description="ไม่มีใครอยู่ในห้องตอนนี้", color=0x5865F2)
        embed.set_footer(text="ออนไลน์ในห้องนี้: 0 คน")
        return await interaction.response.send_message(embed=embed)

    now = datetime.now(timezone.utc)
    lines = []
    for m in members_in:
        row = await pool.fetchrow(
            """
            SELECT joined_at FROM voice_sessions
            WHERE guild_id = $1 AND channel_id = $2 AND user_id = $3 AND left_at IS NULL
            ORDER BY joined_at DESC LIMIT 1
            """,
            interaction.guild.id, channel.id, m.id
        )
        if row:
            mins = int((now - row["joined_at"]).total_seconds() // 60)
            lines.append(f"🔊 {m.mention} — **{mins} นาที**")
        else:
            lines.append(f"🔊 {m.mention} — ไม่ทราบเวลาเข้า (ห้องนี้ยังไม่ถูกติดตาม ใช้ `/track add`)")

    embed = discord.Embed(
        title=f"🔊 {channel.name}",
        description="\n".join(lines),
        color=0x57F287,
        timestamp=now
    )
    embed.set_footer(text=f"ออนไลน์ในห้องนี้: {len(members_in)} คน")
    await interaction.response.send_message(embed=embed)


@voice_group.command(name="stats", description="สรุปเวลารวมของแต่ละคนในห้อง (สูงสุด 200 คน)")
@app_commands.describe(channel="ห้องเสียง", days="ย้อนหลังกี่วัน (ค่าเริ่มต้น 7 วัน, ใส่ 0 = ทั้งหมด)")
async def voice_stats(interaction: discord.Interaction, channel: TrackableChannel, days: app_commands.Range[int, 0, 365] = 7):
    await interaction.response.defer()
    pool = await db.get_pool()

    if days > 0:
        query = """
            SELECT user_id,
                   SUM(COALESCE(duration_seconds, EXTRACT(EPOCH FROM (NOW() - joined_at))::INT)) AS total_seconds,
                   COUNT(*) AS session_count
            FROM voice_sessions
            WHERE guild_id = $1 AND channel_id = $2 AND joined_at >= NOW() - ($3 || ' days')::interval
            GROUP BY user_id
            ORDER BY total_seconds DESC
            LIMIT 200
        """
        rows = await pool.fetch(query, interaction.guild.id, channel.id, str(days))
    else:
        query = """
            SELECT user_id,
                   SUM(COALESCE(duration_seconds, EXTRACT(EPOCH FROM (NOW() - joined_at))::INT)) AS total_seconds,
                   COUNT(*) AS session_count
            FROM voice_sessions
            WHERE guild_id = $1 AND channel_id = $2
            GROUP BY user_id
            ORDER BY total_seconds DESC
            LIMIT 200
        """
        rows = await pool.fetch(query, interaction.guild.id, channel.id)

    if not rows:
        return await interaction.followup.send(f"ยังไม่มีข้อมูลของห้อง {channel.mention}")

    lines = []
    for i, r in enumerate(rows, 1):
        mins = int(r["total_seconds"] or 0) // 60
        member = interaction.guild.get_member(r["user_id"])
        name = member.display_name if member else f"Unknown ({r['user_id']})"
        lines.append(f"`{i}.` {name} — **{mins} นาที** ({r['session_count']} ครั้ง)")

    period_text = f"{days} วันล่าสุด" if days > 0 else "ทั้งหมดตั้งแต่เริ่มติดตาม"

    # Discord จำกัด embed description ไว้ที่ 4096 ตัวอักษร — พอเพิ่มโควต้าเป็น 200 คน
    # รายชื่ออาจยาวเกินพอดี เลยแบ่งเป็นหลาย embed (หน้าละ 40 คน) กันข้อความเกินแล้วส่งไม่ออก
    PAGE_SIZE = 40
    pages = [lines[i:i + PAGE_SIZE] for i in range(0, len(lines), PAGE_SIZE)]

    for page_num, page_lines in enumerate(pages, 1):
        title = f"📊 สถิติห้อง {channel.name}"
        if len(pages) > 1:
            title += f" (หน้า {page_num}/{len(pages)})"
        embed = discord.Embed(
            title=title,
            description="\n".join(page_lines),
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text=f"ช่วงเวลา: {period_text}")
        await interaction.followup.send(embed=embed)


@voice_group.command(name="export", description="ส่งออกข้อมูลการเข้าห้องเป็นไฟล์ CSV (เปิดด้วย Excel ได้)")
@app_commands.describe(channel="ห้องเสียง", days="ย้อนหลังกี่วัน (ค่าเริ่มต้น 30 วัน, ใส่ 0 = ทั้งหมด)")
async def voice_export(interaction: discord.Interaction, channel: TrackableChannel, days: app_commands.Range[int, 0, 365] = 30):
    await interaction.response.defer(ephemeral=True)
    pool = await db.get_pool()

    if days > 0:
        query = """
            SELECT user_id, joined_at, left_at,
                   COALESCE(duration_seconds, EXTRACT(EPOCH FROM (NOW() - joined_at))::INT) AS duration_seconds
            FROM voice_sessions
            WHERE guild_id = $1 AND channel_id = $2 AND joined_at >= NOW() - ($3 || ' days')::interval
            ORDER BY joined_at ASC
        """
        rows = await pool.fetch(query, interaction.guild.id, channel.id, str(days))
    else:
        query = """
            SELECT user_id, joined_at, left_at,
                   COALESCE(duration_seconds, EXTRACT(EPOCH FROM (NOW() - joined_at))::INT) AS duration_seconds
            FROM voice_sessions
            WHERE guild_id = $1 AND channel_id = $2
            ORDER BY joined_at ASC
        """
        rows = await pool.fetch(query, interaction.guild.id, channel.id)

    if not rows:
        return await interaction.followup.send(f"ยังไม่มีข้อมูลของห้อง {channel.mention}", ephemeral=True)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["ชื่อผู้ใช้", "user_id", "เวลาเข้า (UTC)", "เวลาออก (UTC)", "ระยะเวลา (นาที)"])
    for r in rows:
        member = interaction.guild.get_member(r["user_id"])
        name = member.display_name if member else f"Unknown"
        left_text = r["left_at"].strftime("%Y-%m-%d %H:%M:%S") if r["left_at"] else "ยังอยู่ในห้อง"
        writer.writerow([
            name,
            r["user_id"],
            r["joined_at"].strftime("%Y-%m-%d %H:%M:%S"),
            left_text,
            round((r["duration_seconds"] or 0) / 60, 1),
        ])

    # ใส่ BOM (utf-8-sig) เพื่อให้ Excel เปิดแล้วอ่านภาษาไทยได้ถูกต้อง ไม่ขึ้นตัวอักษรมั่ว
    data = buffer.getvalue().encode("utf-8-sig")
    file = discord.File(io.BytesIO(data), filename=f"voice_{channel.name}_{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv")
    await interaction.followup.send(
        content=f"📄 ข้อมูลห้อง {channel.mention} ({len(rows)} session)",
        file=file,
        ephemeral=True
    )


@voice_group.command(name="graph", description="ดูกราฟกิจกรรม (เวลารวมต่อวัน) ของห้องเสียง")
@app_commands.describe(channel="ห้องเสียง", days="ย้อนหลังกี่วัน (ค่าเริ่มต้น 14 วัน)")
async def voice_graph(interaction: discord.Interaction, channel: TrackableChannel, days: app_commands.Range[int, 1, 90] = 14):
    await interaction.response.defer()
    pool = await db.get_pool()

    query = """
        SELECT date_trunc('day', joined_at) AS day,
               SUM(COALESCE(duration_seconds, EXTRACT(EPOCH FROM (NOW() - joined_at))::INT)) / 60.0 AS total_minutes
        FROM voice_sessions
        WHERE guild_id = $1 AND channel_id = $2 AND joined_at >= NOW() - ($3 || ' days')::interval
        GROUP BY day
        ORDER BY day ASC
    """
    rows = await pool.fetch(query, interaction.guild.id, channel.id, str(days))

    if not rows:
        return await interaction.followup.send(f"ยังไม่มีข้อมูลของห้อง {channel.mention} ในช่วง {days} วันนี้")

    dates = [r["day"] for r in rows]
    minutes = [float(r["total_minutes"] or 0) for r in rows]

    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.plot(dates, minutes, marker="o", color="#5865F2", linewidth=2)
    ax.fill_between(dates, minutes, alpha=0.15, color="#5865F2")
    ax.set_title(f"กิจกรรมห้อง {channel.name} (ย้อนหลัง {days} วัน)")
    ax.set_ylabel("นาทีรวมต่อวัน")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    fig.autofmt_xdate()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    img_buffer = io.BytesIO()
    fig.savefig(img_buffer, format="png", dpi=120)
    plt.close(fig)
    img_buffer.seek(0)

    file = discord.File(img_buffer, filename="voice_graph.png")
    embed = discord.Embed(title=f"📈 กราฟกิจกรรม {channel.name}", color=0x5865F2)
    embed.set_image(url="attachment://voice_graph.png")
    await interaction.followup.send(embed=embed, file=file)


tree.add_command(voice_group)


# ─────────────────────────────────────────────
# Player Introduction Board (กระดานแนะนำตัวผู้เล่น)
# ─────────────────────────────────────────────

async def _get_class_options(guild: discord.Guild, current: Optional[str] = None) -> list:
    """ดึง role อาชีพที่แอดมินตั้งไว้ผ่าน /setup-jobs มาเป็นตัวเลือกใน dropdown เลือกอาชีพ"""
    pool = await db.get_pool()
    rows = await pool.fetch("SELECT role_id FROM job_roles WHERE guild_id = $1", guild.id)
    options = []
    for r in rows:
        role = guild.get_role(r["role_id"])
        if role:
            # value เก็บ role_id (ไว้ใช้ติดตั้ง role จริงให้สมาชิกตอน submit) ส่วน label โชว์ชื่อ role
            options.append(discord.SelectOption(label=role.name, value=str(role.id), default=(role.name == current)))
    return options[:25]  # Discord จำกัดตัวเลือกใน Select ไว้ที่ 25


async def _apply_class_role(interaction: discord.Interaction, class_role_id: Optional[int]):
    """
    ติดตั้ง role อาชีพที่เลือกให้สมาชิกจริงใน Discord — ถอด role อาชีพอื่นที่เคยมีออกก่อน
    (เผื่อเปลี่ยนอาชีพตอน /edit-profile) เพื่อให้มี role อาชีพติดตัวได้แค่อันเดียว
    ต้องการสิทธิ์ Manage Roles และ role ของบอทต้องอยู่สูงกว่า role อาชีพในลำดับชั้น ไม่งั้นจะข้ามแบบเงียบๆ
    """
    if class_role_id is None:
        return
    role = interaction.guild.get_role(class_role_id)
    if role is None:
        return
    member = interaction.user
    if not isinstance(member, discord.Member):
        return

    pool = await db.get_pool()
    job_role_ids = {r["role_id"] for r in await pool.fetch("SELECT role_id FROM job_roles WHERE guild_id = $1", interaction.guild.id)}
    try:
        roles_to_remove = [r for r in member.roles if r.id in job_role_ids and r.id != role.id]
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason="เปลี่ยนอาชีพ (ระบบแนะนำตัว)")
        if role not in member.roles:
            await member.add_roles(role, reason="เลือกอาชีพ (ระบบแนะนำตัว)")
    except discord.Forbidden:
        log.warning(f"ไม่มีสิทธิ์ตั้ง role อาชีพให้ {member.id} (เช็คว่า role บอทอยู่สูงกว่า role อาชีพในลำดับชั้นหรือยัง)")
    except Exception as e:
        log.error(f"ตั้ง role อาชีพให้ {member.id} ไม่สำเร็จ: {e}")


def _build_profile_embed(row: dict, member: discord.abc.User) -> discord.Embed:
    embed = discord.Embed(title="🎮 แนะนำตัวผู้เล่น", color=0x57F287)
    embed.add_field(name="ชื่อในเกม", value=row["in_game_name"], inline=False)
    embed.add_field(name="ชื่อในดิส", value=row["discord_name"], inline=False)
    embed.add_field(name="อาชีพที่เล่น", value=row["character_class"], inline=False)
    embed.add_field(name="ผู้ลงทะเบียน", value=member.mention, inline=False)
    ts = row["created_at"].astimezone(BANGKOK_TZ)
    embed.add_field(name="วันที่ลงทะเบียน", value=ts.strftime("%d/%m/%Y %H:%M น."), inline=False)
    return embed


async def _post_profile_embed(interaction: discord.Interaction):
    """
    โพสต์ Embed แนะนำตัว — ถ้าตั้ง log_channel ไว้ (ผ่าน /setup-introduction) จะโพสต์ที่ห้องนั้น
    ห้องเดียว (แยกจากห้องกระดานปุ่ม ไม่ซ้ำ 2 ห้อง) ถ้าไม่ได้ตั้ง log_channel ไว้ ก็ fallback ไปโพสต์ที่
    ห้องกระดานปุ่มแทน — ถ้ายังไม่ตั้งค่าห้องไหนเลย ก็ข้ามไปเงียบๆ
    """
    pool = await db.get_pool()
    settings_row = await pool.fetchrow(
        "SELECT intro_channel, log_channel FROM intro_settings WHERE guild_id = $1", interaction.guild.id
    )
    if not settings_row:
        return

    channel_id = settings_row["log_channel"] or settings_row["intro_channel"]
    channel = interaction.guild.get_channel(channel_id) if channel_id else None
    if channel is None:
        return

    row = await pool.fetchrow(
        "SELECT * FROM player_profiles WHERE guild_id = $1 AND discord_user_id = $2",
        interaction.guild.id, interaction.user.id
    )
    embed = _build_profile_embed(row, interaction.user)
    try:
        await channel.send(embed=embed)
    except Exception as e:
        log.error(f"โพสต์ Embed แนะนำตัวไปห้อง {channel_id} ไม่สำเร็จ: {e}")


def _class_color(guild: discord.Guild, character_class: str) -> tuple:
    """สี role อาชีพจริงในดิส (RGB 0-1) — ใช้ role ที่ตั้งผ่าน /setup-jobs เป็นแหล่งสี ไม่ hardcode อาชีพ"""
    role = discord.utils.get(guild.roles, name=character_class)
    if role and role.color.value != 0:
        return (role.color.r / 255, role.color.g / 255, role.color.b / 255)
    return (0.6, 0.6, 0.6)  # เทา — เผื่อ role ถูกลบไปแล้วหรือไม่ได้ตั้งสีไว้


def _render_player_board_image(guild: discord.Guild, rows: list) -> io.BytesIO:
    """
    วาดตารางรายชื่อสมาชิก (ชื่อในเกม / ชื่อในดิส / อาชีพ) เป็นรูปภาพ สีต่อแถวอิงจากสี role อาชีพจริงในดิส
    เขียนแยกจาก refresh_player_board ไว้ เผื่ออนาคตจะเพิ่มคอลัมน์อื่น (เช่น เช็คชื่อ WOE รายสัปดาห์) ทีหลัง
    """
    headers = ["ชื่อในเกม", "ชื่อในดิส", "อาชีพ"]
    table_data = [[r["in_game_name"], r["discord_name"], r["character_class"]] for r in rows]

    fig_height = 0.6 + 0.5 * (len(rows) + 1)
    fig, ax = plt.subplots(figsize=(8, fig_height))
    ax.axis("off")

    table = ax.table(cellText=table_data, colLabels=headers, cellLoc="center", loc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(11)
    table.auto_set_column_width(col=list(range(len(headers))))
    table.scale(1, 1.8)

    for col in range(len(headers)):
        cell = table[0, col]
        cell.set_facecolor((0.85, 0.45, 0.25))
        cell.set_text_props(weight="bold", color="white")

    for i, r in enumerate(rows, start=1):
        color = _class_color(guild, r["character_class"])
        light = tuple(c * 0.35 + 0.65 for c in color)
        table[i, 0].set_facecolor(light)
        table[i, 1].set_facecolor(light)
        table[i, 2].set_facecolor(color)
        table[i, 2].set_text_props(weight="bold", color="white")

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


async def refresh_player_board(guild: discord.Guild):
    """
    อัปเดตรูปกระดานรายชื่อสมาชิกที่ตั้งไว้ผ่าน /setup-playerboard (ถ้ามี) — เรียกทุกครั้งที่ player_profiles
    เปลี่ยน (แนะนำตัวใหม่ / แก้ไข / ถูกลบ) ถ้ายังไม่เคยตั้งกระดานไว้ ก็ข้ามไปเงียบๆ
    """
    pool = await db.get_pool()
    board_row = await pool.fetchrow("SELECT channel_id, message_id FROM player_board WHERE guild_id = $1", guild.id)
    if not board_row:
        return
    channel = guild.get_channel(board_row["channel_id"])
    if channel is None:
        return

    rows = await pool.fetch("SELECT * FROM player_profiles WHERE guild_id = $1 ORDER BY in_game_name", guild.id)
    try:
        message = await channel.fetch_message(board_row["message_id"])
        if rows:
            img = _render_player_board_image(guild, rows)
            await message.edit(content=None, attachments=[discord.File(img, filename="player_board.png")])
        else:
            await message.edit(content="ยังไม่มีใครแนะนำตัวเลย", attachments=[])
    except discord.NotFound:
        log.warning(f"หาข้อความกระดานรายชื่อสมาชิกของ guild {guild.id} ไม่เจอ (อาจถูกลบไปแล้ว) — ต้องรัน /setup-playerboard ใหม่")
    except Exception as e:
        log.error(f"อัปเดตกระดานรายชื่อสมาชิกไม่สำเร็จ: {e}")


def _format_discord_name(member: discord.abc.User, in_game_name: str) -> str:
    """ชื่อในดิส = ชื่อเล่นในเซิร์ฟเวอร์ (nickname) ต่อด้วยชื่อในเกมในวงเล็บ เช่น 'หนุ่ม (จ๊ก)'"""
    return f"{member.display_name} ({in_game_name})"


class IntroductionModal(discord.ui.Modal, title="แนะนำตัวผู้เล่น"):
    in_game_name = discord.ui.TextInput(label="ชื่อในเกม", placeholder="เช่น RachitTH", max_length=100)

    def __init__(self, character_class: str, class_role_id: Optional[int] = None):
        super().__init__()
        self.character_class = character_class
        self.class_role_id = class_role_id

    async def on_submit(self, interaction: discord.Interaction):
        pool = await db.get_pool()
        existing = await pool.fetchrow(
            "SELECT 1 FROM player_profiles WHERE guild_id = $1 AND discord_user_id = $2",
            interaction.guild.id, interaction.user.id
        )
        if existing:
            await interaction.response.send_message(
                "คุณเคยแนะนำตัวแล้ว — กดปุ่ม ✏️ แก้ไขชื่อแนะนำตัว (คนเก่า) แทน", ephemeral=True
            )
            return

        # ใช้ชื่อเล่นในดิส (nickname ในเซิร์ฟเวอร์ ถ้าไม่ได้ตั้งจะ fallback เป็น username) แทนให้พิมพ์เอง
        # ต่อด้วยชื่อในเกมในวงเล็บ เช่น "หนุ่ม (จ๊ก)" ให้ดูออกง่ายว่าใครในดิสคือใครในเกม
        discord_name = _format_discord_name(interaction.user, str(self.in_game_name))
        await pool.execute(
            """
            INSERT INTO player_profiles (guild_id, discord_user_id, in_game_name, discord_name, character_class)
            VALUES ($1, $2, $3, $4, $5)
            """,
            interaction.guild.id, interaction.user.id,
            str(self.in_game_name), discord_name, self.character_class
        )
        await _apply_class_role(interaction, self.class_role_id)
        await interaction.response.send_message("✅ แนะนำตัวสำเร็จแล้ว!", ephemeral=True)
        await _post_profile_embed(interaction)
        await refresh_player_board(interaction.guild)


class EditProfileModal(discord.ui.Modal, title="แก้ไขข้อมูลแนะนำตัว"):
    in_game_name = discord.ui.TextInput(label="ชื่อในเกม", max_length=100)

    def __init__(self, existing: dict, character_class: str, class_role_id: Optional[int] = None):
        super().__init__()
        self.character_class = character_class
        self.class_role_id = class_role_id
        self.in_game_name.default = existing["in_game_name"]

    async def on_submit(self, interaction: discord.Interaction):
        pool = await db.get_pool()
        discord_name = _format_discord_name(interaction.user, str(self.in_game_name))
        await pool.execute(
            """
            UPDATE player_profiles
            SET in_game_name = $3, discord_name = $4, character_class = $5, updated_at = NOW()
            WHERE guild_id = $1 AND discord_user_id = $2
            """,
            interaction.guild.id, interaction.user.id,
            str(self.in_game_name), discord_name, self.character_class
        )
        await _apply_class_role(interaction, self.class_role_id)
        await interaction.response.send_message("✅ แก้ไขข้อมูลเรียบร้อยแล้ว", ephemeral=True)
        await refresh_player_board(interaction.guild)


class ClassSelectView(discord.ui.View):
    """dropdown เลือกอาชีพก่อนเปิด Modal (Modal ใส่ select menu ไม่ได้ ต้องแยกเป็น 2 ขั้นตอน)"""

    def __init__(self, options: list, *, editing: bool = False, existing: Optional[dict] = None):
        super().__init__(timeout=180)
        self.editing = editing
        self.existing = existing
        self.select_item = discord.ui.Select(placeholder="เลือกอาชีพที่เล่น", options=options)
        self.select_item.callback = self.on_select
        self.add_item(self.select_item)

    async def on_select(self, interaction: discord.Interaction):
        class_role_id = int(self.select_item.values[0])
        role = interaction.guild.get_role(class_role_id)
        character_class = role.name if role else "ไม่ทราบ"
        if self.editing:
            modal = EditProfileModal(existing=self.existing, character_class=character_class, class_role_id=class_role_id)
        else:
            modal = IntroductionModal(character_class=character_class, class_role_id=class_role_id)
        await interaction.response.send_modal(modal)


class IntroductionBoardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📝 แนะนำตัว (คนใหม่)", style=discord.ButtonStyle.primary, custom_id="intro_board_open")
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        pool = await db.get_pool()
        existing = await pool.fetchrow(
            "SELECT 1 FROM player_profiles WHERE guild_id = $1 AND discord_user_id = $2",
            interaction.guild.id, interaction.user.id
        )
        if existing:
            await interaction.response.send_message(
                "คุณเคยแนะนำตัวแล้ว — กดปุ่ม ✏️ แก้ไขชื่อแนะนำตัว (คนเก่า) แทน", ephemeral=True
            )
            return
        options = await _get_class_options(interaction.guild)
        if not options:
            await interaction.response.send_message(
                "ยังไม่ได้ตั้งค่า Role อาชีพ — แจ้งแอดมินให้รัน `/setup-jobs` ก่อน", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "① เลือกอาชีพที่เล่นก่อน แล้วจะเปิดฟอร์มให้กรอกชื่อในเกมต่อ (ชื่อในดิสใช้ชื่อเล่นในเซิร์ฟเวอร์นี้ให้อัตโนมัติ)",
            view=ClassSelectView(options), ephemeral=True
        )

    @discord.ui.button(label="✏️ แก้ไขชื่อแนะนำตัว (คนเก่า)", style=discord.ButtonStyle.secondary, custom_id="intro_board_edit")
    async def open_edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _open_edit_profile(interaction)


class JobRolesSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="เลือก Role อาชีพทั้งหมด (สูงสุด 25)",
                        min_values=1, max_values=25)
    async def select_roles(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM job_roles WHERE guild_id = $1", interaction.guild.id)
                await conn.executemany(
                    "INSERT INTO job_roles (guild_id, role_id) VALUES ($1, $2)",
                    [(interaction.guild.id, r.id) for r in select.values]
                )
        await interaction.response.edit_message(
            content=f"✅ ตั้งค่า Role อาชีพแล้ว ({len(select.values)} อาชีพ): "
                    + ", ".join(r.name for r in select.values),
            view=None
        )


@tree.command(name="setup-jobs", description="ตั้งค่า Role อาชีพที่จะให้สมาชิกเลือกตอนแนะนำตัว")
@has_mod_perms()
async def setup_jobs(interaction: discord.Interaction):
    await interaction.response.send_message(
        "เลือก Role อาชีพทั้งหมดในเซิร์ฟเวอร์ (จะแทนที่รายการเดิมทั้งหมด):",
        view=JobRolesSelectView(), ephemeral=True
    )


@tree.command(name="setup-introduction", description="ตั้งค่ากระดานแนะนำตัวผู้เล่นในห้องที่เลือก")
@has_mod_perms()
@app_commands.describe(
    channel="ห้องที่จะโพสต์กระดานแนะนำตัว (ปุ่มกด)",
    log_channel="ห้องแยกต่างหาก (เช่น #ฐานข้อมูล-ผู้เล่น) สำหรับ Embed แนะนำตัวสำเร็จ — ถ้าตั้งไว้ Embed จะไปห้องนี้แทนห้องกระดานปุ่ม ไม่ซ้ำ 2 ห้อง (ไม่บังคับ)"
)
async def setup_introduction(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    log_channel: Optional[discord.TextChannel] = None
):
    pool = await db.get_pool()
    await pool.execute("""
        INSERT INTO intro_settings (guild_id, intro_channel, log_channel) VALUES ($1, $2, $3)
        ON CONFLICT (guild_id) DO UPDATE SET intro_channel = $2, log_channel = $3
    """, interaction.guild.id, channel.id, log_channel.id if log_channel else None)

    embed = discord.Embed(
        title="🎮 กระดานแนะนำตัวผู้เล่น",
        description="**📝 แนะนำตัว (คนใหม่)** — ยังไม่เคยแนะนำตัว กดเพื่อเลือกอาชีพและกรอกชื่อในเกม\n"
                     "**✏️ แก้ไขชื่อแนะนำตัว (คนเก่า)** — เคยแนะนำตัวแล้ว ต้องการเปลี่ยนชื่อในเกมหรืออาชีพ\n\n"
                     "ชื่อใน Discord ใช้ชื่อเล่นในเซิร์ฟเวอร์ให้อัตโนมัติ",
        color=0xFEE75C
    )
    await channel.send(embed=embed, view=IntroductionBoardView())

    msg = f"ตั้งกระดานแนะนำตัวที่ {channel.mention} เรียบร้อยแล้ว"
    if log_channel:
        msg += f"\nEmbed แนะนำตัวสำเร็จจะไปโพสต์ที่ {log_channel.mention} แทน (ไม่ซ้ำที่ห้องกระดานปุ่ม)"
    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(name="my-profile", description="ดูข้อมูลแนะนำตัวของตัวเอง")
async def my_profile(interaction: discord.Interaction):
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM player_profiles WHERE guild_id = $1 AND discord_user_id = $2",
        interaction.guild.id, interaction.user.id
    )
    if not row:
        await interaction.response.send_message(
            "คุณยังไม่ได้แนะนำตัว กดปุ่ม 📝 แนะนำตัว ที่กระดานแนะนำตัวก่อน", ephemeral=True
        )
        return
    await interaction.response.send_message(embed=_build_profile_embed(row, interaction.user), ephemeral=True)


async def _open_edit_profile(interaction: discord.Interaction):
    """ใช้ร่วมกันระหว่างปุ่ม ✏️ แก้ไขชื่อแนะนำตัว (คนเก่า) บนกระดาน และคำสั่ง /edit-profile"""
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM player_profiles WHERE guild_id = $1 AND discord_user_id = $2",
        interaction.guild.id, interaction.user.id
    )
    if not row:
        await interaction.response.send_message(
            "คุณยังไม่ได้แนะนำตัว — กดปุ่ม 📝 แนะนำตัว (คนใหม่) ก่อน", ephemeral=True
        )
        return
    options = await _get_class_options(interaction.guild, current=row["character_class"])
    if not options:
        await interaction.response.send_message(
            "ยังไม่ได้ตั้งค่า Role อาชีพ — แจ้งแอดมินให้รัน `/setup-jobs` ก่อน", ephemeral=True
        )
        return
    await interaction.response.send_message(
        f"ข้อมูลปัจจุบัน: **{row['in_game_name']}** — {row['character_class']}\n"
        "เลือกอาชีพ (ค่าปัจจุบันถูกเลือกไว้แล้ว) แล้วจะเปิดฟอร์มให้แก้ชื่อในเกมต่อ",
        view=ClassSelectView(options, editing=True, existing=dict(row)), ephemeral=True
    )


@tree.command(name="edit-profile", description="แก้ไขข้อมูลแนะนำตัวของตัวเอง")
async def edit_profile(interaction: discord.Interaction):
    await _open_edit_profile(interaction)


class ConfirmView(discord.ui.View):
    """ปุ่มยืนยัน/ยกเลิก — กดได้เฉพาะคนที่สั่งคำสั่ง, หมดเวลา 60 วินาที, กดได้ครั้งเดียว"""

    def __init__(self, owner_id: int, action, *, danger: bool = False):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.action = action
        self.confirm_btn.style = discord.ButtonStyle.danger if danger else discord.ButtonStyle.success

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("ปุ่มนี้สำหรับคนที่สั่งคำสั่งเท่านั้น", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ ยืนยัน", style=discord.ButtonStyle.danger)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="⏳ กำลังดำเนินการ...", view=None)
        await self.action(interaction)

    @discord.ui.button(label="❌ ยกเลิก", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="ยกเลิกแล้ว ไม่มีอะไรเปลี่ยนแปลง", view=None)


@tree.command(name="reset-introductions", description="ล้างรายชื่อแนะนำตัวทั้งหมด + ถอด Role อาชีพคืนทุกคน (เริ่มเช็คใหม่ตั้งแต่ต้น)")
@has_mod_perms()
async def reset_introductions(interaction: discord.Interaction):
    """
    รีเซ็ตระบบแนะนำตัวทั้งกิลด์ — ลบทุกแถวใน player_profiles และถอด Role อาชีพ (job_roles) ที่ติดตัว
    สมาชิกแต่ละคนออก เพื่อให้ทุกคนกดปุ่ม 📝 แนะนำตัว ใหม่ได้ทันที (ปกติจะโดนกันด้วยเช็ค "คุณเคยแนะนำตัวแล้ว")
    มีขั้นยืนยันด้วยปุ่ม (ConfirmView) กันเผลอกด + จำกัดสิทธิ์ด้วย has_mod_perms()
    ทำงานได้แม้บอทไม่มีสิทธิ์ถอด role บางคน (เช่น role บอทอยู่ต่ำกว่าในลำดับชั้น) จะข้ามคนนั้นไปแบบ log ไว้ ไม่ทำให้คำสั่งทั้งหมดล้ม
    """
    pool = await db.get_pool()
    count = await pool.fetchval("SELECT COUNT(*) FROM player_profiles WHERE guild_id = $1", interaction.guild.id)
    await interaction.response.send_message(
        f"⚠️ **ยืนยันรีเซ็ตระบบแนะนำตัว?**\n"
        f"จะลบรายชื่อทั้งหมด **{count} คน** และถอด Role อาชีพออกจากทุกคน\n"
        "กดยืนยันภายใน 60 วินาที ถ้าไม่ต้องการให้กดยกเลิก",
        view=ConfirmView(interaction.user.id, _do_reset_introductions, danger=True), ephemeral=True
    )


async def _do_reset_introductions(interaction: discord.Interaction):
    pool = await db.get_pool()

    rows = await pool.fetch(
        "SELECT discord_user_id FROM player_profiles WHERE guild_id = $1", interaction.guild.id
    )
    job_role_ids = {r["role_id"] for r in await pool.fetch(
        "SELECT role_id FROM job_roles WHERE guild_id = $1", interaction.guild.id
    )}

    roles_removed_from = 0
    for row in rows:
        member = interaction.guild.get_member(row["discord_user_id"])
        if member is None:
            continue
        roles_to_remove = [r for r in member.roles if r.id in job_role_ids]
        if not roles_to_remove:
            continue
        try:
            await member.remove_roles(*roles_to_remove, reason="รีเซ็ตระบบแนะนำตัว (/reset-introductions)")
            roles_removed_from += 1
        except discord.Forbidden:
            log.warning(f"ไม่มีสิทธิ์ถอด role อาชีพจาก {member.id} ตอนรีเซ็ตแนะนำตัว")
        except Exception as e:
            log.error(f"ถอด role อาชีพจาก {member.id} ไม่สำเร็จ: {e}")

    total = len(rows)
    await pool.execute("DELETE FROM player_profiles WHERE guild_id = $1", interaction.guild.id)
    await refresh_player_board(interaction.guild)

    await interaction.followup.send(
        f"🗑️ รีเซ็ตระบบแนะนำตัวเรียบร้อย — ลบรายชื่อ {total} คน, ถอด Role อาชีพออกจาก {roles_removed_from} คน\n"
        "ทุกคนกดปุ่ม 📝 แนะนำตัว ที่กระดานเดิมเพื่อเริ่มใหม่ได้ทันที",
        ephemeral=True
    )


@tree.command(name="restore-introductions", description="กู้ข้อมูลแนะนำตัวคืนจาก Embed เก่าในห้องบันทึก (แอดมิน)")
@has_mod_perms()
@app_commands.describe(channel="ห้องที่มี Embed แนะนำตัวเก่า (ไม่ใส่ = ห้องที่ตั้งไว้ใน /setup-introduction)")
async def restore_introductions(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    """
    กู้ player_profiles คืนจาก Embed "🎮 แนะนำตัวผู้เล่น" ที่บอทเคยโพสต์ไว้ (ใช้หลังเผลอ /reset-introductions)
    ไม่เขียนทับคนที่แนะนำตัวใหม่ไปแล้ว + ติด Role อาชีพคืนให้
    """
    ch_text = channel.mention if channel else "ห้องที่ตั้งไว้ใน /setup-introduction"

    async def run(inter: discord.Interaction):
        await _do_restore_introductions(inter, channel)

    await interaction.response.send_message(
        f"♻️ **ยืนยันกู้ข้อมูลแนะนำตัว?**\n"
        f"จะอ่าน Embed แนะนำตัวเก่าจาก {ch_text} แล้วใส่รายชื่อ + Role อาชีพคืน\n"
        "(ไม่เขียนทับคนที่แนะนำตัวใหม่ไปแล้ว) กดยืนยันภายใน 60 วินาที",
        view=ConfirmView(interaction.user.id, run), ephemeral=True
    )


async def _do_restore_introductions(interaction: discord.Interaction, channel: Optional[discord.TextChannel]):
    pool = await db.get_pool()
    guild = interaction.guild

    if channel is None:
        st = await pool.fetchrow("SELECT intro_channel, log_channel FROM intro_settings WHERE guild_id = $1", guild.id)
        cid = (st["log_channel"] or st["intro_channel"]) if st else None
        channel = guild.get_channel(cid) if cid else None
    if channel is None:
        await interaction.followup.send("ไม่พบห้อง — ใส่ตัวเลือก channel เอง", ephemeral=True)
        return

    found = {}
    async for msg in channel.history(limit=None, oldest_first=True):
        if msg.author.id != bot.user.id:
            continue
        for e in msg.embeds:
            if e.title != "🎮 แนะนำตัวผู้เล่น":
                continue
            f = {x.name: x.value for x in e.fields}
            m = re.search(r"<@!?(\d+)>", f.get("ผู้ลงทะเบียน", ""))
            if not m or not f.get("ชื่อในเกม") or not f.get("อาชีพที่เล่น"):
                continue
            try:
                created = datetime.strptime(f.get("วันที่ลงทะเบียน", ""), "%d/%m/%Y %H:%M น.").replace(tzinfo=BANGKOK_TZ)
            except ValueError:
                created = msg.created_at
            found[int(m.group(1))] = (f["ชื่อในเกม"], f.get("ชื่อในดิส") or f["ชื่อในเกม"], f["อาชีพที่เล่น"], created)

    if not found:
        await interaction.followup.send(f"ไม่พบ Embed แนะนำตัวใน {channel.mention}", ephemeral=True)
        return

    job_roles = {}
    for r in await pool.fetch("SELECT role_id FROM job_roles WHERE guild_id = $1", guild.id):
        role = guild.get_role(r["role_id"])
        if role:
            job_roles[role.name] = role

    restored = skipped = roles_added = 0
    for uid, (ign, dname, cls, created) in found.items():
        res = await pool.execute(
            """
            INSERT INTO player_profiles (guild_id, discord_user_id, in_game_name, discord_name, character_class, created_at)
            VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (guild_id, discord_user_id) DO NOTHING
            """,
            guild.id, uid, ign, dname, cls, created
        )
        if res.endswith(" 0"):
            skipped += 1
            continue
        restored += 1
        member = guild.get_member(uid)
        role = job_roles.get(cls)
        if member and role and role not in member.roles:
            try:
                await member.add_roles(role, reason="กู้ข้อมูลแนะนำตัว (/restore-introductions)")
                roles_added += 1
            except Exception as ex:
                log.warning(f"ติด role อาชีพคืนให้ {uid} ไม่สำเร็จ: {ex}")

    await refresh_player_board(guild)
    await interaction.followup.send(
        f"♻️ กู้ข้อมูลคืน {restored} คน (ข้าม {skipped} คนที่แนะนำตัวใหม่ไปแล้ว), ติด Role อาชีพคืน {roles_added} คน",
        ephemeral=True
    )


@tree.command(name="player-search", description="ค้นหาผู้เล่นจากชื่อในเกมหรือชื่อในดิส (แอดมิน)")
@has_mod_perms()
@app_commands.describe(query="ชื่อในเกมหรือชื่อในดิส (ค้นแบบบางส่วนได้)")
async def player_search(interaction: discord.Interaction, query: str):
    pool = await db.get_pool()
    rows = await pool.fetch("""
        SELECT * FROM player_profiles
        WHERE guild_id = $1 AND (in_game_name ILIKE $2 OR discord_name ILIKE $2)
        ORDER BY in_game_name LIMIT 15
    """, interaction.guild.id, f"%{query}%")

    if not rows:
        await interaction.response.send_message("ไม่พบผู้เล่นที่ตรงกับคำค้นหา", ephemeral=True)
        return

    lines = [
        f"• **{r['in_game_name']}** (ดิส: {r['discord_name']}, อาชีพ: {r['character_class']}) — <@{r['discord_user_id']}>"
        for r in rows
    ]
    embed = discord.Embed(title=f"🔍 ผลค้นหา: {query}", description="\n".join(lines), color=0x5865F2)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="player-list", description="ดูตารางรายชื่อผู้เล่นที่แนะนำตัวไว้ทั้งหมด (แอดมิน)")
@has_mod_perms()
async def player_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    pool = await db.get_pool()
    rows = await pool.fetch("""
        SELECT * FROM player_profiles WHERE guild_id = $1 ORDER BY in_game_name
    """, interaction.guild.id)

    if not rows:
        await interaction.followup.send("ยังไม่มีใครแนะนำตัวเลย", ephemeral=True)
        return

    lines = [
        f"• **{r['in_game_name']}** (ดิส: {r['discord_name']}, อาชีพ: {r['character_class']}) — <@{r['discord_user_id']}>"
        for r in rows
    ]

    # Discord จำกัด embed description ไว้ที่ 4096 ตัวอักษร แบ่งเป็นหลาย embed (หน้าละ 25 คน) กันข้อความยาวเกินส่งไม่ออก
    PAGE_SIZE = 25
    pages = [lines[i:i + PAGE_SIZE] for i in range(0, len(lines), PAGE_SIZE)]

    for page_num, page_lines in enumerate(pages, 1):
        title = f"📋 ตารางผู้เล่น ({len(rows)} คน)"
        if len(pages) > 1:
            title += f" — หน้า {page_num}/{len(pages)}"
        embed = discord.Embed(title=title, description="\n".join(page_lines), color=0x5865F2)
        await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="remind-introduction", description="แจ้งเตือน (แท็ก) สมาชิกที่ยังไม่ได้แนะนำตัว (แอดมิน)")
@has_mod_perms()
@app_commands.describe(
    channel="ห้องที่จะส่งข้อความแจ้งเตือน (ไม่ใส่ = ห้องปัจจุบัน)",
    role="แจ้งเตือนเฉพาะคนที่มี Role นี้ (ไม่ใส่ = สมาชิกทุกคน)"
)
async def remind_introduction(
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
    role: Optional[discord.Role] = None
):
    await interaction.response.defer(ephemeral=True)
    target = channel or interaction.channel
    pool = await db.get_pool()
    registered = {
        r["discord_user_id"] for r in await pool.fetch(
            "SELECT discord_user_id FROM player_profiles WHERE guild_id = $1", interaction.guild.id
        )
    }
    members = role.members if role else interaction.guild.members
    missing = [m for m in members if not m.bot and m.id not in registered]
    if not missing:
        await interaction.followup.send("✅ ทุกคนแนะนำตัวครบแล้ว", ephemeral=True)
        return

    settings_row = await pool.fetchrow("SELECT intro_channel FROM intro_settings WHERE guild_id = $1", interaction.guild.id)
    board = interaction.guild.get_channel(settings_row["intro_channel"]) if settings_row and settings_row["intro_channel"] else None
    where = f" ที่ {board.mention}" if board else ""
    header = f"📢 **ยังไม่ได้แนะนำตัว {len(missing)} คน** — กรุณากดปุ่ม 📝 แนะนำตัว (คนใหม่){where}\n"

    # แบ่งข้อความไม่ให้เกิน 2000 ตัวอักษรต่อข้อความ
    chunks, cur = [], header
    for m in missing:
        piece = m.mention + " "
        if len(cur) + len(piece) > 1900:
            chunks.append(cur)
            cur = ""
        cur += piece
    chunks.append(cur)
    try:
        for c in chunks:
            await target.send(c, allowed_mentions=discord.AllowedMentions(users=True))
    except discord.Forbidden:
        await interaction.followup.send(f"บอทไม่มีสิทธิ์ส่งข้อความใน {target.mention}", ephemeral=True)
        return
    await interaction.followup.send(f"ส่งแจ้งเตือน {len(missing)} คนที่ {target.mention} แล้ว", ephemeral=True)


@tree.command(name="remind-introduction-dm", description="ส่ง DM แจ้งเตือน (เด้งแจ้งเตือนในดิส) หาคนที่ยังไม่ได้แนะนำตัว (แอดมิน)")
@has_mod_perms()
@app_commands.describe(role="ส่งเฉพาะคนที่มี Role นี้ (ไม่ใส่ = สมาชิกทุกคน)")
async def remind_introduction_dm(interaction: discord.Interaction, role: Optional[discord.Role] = None):
    await interaction.response.defer(ephemeral=True)
    pool = await db.get_pool()
    guild = interaction.guild
    registered = {
        r["discord_user_id"] for r in await pool.fetch(
            "SELECT discord_user_id FROM player_profiles WHERE guild_id = $1", guild.id
        )
    }
    members = role.members if role else guild.members
    missing = [m for m in members if not m.bot and m.id not in registered]
    if not missing:
        return await interaction.followup.send("✅ ทุกคนแนะนำตัวครบแล้ว", ephemeral=True)

    st = await pool.fetchrow("SELECT intro_channel FROM intro_settings WHERE guild_id = $1", guild.id)
    board = guild.get_channel(st["intro_channel"]) if st and st["intro_channel"] else None
    embed = discord.Embed(
        title="📢 คุณยังไม่ได้แนะนำตัว",
        description=(
            f"เซิร์ฟเวอร์ **{guild.name}** ขอให้แนะนำตัวผู้เล่น\n"
            + (f"ไปที่ {board.mention} แล้วกดปุ่ม **📝 แนะนำตัว (คนใหม่)**" if board
               else "ไปที่ห้องกระดานแนะนำตัว แล้วกดปุ่ม **📝 แนะนำตัว (คนใหม่)**")
        ),
        color=0xFEE75C
    )
    view = None
    if board:
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="ไปที่กระดานแนะนำตัว", url=board.jump_url))

    sent, failed = 0, []
    for m in missing:
        try:
            if view:
                await m.send(embed=embed, view=view)
            else:
                await m.send(embed=embed)
            sent += 1
        except (discord.Forbidden, discord.HTTPException):
            failed.append(m)
        await asyncio.sleep(1)  # กันโดน rate limit ของดิส

    msg = f"📨 ส่ง DM แจ้งเตือนสำเร็จ {sent}/{len(missing)} คน"
    if failed:
        msg += (f"\n⚠️ ส่งไม่ได้ {len(failed)} คน (ปิดรับ DM) — ใช้ `/remind-introduction` แท็กในห้องแทน:\n"
                + " ".join(m.mention for m in failed[:40]))
    await interaction.followup.send(msg[:2000], ephemeral=True)


@tree.command(name="player-remove", description="ลบข้อมูลแนะนำตัวของสมาชิก (แอดมิน)")
@has_mod_perms()
@app_commands.describe(member="สมาชิกที่จะลบข้อมูลแนะนำตัว")
async def player_remove(interaction: discord.Interaction, member: discord.Member):
    pool = await db.get_pool()
    existing = await pool.fetchrow(
        "SELECT 1 FROM player_profiles WHERE guild_id = $1 AND discord_user_id = $2",
        interaction.guild.id, member.id
    )
    if not existing:
        await interaction.response.send_message(f"{member.mention} ยังไม่ได้แนะนำตัวไว้", ephemeral=True)
        return

    await pool.execute(
        "DELETE FROM player_profiles WHERE guild_id = $1 AND discord_user_id = $2",
        interaction.guild.id, member.id
    )

    # ถอด role อาชีพออกด้วย (best-effort — เผื่อบอทไม่มีสิทธิ์ Manage Roles หรือ role อาชีพอยู่สูงกว่าบอทในลำดับชั้น)
    try:
        job_role_ids = {r["role_id"] for r in await pool.fetch("SELECT role_id FROM job_roles WHERE guild_id = $1", interaction.guild.id)}
        roles_to_remove = [r for r in member.roles if r.id in job_role_ids]
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason=f"ลบข้อมูลแนะนำตัวโดย {interaction.user}")
    except discord.Forbidden:
        log.warning(f"ไม่มีสิทธิ์ถอด role อาชีพของ {member.id} ตอนลบข้อมูลแนะนำตัว")
    except Exception as e:
        log.error(f"ถอด role อาชีพของ {member.id} ไม่สำเร็จ: {e}")

    await interaction.response.send_message(f"🗑️ ลบข้อมูลแนะนำตัวของ {member.mention} แล้ว", ephemeral=True)
    await refresh_player_board(interaction.guild)


@tree.command(name="setup-playerboard", description="ตั้งค่ากระดานรายชื่อสมาชิก (ชื่อในเกม/ชื่อในดิส/อาชีพ) แบบรูปภาพในห้องที่เลือก")
@has_mod_perms()
@app_commands.describe(channel="ห้องที่จะโพสต์/ปักหมุดกระดานรายชื่อสมาชิก")
async def setup_playerboard(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    pool = await db.get_pool()
    rows = await pool.fetch("SELECT * FROM player_profiles WHERE guild_id = $1 ORDER BY in_game_name", interaction.guild.id)

    if rows:
        img = _render_player_board_image(interaction.guild, rows)
        message = await channel.send(file=discord.File(img, filename="player_board.png"))
    else:
        message = await channel.send("ยังไม่มีใครแนะนำตัวเลย")

    try:
        await message.pin()
    except Exception:
        pass  # ไม่มีสิทธิ์ปักหมุดก็ไม่เป็นไร โพสต์ไว้เฉยๆ ยังใช้ได้

    await pool.execute("""
        INSERT INTO player_board (guild_id, channel_id, message_id) VALUES ($1, $2, $3)
        ON CONFLICT (guild_id) DO UPDATE SET channel_id = $2, message_id = $3
    """, interaction.guild.id, channel.id, message.id)

    await interaction.followup.send(
        f"ตั้งกระดานรายชื่อสมาชิกที่ {channel.mention} เรียบร้อยแล้ว "
        f"จะอัปเดตอัตโนมัติทุกครั้งที่มีคนแนะนำตัว/แก้ไข/ถูกลบข้อมูล",
        ephemeral=True
    )


# ─────────────────────────────────────────────
# ระบบจัดปาร์ตี้ (ตารางหลักจากรูป + กระดานลาแบบเรียลไทม์)
# กติกา:
#  - /party upload แนบรูปตารางปาร์ตี้ → บอทจัดโพยตามรูปทุกช่อง (เฉพาะคนที่อยู่ในรูป)
#  - คนกด "ลา" → เอาออกจากตี้ ไปอยู่กระดานลา (ช่องเดิมว่างไว้)
#  - ถ้าคนลาเป็นพระ → หาตี้ที่มีพระ 2 คนขึ้นไป ย้ายพระ 1 คนมาแทนช่องนั้น
#  - กดยกเลิกลา → ใส่กลับช่องเดิม (ถ้าเคยดึงพระมาแทน จะส่งพระคนนั้นกลับตี้เดิมก่อน)
#  - กระดานตาราง + กระดานลา อัปเดตทันทีทุกครั้ง
# ─────────────────────────────────────────────

PARTY_GROUPS = ["SUN", "MOON", "DARK", "FLASH"]   # ทีมตามตารางหลัก
PARTIES_PER_GROUP = 4                              # ทีมละ 4 ตี้ (รวม 16 ตี้ / 96 ช่อง)
PARTY_SIZE = 6
TEAM_HEADER_COLORS = {                              # สีหัวตี้บนกระดาน (ตามชีต)
    "SUN": (0.94, 0.76, 0.19),
    "MOON": (0.71, 0.65, 0.84),
    "DARK": (0.55, 0.55, 0.55),
    "FLASH": (0.84, 0.65, 0.74),
}
PRIEST_KEYWORDS = ("arch", "priest", "พระ", "พรีช", "บิชอป")


def _is_priest(character_class: Optional[str]) -> bool:
    """พระ = Archbishop / Priest / พรีช (รองรับชื่อ role หลายแบบ และตัวสะกดผิดในชีต เช่น Archbichop)"""
    c = (character_class or "").strip().lower()
    return any(k in c for k in PRIEST_KEYWORDS)


def _party_label(group: str, party_num: int) -> str:
    return f"{group}{party_num:02d}"


async def _generate_party_assignments(pool, guild_id: int):
    """
    สร้างโพยปาร์ตี้อัตโนมัติจาก player_profiles (ตัดคนที่ลาไว้) — ใช้เมื่อไม่มีรูปตาราง
    แจกพระให้ครบทุกตี้ก่อน (ตี้ละ 1) แล้วเติมคนที่เหลือให้ครบ 6 คน/ตี้
    """
    leave_ids = {r["discord_user_id"] for r in await pool.fetch(
        "SELECT discord_user_id FROM party_leave WHERE guild_id = $1", guild_id
    )}
    profiles = await pool.fetch(
        "SELECT discord_user_id, in_game_name, character_class FROM player_profiles WHERE guild_id = $1 ORDER BY in_game_name",
        guild_id
    )
    people = [dict(r) for r in profiles if r["discord_user_id"] not in leave_ids]
    priests = [p for p in people if _is_priest(p["character_class"])]
    others = [p for p in people if not _is_priest(p["character_class"])]

    party_labels = [(g, i + 1) for g in PARTY_GROUPS for i in range(PARTIES_PER_GROUP)]
    total = len(people)
    num_parties = min(len(party_labels), -(-total // PARTY_SIZE)) if total else 0
    active_labels = party_labels[:num_parties]
    assignments = {label: [] for label in active_labels}

    missing_priest_count = 0
    for i, label in enumerate(active_labels):
        if i < len(priests):
            assignments[label].append(priests[i])
        else:
            missing_priest_count += 1

    leftover = priests[len(active_labels):] + others
    idx = 0
    for label in active_labels:
        while len(assignments[label]) < PARTY_SIZE and idx < len(leftover):
            assignments[label].append(leftover[idx])
            idx += 1

    await pool.execute("DELETE FROM party_assignments WHERE guild_id = $1", guild_id)
    rows_to_insert = [
        (guild_id, m["discord_user_id"], group_name, party_num, slot, m["character_class"], m["in_game_name"])
        for (group_name, party_num), members in assignments.items()
        for slot, m in enumerate(members, start=1)
    ]
    if rows_to_insert:
        await pool.executemany(
            """
            INSERT INTO party_assignments (guild_id, discord_user_id, group_name, party_num, slot, character_class, in_game_name)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            rows_to_insert
        )
    return missing_priest_count, len(leftover) - idx


async def _fetch_party_assignments_grouped(pool, guild_id: int) -> dict:
    rows = await pool.fetch(
        """
        SELECT discord_user_id, group_name, party_num, slot, in_game_name, character_class
        FROM party_assignments WHERE guild_id = $1 ORDER BY group_name, party_num, slot
        """,
        guild_id
    )
    grouped = {}
    for r in rows:
        grouped.setdefault((r["group_name"], r["party_num"]), []).append(dict(r))
    return grouped


def _parties_without_priest(grouped: dict) -> list:
    return [
        _party_label(g, p)
        for g in PARTY_GROUPS for p in range(1, PARTIES_PER_GROUP + 1)
        if grouped.get((g, p)) and not any(_is_priest(m["character_class"]) for m in grouped[(g, p)])
    ]


def _party_class_color(guild: discord.Guild, character_class: str) -> tuple:
    """สีอาชีพ: ใช้สีจากชีตก่อน (ให้หน้าตาเหมือนตารางหลัก) ถ้าไม่มีค่อยใช้สี role ในดิส"""
    c = (character_class or "").lower()
    for name, rgb in party_ocr.CLASS_COLORS.items():
        if name.lower()[:6] == c[:6]:
            return tuple(v / 255 for v in rgb)
    if _is_priest(character_class):
        return tuple(v / 255 for v in party_ocr.CLASS_COLORS["Archbishop"])
    return _class_color(guild, character_class)


def _render_party_board_image(guild: discord.Guild, grouped: dict) -> io.BytesIO:
    """วาดตารางปาร์ตี้ 4 ทีม x 4 ตี้ แต่ละตี้ 6 ช่อง (ช่องว่างแสดง '— ว่าง —') ตี้ที่ไม่มีพระหัวเป็นสีแดง"""
    fig, axes = plt.subplots(
        len(PARTY_GROUPS), PARTIES_PER_GROUP,
        figsize=(4.2 * PARTIES_PER_GROUP, 3.1 * len(PARTY_GROUPS))
    )
    for gi, group in enumerate(PARTY_GROUPS):
        for pi in range(PARTIES_PER_GROUP):
            ax = axes[gi][pi]
            ax.axis("off")
            members = grouped.get((group, pi + 1), [])
            by_slot = {}
            extra = []
            for m in members:
                s = m.get("slot") or 0
                if 1 <= s <= PARTY_SIZE and s not in by_slot:
                    by_slot[s] = m
                else:
                    extra.append(m)
            for s in range(1, PARTY_SIZE + 1):  # คนที่ไม่มีเลขช่อง ใส่ช่องว่างที่เหลือ
                if s not in by_slot and extra:
                    by_slot[s] = extra.pop(0)

            has_priest = any(_is_priest(m["character_class"]) for m in members)
            label = _party_label(group, pi + 1) + ("" if has_priest or not members else "  (ไม่มีพระ!)")
            table_data = []
            for s in range(1, PARTY_SIZE + 1):
                m = by_slot.get(s)
                table_data.append([str(s), m["in_game_name"], m["character_class"]] if m else [str(s), "— ว่าง —", ""])

            table = ax.table(cellText=table_data, colLabels=["", label, ""], cellLoc="center",
                             loc="center", colWidths=[0.12, 0.5, 0.38])
            table.auto_set_font_size(False)
            table.set_fontsize(9)
            table.scale(1, 1.6)
            head = TEAM_HEADER_COLORS.get(group, (0.3, 0.3, 0.3))
            if members and not has_priest:
                head = (0.85, 0.1, 0.1)
            for col in range(3):
                table[0, col].set_facecolor(head)
                table[0, col].set_text_props(weight="bold", color="black" if group != "DARK" else "white")
            light_team = tuple(c * 0.45 + 0.55 for c in TEAM_HEADER_COLORS.get(group, (0.6, 0.6, 0.6)))
            for i in range(1, PARTY_SIZE + 1):
                m = by_slot.get(i)
                table[i, 0].set_facecolor(light_team)
                table[i, 1].set_facecolor(light_team)
                if m:
                    color = _party_class_color(guild, m["character_class"])
                    table[i, 2].set_facecolor(color)
                    lum = 0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]
                    table[i, 2].set_text_props(weight="bold", color="black" if lum > 0.6 else "white")
                else:
                    table[i, 1].set_text_props(color=(0.45, 0.45, 0.45))
                    table[i, 2].set_facecolor((0.9, 0.9, 0.9))

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


async def _build_leaveboard_embed(guild: discord.Guild) -> discord.Embed:
    pool = await db.get_pool()
    rows = await pool.fetch(
        "SELECT * FROM party_leave WHERE guild_id = $1 ORDER BY left_at ASC", guild.id
    )
    lines = []
    for r in rows:
        line = f"• <@{r['discord_user_id']}>"
        if r["orig_group"]:
            line += f" — {r['orig_class']} (ออกจาก {_party_label(r['orig_group'], r['orig_party'])})"
            if r["sub_user_id"]:
                line += f"\n   ↳ 🔁 ดึงพระ **{r['sub_name']}** จาก {_party_label(r['sub_group'], r['sub_party'])} มาแทน"
        else:
            line += " — (ไม่อยู่ในตาราง)"
        lines.append(line)
    description = "\n".join(lines) if lines else "*ยังไม่มีใครลา*"
    if len(description) > 4000:
        description = description[:3990] + "\n..."
    embed = discord.Embed(title=f"🟡 กระดานลา ({len(rows)} คน)", description=description, color=0xFEE75C)

    grouped = await _fetch_party_assignments_grouped(pool, guild.id)
    no_priest = _parties_without_priest(grouped)
    if no_priest:
        embed.add_field(name="⚠️ ตี้ที่ยังไม่มีพระ", value=", ".join(no_priest), inline=False)
    embed.set_footer(text="กด 'ลา' → ออกจากตี้ทันที | ถ้าเป็นพระ ระบบดึงพระจากตี้ที่มีพระ 2 คนมาแทน | 'ยกเลิกลา' → กลับช่องเดิม")
    return embed


async def refresh_party_boards(guild: discord.Guild):
    """อัปเดตทั้งกระดานตาราง (รูปภาพ) และกระดานลา (embed+ปุ่ม) ที่เคยตั้งไว้ในกิลด์นี้"""
    pool = await db.get_pool()

    roster_row = await pool.fetchrow("SELECT channel_id, message_id FROM party_rosterboard WHERE guild_id = $1", guild.id)
    if roster_row:
        channel = guild.get_channel(roster_row["channel_id"])
        if channel:
            try:
                message = await channel.fetch_message(roster_row["message_id"])
                grouped = await _fetch_party_assignments_grouped(pool, guild.id)
                if grouped:
                    img = await asyncio.to_thread(_render_party_board_image, guild, grouped)
                    await message.edit(content=None, attachments=[discord.File(img, filename="party_board.png")])
                else:
                    await message.edit(content="ยังไม่มีโพยปาร์ตี้ ใช้ `/party upload` แนบรูปตารางก่อน", attachments=[])
            except discord.NotFound:
                pass
            except Exception as e:
                log.error(f"อัปเดตกระดานตารางปาร์ตี้ไม่สำเร็จ: {e}")

    leave_row = await pool.fetchrow("SELECT channel_id, message_id FROM party_leaveboard WHERE guild_id = $1", guild.id)
    if leave_row:
        channel = guild.get_channel(leave_row["channel_id"])
        if channel:
            try:
                message = await channel.fetch_message(leave_row["message_id"])
                await message.edit(embed=await _build_leaveboard_embed(guild), view=PartyLeaveBoardView())
            except discord.NotFound:
                pass
            except Exception as e:
                log.error(f"อัปเดตกระดานลาไม่สำเร็จ: {e}")


async def _pull_priest_into(conn, guild_id: int, group: str, party_num: int, slot: int):
    """
    หาตี้ที่มีพระ >= 2 คน (ทีมเดียวกันก่อน) แล้วย้ายพระคนท้ายสุดของตี้นั้นมาช่อง (group, party_num, slot)
    คืน dict ข้อมูลพระที่ย้าย หรือ None ถ้าไม่มีตี้ไหนมีพระเกิน
    """
    rows = await conn.fetch(
        "SELECT discord_user_id, group_name, party_num, slot, in_game_name, character_class "
        "FROM party_assignments WHERE guild_id = $1",
        guild_id
    )
    priests_by_party = {}
    for r in rows:
        if _is_priest(r["character_class"]):
            priests_by_party.setdefault((r["group_name"], r["party_num"]), []).append(r)

    donors = [k for k, v in priests_by_party.items() if len(v) >= 2 and k != (group, party_num)]
    if not donors:
        return None
    order = {g: i for i, g in enumerate(PARTY_GROUPS)}
    donors.sort(key=lambda k: (k[0] != group, order.get(k[0], 99), k[1]))
    donor_key = donors[0]
    priest = max(priests_by_party[donor_key], key=lambda r: r["slot"] or 0)

    await conn.execute(
        "UPDATE party_assignments SET group_name = $3, party_num = $4, slot = $5 "
        "WHERE guild_id = $1 AND discord_user_id = $2",
        guild_id, priest["discord_user_id"], group, party_num, slot
    )
    return dict(priest)


async def _ensure_priest_each_party(conn, guild_id: int) -> list:
    """
    ตี้ไหนไม่มีพระ → สลับกับตี้ที่มีพระ 2 คนขึ้นไป (ทีมเดียวกันก่อน):
    พระคนท้ายของตี้ที่มีพระเกิน ⇄ คนท้ายสุดที่ไม่ใช่พระของตี้ที่ขาด  คืนรายการข้อความการสลับ
    """
    swaps = []
    order = {g: i for i, g in enumerate(PARTY_GROUPS)}
    while True:
        rows = await conn.fetch(
            "SELECT discord_user_id, group_name, party_num, slot, in_game_name, character_class "
            "FROM party_assignments WHERE guild_id = $1", guild_id
        )
        parties = {}
        for r in rows:
            parties.setdefault((r["group_name"], r["party_num"]), []).append(r)
        need = sorted([k for k, v in parties.items() if not any(_is_priest(m["character_class"]) for m in v)],
                      key=lambda k: (order.get(k[0], 99), k[1]))
        if not need:
            break
        target = need[0]
        donors = [k for k, v in parties.items() if sum(_is_priest(m["character_class"]) for m in v) >= 2]
        if not donors:
            break
        donors.sort(key=lambda k: (k[0] != target[0], order.get(k[0], 99), k[1]))
        donor = donors[0]
        priest = max((m for m in parties[donor] if _is_priest(m["character_class"])), key=lambda m: m["slot"] or 0)
        other = max(parties[target], key=lambda m: m["slot"] or 0)
        await conn.execute(
            "UPDATE party_assignments SET group_name = $3, party_num = $4, slot = $5 WHERE guild_id = $1 AND discord_user_id = $2",
            guild_id, priest["discord_user_id"], target[0], target[1], other["slot"]
        )
        await conn.execute(
            "UPDATE party_assignments SET group_name = $3, party_num = $4, slot = $5 WHERE guild_id = $1 AND discord_user_id = $2",
            guild_id, other["discord_user_id"], donor[0], donor[1], priest["slot"]
        )
        swaps.append(
            f"{priest['in_game_name']} (พระ) {_party_label(*donor)} → {_party_label(*target)}  ⇄  "
            f"{other['in_game_name']} ({other['character_class']}) {_party_label(*target)} → {_party_label(*donor)}"
        )
    return swaps


async def _apply_leave(conn, guild_id: int, user_id: int) -> dict:
    """
    เอาคนลาออกจากตี้ + บันทึกช่องเดิมไว้ใน party_leave (ต้องมีแถว party_leave อยู่แล้ว)
    ถ้าเป็นพระ ดึงพระจากตี้ที่มีพระ 2 คนมาแทน
    """
    me = await conn.fetchrow(
        "SELECT group_name, party_num, slot, character_class, in_game_name FROM party_assignments "
        "WHERE guild_id = $1 AND discord_user_id = $2",
        guild_id, user_id
    )
    if not me:
        return {"in_table": False}

    await conn.execute(
        "DELETE FROM party_assignments WHERE guild_id = $1 AND discord_user_id = $2", guild_id, user_id
    )
    sub = None
    if _is_priest(me["character_class"]):
        sub = await _pull_priest_into(conn, guild_id, me["group_name"], me["party_num"], me["slot"])

    await conn.execute(
        """
        UPDATE party_leave SET orig_group = $3, orig_party = $4, orig_slot = $5, orig_class = $6, orig_name = $7,
               sub_user_id = $8, sub_group = $9, sub_party = $10, sub_slot = $11, sub_name = $12
        WHERE guild_id = $1 AND discord_user_id = $2
        """,
        guild_id, user_id, me["group_name"], me["party_num"], me["slot"], me["character_class"], me["in_game_name"],
        sub["discord_user_id"] if sub else None,
        sub["group_name"] if sub else None,
        sub["party_num"] if sub else None,
        sub["slot"] if sub else None,
        sub["in_game_name"] if sub else None,
    )
    return {"in_table": True, "me": dict(me), "is_priest": _is_priest(me["character_class"]), "sub": sub}


class PartyLeaveBoardView(discord.ui.View):
    """กระดานลา — ปุ่ม 'ลา' และ 'ยกเลิกลา'"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="ลา", style=discord.ButtonStyle.red, custom_id="party_leave_btn")
    async def leave_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        pool = await db.get_pool()
        guild_id, uid = interaction.guild.id, interaction.user.id
        already = await pool.fetchval(
            "SELECT 1 FROM party_leave WHERE guild_id = $1 AND discord_user_id = $2", guild_id, uid
        )
        if already:
            return await interaction.response.send_message("คุณแจ้งลาไว้อยู่แล้ว", ephemeral=True)
        in_table = await pool.fetchval(
            "SELECT 1 FROM party_assignments WHERE guild_id = $1 AND discord_user_id = $2", guild_id, uid
        )
        if not in_table:
            view = await PartyClaimNameView.build(interaction)
            if view:
                return await interaction.response.send_message(
                    "บอทยังไม่รู้ว่าคุณคือชื่อไหนในตาราง (อาจยังไม่ได้แนะนำตัว) — เลือกชื่อของคุณด้านล่าง "
                    "(บอทจะจำไว้ใช้รอบหน้า):", view=view, ephemeral=True
                )
        await _do_leave(interaction)

    @discord.ui.button(label="ยกเลิกลา", style=discord.ButtonStyle.secondary, custom_id="party_cancel_leave_btn")
    async def cancel_leave_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _do_cancel_leave(interaction)


async def _do_leave(interaction: discord.Interaction, edit: bool = False):
    """บันทึกลา + เอาออกจากตี้ (+ ดึงพระแทนถ้าเป็นพระ) แล้วอัปเดตกระดาน"""
    pool = await db.get_pool()
    guild_id, uid = interaction.guild.id, interaction.user.id
    if True:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO party_leave (guild_id, discord_user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    guild_id, uid
                )
                res = await _apply_leave(conn, guild_id, uid)

        if not res["in_table"]:
            msg = "📋 บันทึกการลาแล้ว (คุณไม่ได้อยู่ในตารางปาร์ตี้)"
        else:
            me = res["me"]
            msg = f"📋 บันทึกการลาแล้ว — ออกจาก {_party_label(me['group_name'], me['party_num'])} ({me['character_class']})"
            if res["is_priest"]:
                sub = res["sub"]
                if sub:
                    msg += f"\n🔁 ดึงพระ **{sub['in_game_name']}** จาก {_party_label(sub['group_name'], sub['party_num'])} มาแทนแล้ว"
                else:
                    msg += "\n⚠️ ไม่มีตี้ไหนมีพระ 2 คน — ตี้นี้จะไม่มีพระ แจ้งแอดมินจัดการ"
        if edit:
            await interaction.response.edit_message(content=msg, view=None)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        await refresh_party_boards(interaction.guild)


class PartyClaimNameView(discord.ui.View):
    """ให้คนที่ยังไม่ถูกผูกกับตาราง เลือกชื่อตัวเองจากช่องที่ยังไม่มีเจ้าของ แล้วลาได้เลย"""

    def __init__(self, options: list):
        super().__init__(timeout=180)
        self.sel = discord.ui.Select(placeholder="เลือกชื่อของคุณในตาราง", options=options)
        self.sel.callback = self.on_pick
        self.add_item(self.sel)

    @classmethod
    async def build(cls, interaction: discord.Interaction):
        pool = await db.get_pool()
        rows = await pool.fetch(
            "SELECT discord_user_id, group_name, party_num, in_game_name, character_class FROM party_assignments "
            "WHERE guild_id = $1 AND discord_user_id < 0", interaction.guild.id
        )
        if not rows:
            return None
        me = interaction.user.display_name.lower()
        rows = sorted(rows, key=lambda r: -difflib.SequenceMatcher(None, me, r["in_game_name"].lower()).ratio())[:25]
        options = [
            discord.SelectOption(
                label=r["in_game_name"][:100],
                description=f"{_party_label(r['group_name'], r['party_num'])} · {r['character_class']}"[:100],
                value=str(r["discord_user_id"])
            ) for r in rows
        ]
        return cls(options)

    @discord.ui.button(label="ฉันไม่อยู่ในตาราง", style=discord.ButtonStyle.secondary, row=1)
    async def not_in_table(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _do_leave(interaction, edit=True)

    async def on_pick(self, interaction: discord.Interaction):
        pool = await db.get_pool()
        fake_id, uid, gid = int(self.sel.values[0]), interaction.user.id, interaction.guild.id
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "UPDATE party_assignments SET discord_user_id = $3 WHERE guild_id = $1 AND discord_user_id = $2 "
                    "RETURNING in_game_name", gid, fake_id, uid
                )
                if row:
                    await conn.execute(
                        "INSERT INTO party_name_links (guild_id, discord_user_id, in_game_name) VALUES ($1, $2, $3) "
                        "ON CONFLICT (guild_id, discord_user_id) DO UPDATE SET in_game_name = $3",
                        gid, uid, row["in_game_name"]
                    )
        if not row:
            return await interaction.response.edit_message(content="ชื่อนี้ถูกคนอื่นเลือกไปแล้ว กดลาใหม่อีกครั้ง", view=None)
        await _do_leave(interaction, edit=True)


async def _do_cancel_leave(interaction: discord.Interaction):
    if True:
        pool = await db.get_pool()
        guild_id, uid = interaction.guild.id, interaction.user.id
        note = ""
        async with pool.acquire() as conn:
            async with conn.transaction():
                lv = await conn.fetchrow(
                    "DELETE FROM party_leave WHERE guild_id = $1 AND discord_user_id = $2 RETURNING *", guild_id, uid
                )
                if not lv:
                    return await interaction.response.send_message("คุณไม่ได้อยู่ในสถานะลา", ephemeral=True)

                if lv["orig_group"]:
                    g, p, s = lv["orig_group"], lv["orig_party"], lv["orig_slot"]
                    # ถ้าเคยดึงพระมาแทน และพระคนนั้นยังอยู่ช่องนี้ → ส่งกลับตี้เดิม
                    if lv["sub_user_id"]:
                        await conn.execute(
                            "UPDATE party_assignments SET group_name = $3, party_num = $4, slot = $5 "
                            "WHERE guild_id = $1 AND discord_user_id = $2 AND group_name = $6 AND party_num = $7",
                            guild_id, lv["sub_user_id"], lv["sub_group"], lv["sub_party"], lv["sub_slot"], g, p
                        )
                    count = await conn.fetchval(
                        "SELECT COUNT(*) FROM party_assignments WHERE guild_id = $1 AND group_name = $2 AND party_num = $3",
                        guild_id, g, p
                    )
                    if count < PARTY_SIZE:
                        await conn.execute(
                            """
                            INSERT INTO party_assignments (guild_id, discord_user_id, group_name, party_num, slot, character_class, in_game_name)
                            VALUES ($1, $2, $3, $4, $5, $6, $7)
                            ON CONFLICT (guild_id, discord_user_id) DO NOTHING
                            """,
                            guild_id, uid, g, p, s, lv["orig_class"], lv["orig_name"]
                        )
                        note = f"\n↩️ กลับเข้า {_party_label(g, p)} ช่องเดิมแล้ว"
                    else:
                        note = f"\n⚠️ {_party_label(g, p)} เต็มแล้ว ยังไม่ได้ใส่กลับ แจ้งแอดมินจัดการ"

        await interaction.response.send_message("✅ ยกเลิกการลาแล้ว" + note, ephemeral=True)
        await refresh_party_boards(interaction.guild)


party_group = app_commands.Group(name="party", description="ระบบจัดปาร์ตี้")


@party_group.command(name="generate", description="สร้างโพยปาร์ตี้อัตโนมัติจากรายชื่อผู้เล่น (ใช้เมื่อไม่มีรูปตาราง)")
@has_mod_perms()
async def party_generate(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    pool = await db.get_pool()

    missing_priest, unassigned = await _generate_party_assignments(pool, interaction.guild.id)
    msg = "✅ สร้างโพยปาร์ตี้อัตโนมัติเรียบร้อยแล้ว"
    if missing_priest:
        msg += f"\n⚠️ พระไม่พอ ขาดพระอีก {missing_priest} ตี้"
    if unassigned:
        msg += f"\n⚠️ มีคนเกินความจุ 16 ตี้ (96 คน) อยู่ {unassigned} คน ยังไม่ถูกจัดเข้าตี้"
    msg += "\nใช้ `/party rosterboard` เพื่อโพสต์/อัปเดตกระดานตาราง"
    await interaction.followup.send(msg, ephemeral=True)
    await refresh_party_boards(interaction.guild)


@party_group.command(name="upload", description="แนบรูปตารางปาร์ตี้ (SUN/MOON/DARK/FLASH) ให้บอทจัดโพยตามรูป")
@has_mod_perms()
@app_commands.describe(
    image="รูปตารางปาร์ตี้ (รูปที่ 1)",
    image2="รูปตารางปาร์ตี้ (รูปที่ 2 ถ้าแยกเป็น 2 รูป)"
)
async def party_upload(interaction: discord.Interaction, image: discord.Attachment,
                       image2: Optional[discord.Attachment] = None):
    await interaction.response.defer(ephemeral=True)
    pool = await db.get_pool()
    guild_id = interaction.guild.id

    attachments = [a for a in (image, image2) if a]
    for a in attachments:
        if not a.content_type or not a.content_type.startswith("image/"):
            return await interaction.followup.send(f"❌ ไฟล์ {a.filename} ไม่ใช่รูปภาพ", ephemeral=True)
    images = [await a.read() for a in attachments]

    try:
        rows = await asyncio.to_thread(party_ocr.read_party_images, images)
    except Exception as e:
        log.error(f"อ่านรูปตารางปาร์ตี้ไม่สำเร็จ: {e}")
        return await interaction.followup.send("❌ อ่านรูปไม่สำเร็จ ลองส่งรูปที่ชัดกว่านี้", ephemeral=True)
    if not rows:
        return await interaction.followup.send(
            "❌ ไม่พบตารางปาร์ตี้ในรูป (ต้องเป็นรูปตารางแบบ SUN01 / MOON01 ... ที่มีหัวคอลัมน์ ลำดับ/รายชื่อ/อาชีพ)",
            ephemeral=True
        )

    profiles = [dict(r) for r in await pool.fetch(
        "SELECT discord_user_id, in_game_name, character_class FROM player_profiles WHERE guild_id = $1", guild_id
    )]
    known = {p["discord_user_id"] for p in profiles}
    # คนที่ยังไม่แนะนำตัว: ใช้ชื่อที่เคยผูกไว้ตอนกดลา (party_name_links) + ชื่อเล่นในดิส (ต้องคล้ายมาก)
    for r in await pool.fetch("SELECT discord_user_id, in_game_name FROM party_name_links WHERE guild_id = $1", guild_id):
        profiles.append({"discord_user_id": r["discord_user_id"], "in_game_name": r["in_game_name"],
                         "character_class": None, "bonus": 0.01 if r["discord_user_id"] in known else 0.02})
    for m in interaction.guild.members:
        if not m.bot:
            profiles.append({"discord_user_id": m.id, "in_game_name": m.display_name,
                             "character_class": None, "min_score": 0.8})
    party_ocr.match_names(rows, profiles)

    to_insert, unmatched = [], []
    fake_id = 0
    for r in rows:
        p = r["profile"]
        cls = r["cls"] or (p and p["character_class"]) or "?"
        if p:
            uid = p["discord_user_id"]
            # ถ้าจับคู่จากชื่อเล่นในดิส ใช้ชื่อที่อ่านจากรูปแทน
            name = p["in_game_name"] if p.get("min_score") is None else (r["name_texts"][0] if r["name_texts"] else p["in_game_name"])
        else:
            fake_id -= 1  # ยังผูกกับคนในดิสไม่ได้ → id ชั่วคราว (ติดลบ) จนกว่าเจ้าตัวจะกดลาแล้วเลือกชื่อ
            uid = fake_id
            name = r["name_texts"][0] if r["name_texts"] else "?"
            unmatched.append(f"{_party_label(r['group'], r['party_num'])} ช่อง {r['slot']}: \"{name}\" ({cls})")
        to_insert.append((guild_id, uid, r["group"], r["party_num"], r["slot"], cls, name))

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM party_assignments WHERE guild_id = $1", guild_id)
            await conn.executemany(
                """
                INSERT INTO party_assignments (guild_id, discord_user_id, group_name, party_num, slot, character_class, in_game_name)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (guild_id, discord_user_id) DO NOTHING
                """,
                to_insert
            )
            # กติกา: ทุกตี้ต้องมีพระ → ตี้ที่ไม่มีพระ สลับกับตี้ที่มีพระเกิน
            swaps = await _ensure_priest_each_party(conn, guild_id)
            # คนที่กดลาไว้ก่อนแล้ว → เอาออกจากตี้ตามกติกาทันที
            leave_ids = [r["discord_user_id"] for r in await conn.fetch(
                "SELECT discord_user_id FROM party_leave WHERE guild_id = $1 ORDER BY left_at", guild_id
            )]
            for lid in leave_ids:
                await _apply_leave(conn, guild_id, lid)

    grouped = await _fetch_party_assignments_grouped(pool, guild_id)
    total = sum(len(v) for v in grouped.values())
    priests = sum(1 for v in grouped.values() for m in v if _is_priest(m["character_class"]))
    no_priest = _parties_without_priest(grouped)

    msg = f"✅ จัดโพยตามรูปแล้ว: {len(grouped)} ตี้ / {total} คน / พระ {priests} คน"
    if swaps:
        msg += "\n🔁 สลับให้ทุกตี้มีพระ:\n" + "\n".join(f"• {x}" for x in swaps)
    if leave_ids:
        msg += f"\n📋 เอาคนที่ลาไว้แล้วออก {len(leave_ids)} คน"
    if no_priest:
        msg += f"\n⚠️ ตี้ที่ไม่มีพระ: {', '.join(no_priest)}"
    if unmatched:
        msg += (f"\n⚠️ ยังผูกกับคนในดิสไม่ได้ {len(unmatched)} คน (ส่วนใหญ่คือคนที่ยังไม่แนะนำตัว) — "
                "ยังอยู่ในตารางตามรูป ตอนกดลาบอทจะให้เลือกชื่อตัวเองจากตาราง แล้วจำไว้ใช้รอบหน้า"
                "\nแนะนำใช้ `/remind-introduction` แท็กให้ไปแนะนำตัว")
    msg += "\nใช้ `/party rosterboard` และ `/party leaveboard` เพื่อโพสต์กระดาน (ถ้ายังไม่เคยโพสต์)"
    await interaction.followup.send(msg[:2000], ephemeral=True)
    await refresh_party_boards(interaction.guild)


@party_group.command(name="leaveboard", description="โพสต์กระดานลา (ปุ่มลา/ยกเลิกลา) ในห้องนี้")
@has_mod_perms()
async def party_leaveboard(interaction: discord.Interaction):
    embed = await _build_leaveboard_embed(interaction.guild)
    await interaction.response.send_message(embed=embed, view=PartyLeaveBoardView())
    msg = await interaction.original_response()

    pool = await db.get_pool()
    await pool.execute(
        """
        INSERT INTO party_leaveboard (guild_id, channel_id, message_id) VALUES ($1, $2, $3)
        ON CONFLICT (guild_id) DO UPDATE SET channel_id = $2, message_id = $3
        """,
        interaction.guild.id, interaction.channel.id, msg.id
    )


@party_group.command(name="rosterboard", description="โพสต์กระดานตารางปาร์ตี้ (รูปภาพ) ในห้องนี้")
@has_mod_perms()
async def party_rosterboard(interaction: discord.Interaction):
    await interaction.response.defer()
    pool = await db.get_pool()
    grouped = await _fetch_party_assignments_grouped(pool, interaction.guild.id)
    if grouped:
        img = await asyncio.to_thread(_render_party_board_image, interaction.guild, grouped)
        message = await interaction.channel.send(file=discord.File(img, filename="party_board.png"))
    else:
        message = await interaction.channel.send("ยังไม่มีโพยปาร์ตี้ ใช้ `/party upload` แนบรูปตารางก่อน")
    await interaction.followup.send("โพสต์กระดานตารางปาร์ตี้แล้ว", ephemeral=True)

    await pool.execute(
        """
        INSERT INTO party_rosterboard (guild_id, channel_id, message_id) VALUES ($1, $2, $3)
        ON CONFLICT (guild_id) DO UPDATE SET channel_id = $2, message_id = $3
        """,
        interaction.guild.id, interaction.channel.id, message.id
    )


@party_group.command(name="cancel", description="ยกเลิกระบบจัดปาร์ตี้ทั้งหมด (ล้างโพย + ปิดกระดาน)")
@has_mod_perms()
async def party_cancel(interaction: discord.Interaction):
    pool = await db.get_pool()
    await pool.execute("DELETE FROM party_assignments WHERE guild_id = $1", interaction.guild.id)
    await pool.execute("DELETE FROM party_leave WHERE guild_id = $1", interaction.guild.id)

    for table_name in ("party_leaveboard", "party_rosterboard"):
        row = await pool.fetchrow(f"SELECT channel_id, message_id FROM {table_name} WHERE guild_id = $1", interaction.guild.id)
        if row:
            channel = interaction.guild.get_channel(row["channel_id"])
            if channel:
                try:
                    old_msg = await channel.fetch_message(row["message_id"])
                    await old_msg.delete()
                except Exception:
                    pass
        await pool.execute(f"DELETE FROM {table_name} WHERE guild_id = $1", interaction.guild.id)

    await interaction.response.send_message("🗑️ ยกเลิกระบบจัดปาร์ตี้ทั้งหมดแล้ว (ล้างโพย + ปิดกระดาน)", ephemeral=True)

tree.add_command(party_group)


# Invite
@tree.command(name="invite", description="รับลิงก์เชิญบอทเข้าเซิร์ฟเวอร์ (พร้อม permission ครบ รวม Manage Roles)")
async def invite_cmd(interaction: discord.Interaction):
    perms = discord.Permissions(
        view_channel=True,
        send_messages=True,
        embed_links=True,
        attach_files=True,
        read_message_history=True,
        manage_roles=True,  # จำเป็นสำหรับติดตั้ง role อาชีพให้สมาชิกอัตโนมัติตอนแนะนำตัว/edit-profile
    )
    url = discord.utils.oauth_url(
        client_id=str(bot.user.id),
        permissions=perms,
        scopes=("bot", "applications.commands"),
    )
    embed = discord.Embed(title="เชิญบอทยามเข้าเซิร์ฟเวอร์", color=0x5865F2)
    embed.description = f"[คลิกที่นี่เพื่อเชิญบอท]({url})"
    embed.add_field(name="Permissions ที่ขอ", value=(
        "View Channel • Send Messages\n"
        "Embed Links • Attach Files • Read History\n"
        "Manage Roles (สำหรับติดตั้ง role อาชีพให้สมาชิก)"
    ), inline=False)
    embed.set_footer(text="ถ้าบอทอยู่ในเซิร์ฟเวอร์นี้แล้ว กดลิงก์นี้ซ้ำได้เลย — Discord จะอัปเดต permission ให้โดยไม่ต้องเตะบอทออก")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# Ping / Help
@tree.command(name="ping", description="ตรวจสอบสถานะบอท")
async def ping_cmd(interaction: discord.Interaction):
    latency_ms = round(bot.latency * 1000)
    embed = discord.Embed(title="🏓 Pong!", color=0x5865F2)
    embed.add_field(name="ความหน่วง", value=f"`{latency_ms} ms`", inline=True)
    embed.add_field(name="สถานะ", value="`ออนไลน์ ✅`", inline=True)
    await interaction.response.send_message(embed=embed)


@tree.command(name="help", description="ดูคำสั่งทั้งหมด")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="Voice Tracker Bot — คำสั่งทั้งหมด", color=0x5865F2)
    embed.add_field(name="/track add", value="เริ่มติดตามห้องเสียง (ต้องมีสิทธิ์ Manage Channels)", inline=False)
    embed.add_field(name="/track remove", value="เลิกติดตามห้องเสียง", inline=False)
    embed.add_field(name="/track list", value="ดูรายการห้องที่ติดตามอยู่", inline=False)
    embed.add_field(name="/voice now", value="ดูว่าใครอยู่ในห้องตอนนี้ + เวลาที่อยู่มา", inline=False)
    embed.add_field(name="/voice stats", value="สรุปเวลารวมของแต่ละคนในห้อง (เลือกช่วงวันได้)", inline=False)
    embed.add_field(name="/voice export", value="ส่งออกข้อมูลเป็นไฟล์ CSV (เปิดด้วย Excel ได้)", inline=False)
    embed.add_field(name="/voice graph", value="ดูกราฟกิจกรรม (เวลารวมต่อวัน) แบบรูปภาพ", inline=False)
    embed.add_field(name="/setup-introduction", value="โพสต์กระดานแนะนำตัวผู้เล่นในห้องที่เลือก (แอดมิน)", inline=False)
    embed.add_field(name="/setup-jobs", value="ตั้งค่า Role อาชีพที่จะให้เลือกตอนแนะนำตัว (แอดมิน)", inline=False)
    embed.add_field(name="/my-profile", value="ดูข้อมูลแนะนำตัวของตัวเอง", inline=False)
    embed.add_field(name="/edit-profile", value="แก้ไขข้อมูลแนะนำตัวของตัวเอง", inline=False)
    embed.add_field(name="/player-search", value="ค้นหาผู้เล่นจากชื่อในเกมหรือชื่อในดิส (แอดมิน)", inline=False)
    embed.add_field(name="/player-list", value="ดูตารางรายชื่อผู้เล่นที่แนะนำตัวไว้ทั้งหมด (แอดมิน)", inline=False)
    embed.add_field(name="/player-remove", value="ลบข้อมูลแนะนำตัวของสมาชิก (แอดมิน)", inline=False)
    embed.add_field(name="/remind-introduction-dm", value="ส่ง DM เด้งแจ้งเตือนหาคนที่ยังไม่แนะนำตัว (แอดมิน)", inline=False)
    embed.add_field(name="/restore-introductions", value="กู้ข้อมูลแนะนำตัวคืนจาก Embed เก่า (แอดมิน)", inline=False)
    embed.add_field(name="/remind-introduction", value="แท็กแจ้งเตือนสมาชิกที่ยังไม่ได้แนะนำตัว (แอดมิน)", inline=False)
    embed.add_field(name="/setup-playerboard", value="ตั้งกระดานรายชื่อสมาชิกแบบรูปภาพ อัปเดตอัตโนมัติ (แอดมิน)", inline=False)
    embed.add_field(name="/party generate", value="สร้างโพยอัตโนมัติจากรายชื่อผู้เล่น (ใช้เมื่อไม่มีรูปตาราง) (แอดมิน)", inline=False)
    embed.add_field(name="/party upload", value="แนบรูปตารางปาร์ตี้ให้บอทอ่านด้วย OCR แล้วจัดโพยตามรูป (แอดมิน)", inline=False)
    embed.add_field(name="/party leaveboard", value="โพสต์กระดานลา (ปุ่มลา/ยกเลิกลา) (แอดมิน)", inline=False)
    embed.add_field(name="/party rosterboard", value="โพสต์กระดานตารางปาร์ตี้แบบรูปภาพ (แอดมิน)", inline=False)
    embed.add_field(name="/party cancel", value="ยกเลิกระบบจัดปาร์ตี้ทั้งหมด (แอดมิน)", inline=False)
    embed.add_field(name="/invite", value="รับลิงก์เชิญบอทพร้อม permission ครบ (รวม Manage Roles)", inline=False)
    embed.add_field(name="/ping", value="ตรวจสอบสถานะบอท", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def main():
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
