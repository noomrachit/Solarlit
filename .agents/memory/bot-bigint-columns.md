---
name: Bot BigInt columns
description: How the Moonlit Bot schema uses BIGINT for Discord IDs and how to handle them in Node.js
---

The Moonlit Bot tables (settings, warnings, queue, banned_words, custom_commands) store Discord IDs as BIGINT (guild_id, user_id, moderator_id, log_channel, welcome_channel).

**Rule:** When querying from Node.js (node-postgres/pg):
- Pass values as `BigInt(stringId)` — e.g. `BigInt(rawGuildId)`  
- Results come back as strings from pg automatically (BIGINT > Number.MAX_SAFE_INTEGER)
- Convert to string explicitly in response objects: `String(row.guild_id)`

**Why:** Discord snowflake IDs exceed JS's Number.MAX_SAFE_INTEGER. node-postgres returns BIGINT columns as JS strings by default to avoid precision loss.
