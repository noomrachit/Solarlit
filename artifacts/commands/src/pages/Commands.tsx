import { useEffect, useState, useMemo } from 'react';
import { Search, Copy, Check, Command } from 'lucide-react';
import { cn } from '../lib/utils';

const CATEGORIES = [
  "ทั้งหมด",
  "ทั่วไป",
  "โมเดอเรชั่น",
  "ต้อนรับ",
  "ออโตม็อด",
  "คำสั่งกำหนดเอง",
  "รีแอคชั่นโรล",
  "คิว",
  "ห้องเสียง",
  "ห้องกระจายเสียง",
  "ตั้งค่า"
] as const;

type Category = typeof CATEGORIES[number];

const COMMANDS = [
  { name: "/ping", category: "ทั่วไป", desc: "ตรวจสอบสถานะและความหน่วงของบอท" },
  { name: "/help", category: "ทั่วไป", desc: "ดูคำสั่งทั้งหมด" },
  { name: "/mod kick", category: "โมเดอเรชั่น", desc: "เตะสมาชิกออกจากเซิร์ฟเวอร์" },
  { name: "/mod ban", category: "โมเดอเรชั่น", desc: "แบนสมาชิก" },
  { name: "/mod timeout", category: "โมเดอเรชั่น", desc: "พักโทษสมาชิกชั่วคราว" },
  { name: "/mod warn", category: "โมเดอเรชั่น", desc: "เตือนสมาชิก" },
  { name: "/mod warnings", category: "โมเดอเรชั่น", desc: "ดูประวัติคำเตือนของสมาชิก" },
  { name: "/mod clear", category: "โมเดอเรชั่น", desc: "ลบข้อความจำนวนมาก" },
  { name: "/welcome channel", category: "ต้อนรับ", desc: "ตั้งช่องสำหรับข้อความต้อนรับ" },
  { name: "/welcome message", category: "ต้อนรับ", desc: "ตั้งข้อความต้อนรับสมาชิกใหม่" },
  { name: "/welcome leave", category: "ต้อนรับ", desc: "ตั้งข้อความเมื่อสมาชิกออก" },
  { name: "/automod toggle", category: "ออโตม็อด", desc: "เปิด/ปิดระบบออโตม็อด" },
  { name: "/automod anti_invite", category: "ออโตม็อด", desc: "ป้องกันลิ้งค์เชิญจากเซิร์ฟเวอร์อื่น" },
  { name: "/automod anti_mention_spam", category: "ออโตม็อด", desc: "ป้องกันการ mention สแปม" },
  { name: "/automod addword", category: "ออโตม็อด", desc: "เพิ่มคำต้องห้าม" },
  { name: "/automod removeword", category: "ออโตม็อด", desc: "ลบคำต้องห้าม" },
  { name: "/automod listwords", category: "ออโตม็อด", desc: "ดูรายการคำต้องห้ามทั้งหมด" },
  { name: "/customcommand add", category: "คำสั่งกำหนดเอง", desc: "เพิ่มคำสั่งกำหนดเอง" },
  { name: "/customcommand remove", category: "คำสั่งกำหนดเอง", desc: "ลบคำสั่งกำหนดเอง" },
  { name: "/customcommand list", category: "คำสั่งกำหนดเอง", desc: "ดูคำสั่งกำหนดเองทั้งหมด" },
  { name: "/customcommand prefix", category: "คำสั่งกำหนดเอง", desc: "ตั้ง prefix สำหรับคำสั่ง" },
  { name: "/reactionrole add", category: "รีแอคชั่นโรล", desc: "เพิ่ม reaction role ให้ข้อความ" },
  { name: "/reactionrole remove", category: "รีแอคชั่นโรล", desc: "ลบ reaction role" },
  { name: "/queue join", category: "คิว", desc: "เข้าร่วมคิว" },
  { name: "/queue leave", category: "คิว", desc: "ออกจากคิว" },
  { name: "/queue list", category: "คิว", desc: "ดูรายชื่อในคิว" },
  { name: "/queue reset", category: "คิว", desc: "รีเซ็ตคิวทั้งหมด" },
  { name: "/queue panel", category: "คิว", desc: "โพสต์แผงควบคุมคิว" },
  { name: "/voice create", category: "ห้องเสียง", desc: "สร้าง Voice Channel ใหม่" },
  { name: "/voice delete", category: "ห้องเสียง", desc: "ลบ Voice Channel" },
  { name: "/voice limit", category: "ห้องเสียง", desc: "ตั้งจำนวนคนสูงสุดใน Voice Channel" },
  { name: "/voice rename", category: "ห้องเสียง", desc: "เปลี่ยนชื่อ Voice Channel" },
  { name: "/stage create", category: "ห้องกระจายเสียง", desc: "สร้าง Stage Channel (ห้องกระจายเสียง)" },
  { name: "/stage delete", category: "ห้องกระจายเสียง", desc: "ลบ Stage Channel" },
  { name: "/stage topic", category: "ห้องกระจายเสียง", desc: "ตั้งหัวข้อเวทีของ Stage Channel" },
  { name: "/stage rename", category: "ห้องกระจายเสียง", desc: "เปลี่ยนชื่อ Stage Channel" },
  { name: "/supportpanel panel", category: "ตั้งค่า", desc: "โพสต์แผงระบบช่วยเหลือ" },
  { name: "/settings logchannel", category: "ตั้งค่า", desc: "ตั้งช่อง log สำหรับบันทึกกิจกรรม" },
];

export default function CommandsPage() {
  const [searchQuery, setSearchQuery] = useState("");
  const [activeCategory, setActiveCategory] = useState<Category>("ทั้งหมด");
  const [copiedId, setCopiedId] = useState<string | null>(null);

  useEffect(() => {
    document.title = "SoLARLIT — คำสั่งทั้งหมด";
  }, []);

  const filteredCommands = useMemo(() => {
    return COMMANDS.filter(cmd => {
      const searchLower = searchQuery.toLowerCase();
      const matchesSearch = 
        cmd.name.toLowerCase().includes(searchLower) || 
        cmd.desc.toLowerCase().includes(searchLower) ||
        cmd.category.toLowerCase().includes(searchLower);
      
      const matchesCategory = activeCategory === "ทั้งหมด" || cmd.category === activeCategory;
      
      return matchesSearch && matchesCategory;
    });
  }, [searchQuery, activeCategory]);

  const handleCopy = (commandName: string) => {
    navigator.clipboard.writeText(commandName);
    setCopiedId(commandName);
    setTimeout(() => {
      setCopiedId(null);
    }, 2000);
  };

  const getTestId = (str: string) => str.replace(/[^a-zA-Z0-9]/g, '');

  return (
    <div className="min-h-screen bg-background pb-24 selection:bg-primary/20 selection:text-primary">
      <header className="sticky top-0 z-20 bg-background/95 backdrop-blur-xl border-b border-border/60 pt-8 sm:pt-12 pb-4 sm:pb-5 px-4 sm:px-6">
        <div className="max-w-3xl mx-auto space-y-6">
          
          <div className="flex items-center gap-4">
            <div className="h-12 w-12 rounded-2xl bg-primary flex items-center justify-center text-primary-foreground shadow-lg shadow-primary/25">
              <Command size={24} />
            </div>
            <div>
              <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-foreground">SoLARLIT</h1>
              <p className="text-sm sm:text-base text-muted-foreground mt-0.5 font-medium">คำสั่งทั้งหมด ({COMMANDS.length})</p>
            </div>
          </div>
          
          <div className="relative group">
            <Search className="absolute left-4 top-1/2 -translate-y-1/2 text-muted-foreground h-5 w-5 group-focus-within:text-primary transition-colors" />
            <input 
              type="search"
              placeholder="ค้นหาคำสั่ง..."
              className="w-full h-12 sm:h-14 pl-12 pr-4 rounded-xl sm:rounded-2xl bg-card border border-card-border shadow-sm focus:border-primary focus:ring-4 focus:ring-primary/10 outline-none transition-all text-base sm:text-lg text-foreground placeholder:text-muted-foreground font-medium"
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
              data-testid="input-search"
            />
          </div>

          <div className="flex overflow-x-auto pb-1 -mx-4 px-4 sm:mx-0 sm:px-0 hide-scrollbar gap-2">
            {CATEGORIES.map(cat => (
              <button
                key={cat}
                onClick={() => setActiveCategory(cat)}
                data-testid={`btn-category-${cat}`}
                className={cn(
                  "whitespace-nowrap px-4 py-2 sm:py-2.5 rounded-full text-sm font-semibold transition-all duration-200 border",
                  activeCategory === cat 
                    ? "bg-foreground text-background border-foreground shadow-md" 
                    : "bg-card text-muted-foreground border-card-border hover:bg-secondary hover:text-foreground"
                )}
              >
                {cat}
              </button>
            ))}
          </div>
        </div>
      </header>

      <main className="max-w-3xl mx-auto px-4 sm:px-6 pt-6 sm:pt-8 space-y-3 sm:space-y-4">
        {filteredCommands.length > 0 ? (
          filteredCommands.map(cmd => (
            <div 
              key={cmd.name}
              onClick={() => handleCopy(cmd.name)}
              data-testid={`card-command-${getTestId(cmd.name)}`}
              className="group flex flex-col sm:flex-row sm:items-center justify-between p-4 sm:p-5 rounded-2xl bg-card border border-card-border hover:border-primary/40 hover:shadow-md transition-all duration-200 cursor-pointer gap-4 relative overflow-hidden"
            >
              <div className="space-y-2 flex-1 relative z-10">
                <div className="flex items-center gap-3">
                  <span className="font-mono font-bold text-primary text-base sm:text-lg tracking-tight bg-primary/10 px-2 py-0.5 rounded-md">{cmd.name}</span>
                  <span className="px-2.5 py-0.5 rounded-full bg-secondary text-[11px] sm:text-xs font-semibold text-muted-foreground uppercase tracking-wider">{cmd.category}</span>
                </div>
                <p className="text-foreground/80 text-sm sm:text-base font-medium">{cmd.desc}</p>
              </div>
              
              <div className="flex-shrink-0 flex items-center justify-end relative z-10">
                <button 
                  onClick={(e) => {
                    e.stopPropagation();
                    handleCopy(cmd.name);
                  }}
                  data-testid={`btn-copy-${getTestId(cmd.name)}`}
                  className={cn(
                    "h-10 w-10 sm:h-12 sm:w-12 rounded-full flex items-center justify-center transition-all duration-300",
                    copiedId === cmd.name 
                      ? "bg-green-100 text-green-600 scale-110 shadow-sm" 
                      : "bg-secondary text-muted-foreground group-hover:bg-primary group-hover:text-primary-foreground group-hover:scale-105 group-hover:shadow-md"
                  )}
                  aria-label={`Copy ${cmd.name}`}
                >
                  {copiedId === cmd.name ? (
                    <Check size={20} className="sm:w-6 sm:h-6" />
                  ) : (
                    <Copy size={18} className="sm:w-5 sm:h-5" />
                  )}
                </button>
              </div>
            </div>
          ))
        ) : (
          <div className="text-center py-20 px-4">
            <div className="h-16 w-16 mx-auto rounded-full bg-secondary flex items-center justify-center text-muted-foreground mb-4">
              <Search size={24} />
            </div>
            <h3 className="text-lg font-bold text-foreground">ไม่พบคำสั่ง</h3>
            <p className="text-muted-foreground mt-1 text-sm sm:text-base">ลองค้นหาด้วยคำอื่นดูอีกครั้ง หรือเลือกหมวดหมู่ "ทั้งหมด"</p>
          </div>
        )}
      </main>
    </div>
  );
}