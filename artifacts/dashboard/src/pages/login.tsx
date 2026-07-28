import { Activity, Shield, Zap, Lock } from "lucide-react";
import { motion } from "framer-motion";

export default function Login() {
  return (
    <div className="min-h-screen bg-background text-foreground flex items-center justify-center relative overflow-hidden">
      {/* Grid background */}
      <div
        className="absolute inset-0 pointer-events-none opacity-[0.04]"
        style={{
          backgroundImage:
            "linear-gradient(var(--border) 1px, transparent 1px), linear-gradient(90deg, var(--border) 1px, transparent 1px)",
          backgroundSize: "40px 40px",
        }}
      />

      {/* Glow orb */}
      <div className="absolute top-1/4 left-1/2 -translate-x-1/2 w-[600px] h-[600px] rounded-full bg-primary/5 blur-[120px] pointer-events-none" />

      <motion.div
        initial={{ opacity: 0, y: 24 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5 }}
        className="relative z-10 w-full max-w-md mx-4"
      >
        {/* Header */}
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center w-16 h-16 rounded-full bg-primary/10 border border-primary/30 mb-4 glow-primary">
            <Activity className="w-8 h-8 text-primary" />
          </div>
          <h1 className="text-3xl font-bold tracking-tight uppercase">
            SoLAR<span className="text-primary">LIT</span>
          </h1>
          <p className="text-xs font-mono text-muted-foreground uppercase tracking-widest mt-1">
            ศูนย์ควบคุม
          </p>
        </div>

        {/* Card */}
        <div className="border border-border bg-card/60 backdrop-blur-sm p-8">
          <div className="flex items-center gap-2 mb-6">
            <Lock className="w-4 h-4 text-primary" />
            <h2 className="text-sm font-mono uppercase tracking-widest text-muted-foreground">
              กรุณาเข้าสู่ระบบ
            </h2>
          </div>

          <p className="text-sm text-muted-foreground mb-8 leading-relaxed">
            เข้าสู่ระบบด้วย Discord เพื่อใช้งานแผงควบคุม
            เฉพาะผู้ดูแลเซิร์ฟเวอร์เท่านั้นที่สามารถเข้าถึงได้
          </p>

          {/* Feature list */}
          <div className="space-y-3 mb-8">
            {[
              { icon: Shield, label: "ระบบ Moderation และประวัติคำเตือน" },
              { icon: Zap, label: "ตั้งค่า Automod และคำต้องห้าม" },
              { icon: Activity, label: "คิวเรียลไทม์และคำสั่งกำหนดเอง" },
            ].map(({ icon: Icon, label }) => (
              <div key={label} className="flex items-center gap-3 text-xs font-mono text-muted-foreground">
                <Icon className="w-3.5 h-3.5 text-primary/70 shrink-0" />
                <span>{label}</span>
              </div>
            ))}
          </div>

          <a
            href="/api/auth/login"
            className="flex items-center justify-center gap-3 w-full py-3 px-4 bg-primary hover:bg-primary/90 text-primary-foreground font-mono text-sm uppercase tracking-widest transition-colors"
          >
            <svg className="w-4 h-4" viewBox="0 0 24 24" fill="currentColor">
              <path d="M20.317 4.37a19.791 19.791 0 0 0-4.885-1.515.074.074 0 0 0-.079.037c-.21.375-.444.864-.608 1.25a18.27 18.27 0 0 0-5.487 0 12.64 12.64 0 0 0-.617-1.25.077.077 0 0 0-.079-.037A19.736 19.736 0 0 0 3.677 4.37a.07.07 0 0 0-.032.027C.533 9.046-.32 13.58.099 18.057c.01.107.058.213.12.28 2.052 1.507 4.043 2.422 6.007 3.025a.078.078 0 0 0 .084-.028c.462-.63.874-1.295 1.226-1.994a.076.076 0 0 0-.041-.106 13.107 13.107 0 0 1-1.872-.892.077.077 0 0 1-.008-.128 10.2 10.2 0 0 0 .372-.292.074.074 0 0 1 .077-.01c3.928 1.793 8.18 1.793 12.062 0a.074.074 0 0 1 .078.01c.12.098.246.198.373.292a.077.077 0 0 1-.006.127 12.299 12.299 0 0 1-1.873.892.077.077 0 0 0-.041.107c.36.698.772 1.362 1.225 1.993a.076.076 0 0 0 .084.028c1.97-.603 3.96-1.519 6.012-3.025a.077.077 0 0 0 .032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 0 0-.031-.03z" />
            </svg>
            เข้าสู่ระบบด้วย Discord
          </a>
        </div>

        <p className="text-center text-xs text-muted-foreground/50 font-mono mt-6">
          ปลอดภัย · เข้ารหัส · เฉพาะผู้ดูแล
        </p>
      </motion.div>
    </div>
  );
}
