# ─────────────────────────────────────────────
# Voice Channel + Stage Channel Commands
# สำหรับ Moonlit Bot
# ─────────────────────────────────────────────
# ต้องมี has_mod_perms(), send_log(), tree, app_commands, discord พร้อมอยู่แล้ว

# Voice Channels — ห้องเสียงปกติ (ทุกคนพูดได้)
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


# Stage Channels — ห้องกระจายเสียง / โทรโข่ง (ขึ้นเวที + คนฟัง)
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
            topic=topic,
            category=category,
            reason=f"สร้างโดย {interaction.user}"
        )
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
