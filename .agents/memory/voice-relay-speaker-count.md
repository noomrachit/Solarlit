---
name: Voice Relay speaker bot count
description: Confirmed live count of ลูกน้อง (speaker) bots for the Voice Relay Bot service, and the sequential-token gotcha that can silently disable some of them
---

Confirmed via Railway deploy logs (2026-09-05) that the Voice Relay Bot service (`voicerelay/relay_bot.py`) has **10 ลูกน้อง (speaker bots) live and working**, plus 1 หัวหน้า (listener bot) — `SPEAKER_BOT_TOKEN_1` through `SPEAKER_BOT_TOKEN_10` are all set on the `voice-relay` Railway service and all logged in successfully:

```
INFO | พบบอทพูดทั้งหมด 10 ตัว (รองรับห้องฟังพร้อมกันได้สูงสุด 10 ห้อง)
```

This supports up to 10 simultaneous target rooms.

**Gotcha:** the token-discovery loop in `relay_bot.py` reads `SPEAKER_BOT_TOKEN_1`, `_2`, `_3`, ... sequentially and **stops at the first empty/missing value**. Previously `SPEAKER_BOT_TOKEN_6` was set but empty, which silently prevented tokens 7-10 from ever being used even though they had valid values — the startup log only reported "5 ตัว" instead of 10, with no error. If speaker bots seem to be missing after adding a new token, check for a gap earlier in the sequence before assuming the new token itself is bad.

**10 is the current intentional count**, not leftover/stale config — don't reduce it or renumber the `SPEAKER_BOT_TOKEN_*` variables without the user asking first.
