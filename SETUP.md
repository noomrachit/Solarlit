# Moonlit Bot — วิธีติดตั้งและรันในเครื่อง

## สิ่งที่ต้องมีก่อน
- Node.js 20+ และ pnpm (`npm install -g pnpm`)
- Python 3.11+
- PostgreSQL (หรือใช้ Neon / Supabase ก็ได้)

---

## 1. ตั้งค่า Environment

```bash
cp .env.example .env
# แก้ไขไฟล์ .env ใส่ค่า DISCORD_BOT_TOKEN, DATABASE_URL ฯลฯ
```

---

## 2. ติดตั้ง Node dependencies

```bash
pnpm install
```

---

## 3. ติดตั้ง Python dependencies (สำหรับบอท)

```bash
cd bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cd ..
```

---

## 4. รัน

### บอท
```bash
cd bot
.venv/bin/python moonlit_bot.py
```

### API Server
```bash
pnpm --filter @workspace/api-server run dev
```

### Dashboard (เปิด http://localhost:PORT)
```bash
pnpm --filter @workspace/dashboard run dev
```

### หน้า Commands
```bash
pnpm --filter @workspace/commands run dev
```

---

## โครงสร้างโปรเจกต์

```
bot/                        บอท Discord (Python)
  moonlit_bot.py            ไฟล์หลัก — slash commands ทั้งหมด
  database.py               asyncpg pool + init_db()
  requirements.txt

artifacts/
  api-server/src/           Express 5 API (TypeScript)
  dashboard/src/            React Dashboard (Vite)
  commands/src/             หน้า Command Reference (Vite)

lib/
  db/src/                   Drizzle schema
  api-spec/openapi.yaml     OpenAPI spec

.env.example                ตัวอย่าง environment variables
```
