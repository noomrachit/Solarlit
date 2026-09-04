import os
import asyncio
import logging
import csv
import io
from datetime import datetime, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from aiohttp import web

import matplotlib
matplotlib.use("Agg")  # ไม่ต้องใช้ GUI backend เพราะรันบนเซิร์ฟเวอร์
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

import database as db
# หมายเหตุ: import access as billing_access ถูกลบออก — ไม่มีไฟล์ access.py อยู่จริงใน repo นี้เลย
# (ไม่มีทั้งใน voicebot/, bot/, voicerelay/, website/) ทำให้ deploy พังด้วย ModuleNotFoundError
# ถ้าต้องการเช็คสิทธิ์สมาชิกก่อนใช้คำสั่ง ต้องสร้าง access.py จริงก่อน แล้วค่อยเปิดใช้ global_billing_check ใหม่

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("voice-tracker-bot")

BANGKOK_TZ = ZoneInfo("Asia/Bangkok")

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
            options.append(discord.SelectOption(label=role.name, value=role.name, default=(role.name == current)))
    return options[:25]  # Discord จำกัดตัวเลือกใน Select ไว้ที่ 25


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
    โพสต์ Embed แนะนำตัวลงห้องกระดานหลักที่ตั้งไว้ผ่าน /setup-introduction
    และห้องที่สอง (เช่น #ฐานข้อมูล-ผู้เล่น) ถ้าตั้งค่าไว้ด้วย — ถ้ายังไม่ตั้งค่าห้องไหนเลย ก็ข้ามไปเงียบๆ
    """
    pool = await db.get_pool()
    settings_row = await pool.fetchrow(
        "SELECT intro_channel, log_channel FROM intro_settings WHERE guild_id = $1", interaction.guild.id
    )
    if not settings_row:
        return

    row = await pool.fetchrow(
        "SELECT * FROM player_profiles WHERE guild_id = $1 AND discord_user_id = $2",
        interaction.guild.id, interaction.user.id
    )
    embed = _build_profile_embed(row, interaction.user)

    channel_ids = {settings_row["intro_channel"], settings_row["log_channel"]} - {None}
    for channel_id in channel_ids:
        channel = interaction.guild.get_channel(channel_id)
        if channel is None:
            continue
        try:
            await channel.send(embed=embed)
        except Exception as e:
            log.error(f"โพสต์ Embed แนะนำตัวไปห้อง {channel_id} ไม่สำเร็จ: {e}")


class IntroductionModal(discord.ui.Modal, title="แนะนำตัวผู้เล่น"):
    in_game_name = discord.ui.TextInput(label="ชื่อในเกม", placeholder="เช่น RachitTH", max_length=100)
    discord_name = discord.ui.TextInput(label="ชื่อในดิส", placeholder="เช่น Rachit", max_length=100)

    def __init__(self, character_class: str):
        super().__init__()
        self.character_class = character_class

    async def on_submit(self, interaction: discord.Interaction):
        pool = await db.get_pool()
        existing = await pool.fetchrow(
            "SELECT 1 FROM player_profiles WHERE guild_id = $1 AND discord_user_id = $2",
            interaction.guild.id, interaction.user.id
        )
        if existing:
            await interaction.response.send_message(
                "คุณเคยแนะนำตัวแล้ว กรุณาใช้คำสั่ง `/edit-profile` เพื่อแก้ไขข้อมูล", ephemeral=True
            )
            return

        await pool.execute(
            """
            INSERT INTO player_profiles (guild_id, discord_user_id, in_game_name, discord_name, character_class)
            VALUES ($1, $2, $3, $4, $5)
            """,
            interaction.guild.id, interaction.user.id,
            str(self.in_game_name), str(self.discord_name), self.character_class
        )
        await interaction.response.send_message("✅ แนะนำตัวสำเร็จแล้ว!", ephemeral=True)
        await _post_profile_embed(interaction)


class EditProfileModal(discord.ui.Modal, title="แก้ไขข้อมูลแนะนำตัว"):
    in_game_name = discord.ui.TextInput(label="ชื่อในเกม", max_length=100)
    discord_name = discord.ui.TextInput(label="ชื่อในดิส", max_length=100)

    def __init__(self, existing: dict, character_class: str):
        super().__init__()
        self.character_class = character_class
        self.in_game_name.default = existing["in_game_name"]
        self.discord_name.default = existing["discord_name"]

    async def on_submit(self, interaction: discord.Interaction):
        pool = await db.get_pool()
        await pool.execute(
            """
            UPDATE player_profiles
            SET in_game_name = $3, discord_name = $4, character_class = $5, updated_at = NOW()
            WHERE guild_id = $1 AND discord_user_id = $2
            """,
            interaction.guild.id, interaction.user.id,
            str(self.in_game_name), str(self.discord_name), self.character_class
        )
        await interaction.response.send_message("✅ แก้ไขข้อมูลเรียบร้อยแล้ว", ephemeral=True)


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
        character_class = self.select_item.values[0]
        if self.editing:
            modal = EditProfileModal(existing=self.existing, character_class=character_class)
        else:
            modal = IntroductionModal(character_class=character_class)
        await interaction.response.send_modal(modal)


class IntroductionBoardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📝 แนะนำตัว", style=discord.ButtonStyle.primary, custom_id="intro_board_open")
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        options = await _get_class_options(interaction.guild)
        if not options:
            await interaction.response.send_message(
                "ยังไม่ได้ตั้งค่า Role อาชีพ — แจ้งแอดมินให้รัน `/setup-jobs` ก่อน", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "① เลือกอาชีพที่เล่นก่อน แล้วจะเปิดฟอร์มให้กรอกชื่อในเกม/ชื่อในดิสต่อ",
            view=ClassSelectView(options), ephemeral=True
        )


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
    log_channel="ห้องที่สอง (เช่น #ฐานข้อมูล-ผู้เล่น) ที่จะได้รับสำเนา Embed แนะนำตัวทุกครั้งที่มีคนลงทะเบียนด้วย (ไม่บังคับ)"
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
        description="กดปุ่มด้านล่างเพื่อแนะนำตัวกับสมาชิกในกิลด์\n"
                     "กรุณากรอกชื่อในเกม ชื่อใน Discord และอาชีพที่เล่นให้ครบถ้วน",
        color=0xFEE75C
    )
    await channel.send(embed=embed, view=IntroductionBoardView())

    msg = f"ตั้งกระดานแนะนำตัวที่ {channel.mention} เรียบร้อยแล้ว"
    if log_channel:
        msg += f"\nจะส่งสำเนา Embed แนะนำตัวไปที่ {log_channel.mention} ทุกครั้งที่มีคนลงทะเบียนด้วย"
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


@tree.command(name="edit-profile", description="แก้ไขข้อมูลแนะนำตัวของตัวเอง")
async def edit_profile(interaction: discord.Interaction):
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
    options = await _get_class_options(interaction.guild, current=row["character_class"])
    if not options:
        await interaction.response.send_message(
            "ยังไม่ได้ตั้งค่า Role อาชีพ — แจ้งแอดมินให้รัน `/setup-jobs` ก่อน", ephemeral=True
        )
        return
    await interaction.response.send_message(
        "เลือกอาชีพที่เล่น (ค่าปัจจุบันถูกเลือกไว้แล้ว) แล้วจะเปิดฟอร์มให้แก้ชื่อในเกม/ชื่อในดิสต่อ",
        view=ClassSelectView(options, editing=True, existing=dict(row)), ephemeral=True
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
    embed.add_field(name="/ping", value="ตรวจสอบสถานะบอท", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def main():
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
