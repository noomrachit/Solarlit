-- Moonlit Bot schema
CREATE TABLE IF NOT EXISTS settings (
    guild_id BIGINT PRIMARY KEY,
    log_channel BIGINT,
    welcome_channel BIGINT,
    welcome_message TEXT DEFAULT 'ยินดีต้อนรับ {mention} เข้าสู่ {server}!',
    leave_message TEXT,
    prefix TEXT DEFAULT '!',
    automod_enabled BOOLEAN DEFAULT FALSE,
    anti_invite BOOLEAN DEFAULT FALSE,
    anti_mention_spam BOOLEAN DEFAULT FALSE,
    mention_limit INTEGER DEFAULT 5
);

CREATE TABLE IF NOT EXISTS warnings (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    reason TEXT,
    moderator_id BIGINT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS custom_commands (
    guild_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    response TEXT NOT NULL,
    PRIMARY KEY (guild_id, name)
);

CREATE TABLE IF NOT EXISTS reaction_roles (
    message_id BIGINT NOT NULL,
    emoji TEXT NOT NULL,
    role_id BIGINT NOT NULL,
    guild_id BIGINT,
    PRIMARY KEY (message_id, emoji)
);

CREATE TABLE IF NOT EXISTS presence_logs (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    status TEXT,
    last_seen TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS queue (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    position INTEGER,
    joined_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS banned_words (
    guild_id BIGINT NOT NULL,
    word TEXT NOT NULL,
    PRIMARY KEY (guild_id, word)
);
