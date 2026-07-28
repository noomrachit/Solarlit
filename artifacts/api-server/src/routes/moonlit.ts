import { Router, type IRouter } from "express";
import { pool } from "@workspace/db";
import {
  GetStatsResponse,
  ListGuildsResponse,
  GetGuildSettingsParams,
  GetGuildSettingsResponse,
  UpdateGuildSettingsParams,
  UpdateGuildSettingsBody,
  UpdateGuildSettingsResponse,
  ListWarningsParams,
  ListWarningsResponse,
  DeleteWarningParams,
  DeleteWarningResponse,
  GetWarningStatsParams,
  GetWarningStatsResponse,
  GetQueueParams,
  GetQueueResponse,
  ResetQueueParams,
  ResetQueueResponse,
  ListBannedWordsParams,
  ListBannedWordsResponse,
  AddBannedWordParams,
  AddBannedWordBody,
  AddBannedWordResponse,
  DeleteBannedWordParams,
  DeleteBannedWordResponse,
  ListCustomCommandsParams,
  ListCustomCommandsResponse,
  AddCustomCommandParams,
  AddCustomCommandBody,
  AddCustomCommandResponse,
  DeleteCustomCommandParams,
  DeleteCustomCommandResponse,
} from "@workspace/api-zod";

const router: IRouter = Router();

// ─── Stats ────────────────────────────────────────────────────────────────────

router.get("/stats", async (_req, res): Promise<void> => {
  const client = await pool.connect();
  try {
    const [guilds, warnings, bannedWords, customCmds, queueEntries] =
      await Promise.all([
        client.query("SELECT COUNT(DISTINCT guild_id) AS cnt FROM settings"),
        client.query("SELECT COUNT(*) AS cnt FROM warnings"),
        client.query("SELECT COUNT(*) AS cnt FROM banned_words"),
        client.query("SELECT COUNT(*) AS cnt FROM custom_commands"),
        client.query("SELECT COUNT(*) AS cnt FROM queue"),
      ]);

    const raw = {
      totalGuilds: Number(guilds.rows[0]?.cnt ?? 0),
      totalWarnings: Number(warnings.rows[0]?.cnt ?? 0),
      totalBannedWords: Number(bannedWords.rows[0]?.cnt ?? 0),
      totalCustomCommands: Number(customCmds.rows[0]?.cnt ?? 0),
      totalQueueEntries: Number(queueEntries.rows[0]?.cnt ?? 0),
    };

    res.json(GetStatsResponse.parse(raw));
  } finally {
    client.release();
  }
});

// ─── Guild list ───────────────────────────────────────────────────────────────

router.get("/guilds", async (_req, res): Promise<void> => {
  const client = await pool.connect();
  try {
    // Aggregate all guild IDs from the tables that have guild_id
    const result = await client.query(`
      SELECT
        g.guild_id::text AS "guildId",
        COALESCE(w.cnt, 0) AS "warningCount",
        COALESCE(q.cnt, 0) AS "queueCount",
        COALESCE(bw.cnt, 0) AS "bannedWordCount",
        COALESCE(cc.cnt, 0) AS "customCommandCount",
        COALESCE(s.automod_enabled, false) AS "automodEnabled"
      FROM (
        SELECT guild_id FROM settings
        UNION
        SELECT guild_id FROM warnings
        UNION
        SELECT guild_id FROM queue
        UNION
        SELECT guild_id FROM banned_words
        UNION
        SELECT guild_id FROM custom_commands
      ) g
      LEFT JOIN settings s ON s.guild_id = g.guild_id
      LEFT JOIN (SELECT guild_id, COUNT(*) AS cnt FROM warnings GROUP BY guild_id) w ON w.guild_id = g.guild_id
      LEFT JOIN (SELECT guild_id, COUNT(*) AS cnt FROM queue GROUP BY guild_id) q ON q.guild_id = g.guild_id
      LEFT JOIN (SELECT guild_id, COUNT(*) AS cnt FROM banned_words GROUP BY guild_id) bw ON bw.guild_id = g.guild_id
      LEFT JOIN (SELECT guild_id, COUNT(*) AS cnt FROM custom_commands GROUP BY guild_id) cc ON cc.guild_id = g.guild_id
      ORDER BY g.guild_id
    `);

    const rows = result.rows.map((r) => ({
      guildId: String(r.guildId),
      warningCount: Number(r.warningCount),
      queueCount: Number(r.queueCount),
      bannedWordCount: Number(r.bannedWordCount),
      customCommandCount: Number(r.customCommandCount),
      automodEnabled: Boolean(r.automodEnabled),
    }));

    res.json(ListGuildsResponse.parse(rows));
  } finally {
    client.release();
  }
});

// ─── Guild Settings ───────────────────────────────────────────────────────────

router.get(
  "/guilds/:guildId/settings",
  async (req, res): Promise<void> => {
    const rawId = Array.isArray(req.params.guildId)
      ? req.params.guildId[0]
      : req.params.guildId;

    const client = await pool.connect();
    try {
      const result = await client.query(
        "SELECT * FROM settings WHERE guild_id = $1",
        [BigInt(rawId)],
      );
      if (!result.rows[0]) {
        res.status(404).json({ error: "Guild settings not found" });
        return;
      }
      const row = result.rows[0];
      const data = {
        guildId: String(row.guild_id),
        logChannel: row.log_channel ? String(row.log_channel) : null,
        welcomeChannel: row.welcome_channel
          ? String(row.welcome_channel)
          : null,
        welcomeMessage: row.welcome_message ?? null,
        leaveMessage: row.leave_message ?? null,
        prefix: row.prefix ?? "!",
        automodEnabled: Boolean(row.automod_enabled),
        antiInvite: Boolean(row.anti_invite),
        antiMentionSpam: Boolean(row.anti_mention_spam),
        mentionLimit: Number(row.mention_limit ?? 5),
      };
      res.json(GetGuildSettingsResponse.parse(data));
    } finally {
      client.release();
    }
  },
);

router.put(
  "/guilds/:guildId/settings",
  async (req, res): Promise<void> => {
    const rawId = Array.isArray(req.params.guildId)
      ? req.params.guildId[0]
      : req.params.guildId;

    const parsed = UpdateGuildSettingsBody.safeParse(req.body);
    if (!parsed.success) {
      res.status(400).json({ error: parsed.error.message });
      return;
    }
    const b = parsed.data;

    const client = await pool.connect();
    try {
      await client.query(
        `INSERT INTO settings (
          guild_id, log_channel, welcome_channel, welcome_message, leave_message,
          prefix, automod_enabled, anti_invite, anti_mention_spam, mention_limit
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        ON CONFLICT (guild_id) DO UPDATE SET
          log_channel = COALESCE($2, settings.log_channel),
          welcome_channel = COALESCE($3, settings.welcome_channel),
          welcome_message = COALESCE($4, settings.welcome_message),
          leave_message = COALESCE($5, settings.leave_message),
          prefix = COALESCE($6, settings.prefix),
          automod_enabled = COALESCE($7, settings.automod_enabled),
          anti_invite = COALESCE($8, settings.anti_invite),
          anti_mention_spam = COALESCE($9, settings.anti_mention_spam),
          mention_limit = COALESCE($10, settings.mention_limit)`,
        [
          BigInt(rawId),
          b.logChannel ? BigInt(b.logChannel) : null,
          b.welcomeChannel ? BigInt(b.welcomeChannel) : null,
          b.welcomeMessage ?? null,
          b.leaveMessage ?? null,
          b.prefix ?? null,
          b.automodEnabled ?? null,
          b.antiInvite ?? null,
          b.antiMentionSpam ?? null,
          b.mentionLimit ?? null,
        ],
      );

      const result = await client.query(
        "SELECT * FROM settings WHERE guild_id = $1",
        [BigInt(rawId)],
      );
      const row = result.rows[0];
      const data = {
        guildId: String(row.guild_id),
        logChannel: row.log_channel ? String(row.log_channel) : null,
        welcomeChannel: row.welcome_channel
          ? String(row.welcome_channel)
          : null,
        welcomeMessage: row.welcome_message ?? null,
        leaveMessage: row.leave_message ?? null,
        prefix: row.prefix ?? "!",
        automodEnabled: Boolean(row.automod_enabled),
        antiInvite: Boolean(row.anti_invite),
        antiMentionSpam: Boolean(row.anti_mention_spam),
        mentionLimit: Number(row.mention_limit ?? 5),
      };
      res.json(UpdateGuildSettingsResponse.parse(data));
    } finally {
      client.release();
    }
  },
);

// ─── Warnings ─────────────────────────────────────────────────────────────────

router.get("/guilds/:guildId/warnings", async (req, res): Promise<void> => {
  const rawId = Array.isArray(req.params.guildId)
    ? req.params.guildId[0]
    : req.params.guildId;

  const client = await pool.connect();
  try {
    const result = await client.query(
      "SELECT id, guild_id, user_id, reason, moderator_id, created_at FROM warnings WHERE guild_id = $1 ORDER BY created_at DESC LIMIT 200",
      [BigInt(rawId)],
    );
    const rows = result.rows.map((r) => ({
      id: Number(r.id),
      guildId: String(r.guild_id),
      userId: String(r.user_id),
      reason: r.reason ?? null,
      moderatorId: r.moderator_id ? String(r.moderator_id) : null,
      createdAt: r.created_at?.toISOString() ?? new Date().toISOString(),
    }));
    res.json(ListWarningsResponse.parse(rows));
  } finally {
    client.release();
  }
});

router.delete(
  "/guilds/:guildId/warnings/:warningId",
  async (req, res): Promise<void> => {
    const rawGuildId = Array.isArray(req.params.guildId)
      ? req.params.guildId[0]
      : req.params.guildId;
    const rawWarningId = Array.isArray(req.params.warningId)
      ? req.params.warningId[0]
      : req.params.warningId;
    const warningId = parseInt(rawWarningId, 10);

    const client = await pool.connect();
    try {
      const result = await client.query(
        "DELETE FROM warnings WHERE id = $1 AND guild_id = $2 RETURNING id",
        [warningId, BigInt(rawGuildId)],
      );
      if (!result.rows[0]) {
        res.status(404).json({ error: "Warning not found" });
        return;
      }
      res.json(DeleteWarningResponse.parse({ success: true }));
    } finally {
      client.release();
    }
  },
);

router.get(
  "/guilds/:guildId/warnings/stats",
  async (req, res): Promise<void> => {
    const rawId = Array.isArray(req.params.guildId)
      ? req.params.guildId[0]
      : req.params.guildId;

    const client = await pool.connect();
    try {
      const result = await client.query(
        "SELECT user_id, COUNT(*) AS cnt FROM warnings WHERE guild_id = $1 GROUP BY user_id ORDER BY cnt DESC LIMIT 20",
        [BigInt(rawId)],
      );
      const rows = result.rows.map((r) => ({
        userId: String(r.user_id),
        count: Number(r.cnt),
      }));
      res.json(GetWarningStatsResponse.parse(rows));
    } finally {
      client.release();
    }
  },
);

// ─── Queue ────────────────────────────────────────────────────────────────────

router.get("/guilds/:guildId/queue", async (req, res): Promise<void> => {
  const rawId = Array.isArray(req.params.guildId)
    ? req.params.guildId[0]
    : req.params.guildId;

  const client = await pool.connect();
  try {
    const result = await client.query(
      "SELECT user_id, position, joined_at FROM queue WHERE guild_id = $1 ORDER BY position ASC",
      [BigInt(rawId)],
    );
    const rows = result.rows.map((r) => ({
      userId: String(r.user_id),
      position: Number(r.position),
      joinedAt: r.joined_at?.toISOString() ?? new Date().toISOString(),
    }));
    res.json(GetQueueResponse.parse(rows));
  } finally {
    client.release();
  }
});

router.delete("/guilds/:guildId/queue", async (req, res): Promise<void> => {
  const rawId = Array.isArray(req.params.guildId)
    ? req.params.guildId[0]
    : req.params.guildId;

  const client = await pool.connect();
  try {
    await client.query("DELETE FROM queue WHERE guild_id = $1", [
      BigInt(rawId),
    ]);
    res.json(ResetQueueResponse.parse({ success: true }));
  } finally {
    client.release();
  }
});

// ─── Banned Words ─────────────────────────────────────────────────────────────

router.get(
  "/guilds/:guildId/banned-words",
  async (req, res): Promise<void> => {
    const rawId = Array.isArray(req.params.guildId)
      ? req.params.guildId[0]
      : req.params.guildId;

    const client = await pool.connect();
    try {
      const result = await client.query(
        "SELECT word FROM banned_words WHERE guild_id = $1 ORDER BY word",
        [BigInt(rawId)],
      );
      const rows = result.rows.map((r) => ({ word: r.word as string }));
      res.json(ListBannedWordsResponse.parse(rows));
    } finally {
      client.release();
    }
  },
);

router.post(
  "/guilds/:guildId/banned-words",
  async (req, res): Promise<void> => {
    const rawId = Array.isArray(req.params.guildId)
      ? req.params.guildId[0]
      : req.params.guildId;

    const parsed = AddBannedWordBody.safeParse(req.body);
    if (!parsed.success) {
      res.status(400).json({ error: parsed.error.message });
      return;
    }

    const client = await pool.connect();
    try {
      await client.query(
        "INSERT INTO banned_words (guild_id, word) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        [BigInt(rawId), parsed.data.word.toLowerCase()],
      );
      res
        .status(201)
        .json(AddBannedWordResponse.parse({ word: parsed.data.word.toLowerCase() }));
    } finally {
      client.release();
    }
  },
);

router.delete(
  "/guilds/:guildId/banned-words/:word",
  async (req, res): Promise<void> => {
    const rawId = Array.isArray(req.params.guildId)
      ? req.params.guildId[0]
      : req.params.guildId;
    const word = Array.isArray(req.params.word)
      ? req.params.word[0]
      : req.params.word;

    const client = await pool.connect();
    try {
      await client.query(
        "DELETE FROM banned_words WHERE guild_id = $1 AND word = $2",
        [BigInt(rawId), word.toLowerCase()],
      );
      res.json(DeleteBannedWordResponse.parse({ success: true }));
    } finally {
      client.release();
    }
  },
);

// ─── Custom Commands ──────────────────────────────────────────────────────────

router.get(
  "/guilds/:guildId/custom-commands",
  async (req, res): Promise<void> => {
    const rawId = Array.isArray(req.params.guildId)
      ? req.params.guildId[0]
      : req.params.guildId;

    const client = await pool.connect();
    try {
      const result = await client.query(
        "SELECT name, response FROM custom_commands WHERE guild_id = $1 ORDER BY name",
        [BigInt(rawId)],
      );
      const rows = result.rows.map((r) => ({
        name: r.name as string,
        response: r.response as string,
      }));
      res.json(ListCustomCommandsResponse.parse(rows));
    } finally {
      client.release();
    }
  },
);

router.post(
  "/guilds/:guildId/custom-commands",
  async (req, res): Promise<void> => {
    const rawId = Array.isArray(req.params.guildId)
      ? req.params.guildId[0]
      : req.params.guildId;

    const parsed = AddCustomCommandBody.safeParse(req.body);
    if (!parsed.success) {
      res.status(400).json({ error: parsed.error.message });
      return;
    }

    const client = await pool.connect();
    try {
      await client.query(
        `INSERT INTO custom_commands (guild_id, name, response)
         VALUES ($1, $2, $3)
         ON CONFLICT (guild_id, name) DO UPDATE SET response = $3`,
        [BigInt(rawId), parsed.data.name.toLowerCase(), parsed.data.response],
      );
      res.status(201).json(
        AddCustomCommandResponse.parse({
          name: parsed.data.name.toLowerCase(),
          response: parsed.data.response,
        }),
      );
    } finally {
      client.release();
    }
  },
);

router.delete(
  "/guilds/:guildId/custom-commands/:commandName",
  async (req, res): Promise<void> => {
    const rawId = Array.isArray(req.params.guildId)
      ? req.params.guildId[0]
      : req.params.guildId;
    const commandName = Array.isArray(req.params.commandName)
      ? req.params.commandName[0]
      : req.params.commandName;

    const client = await pool.connect();
    try {
      await client.query(
        "DELETE FROM custom_commands WHERE guild_id = $1 AND name = $2",
        [BigInt(rawId), commandName.toLowerCase()],
      );
      res.json(DeleteCustomCommandResponse.parse({ success: true }));
    } finally {
      client.release();
    }
  },
);

export default router;
