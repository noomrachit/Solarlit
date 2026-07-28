import { Link, useLocation } from "wouter"
import { LayoutDashboard, Settings, Activity } from "lucide-react"
import { useEffect } from "react"

export function Layout({ children }: { children: React.ReactNode }) {
  const [location] = useLocation()

  // Ensure dark mode is active on mount
  useEffect(() => {
    document.documentElement.classList.add('dark');
  }, []);

  return (
    <div className="min-h-screen bg-background text-foreground flex flex-col md:flex-row">
      <aside className="w-full md:w-64 border-r border-border bg-card/50 flex flex-col backdrop-blur-sm sticky top-0 md:h-screen z-10">
        <div className="p-6 border-b border-border flex items-center gap-3">
          <div className="w-8 h-8 bg-primary/20 rounded flex items-center justify-center text-primary glow-primary border border-primary/50">
            <Activity className="w-5 h-5" />
          </div>
          <div>
            <h1 className="font-bold text-lg leading-tight tracking-tight uppercase">Moonlit<span className="text-primary">Bot</span></h1>
            <p className="text-[10px] text-muted-foreground font-mono uppercase tracking-widest">Mission Control</p>
          </div>
        </div>
        
        <nav className="p-4 flex-1 space-y-1">
          <Link href="/">
            <div className={`flex items-center gap-3 px-3 py-2 text-sm font-medium cursor-pointer transition-colors ${location === '/' ? 'bg-primary/10 text-primary border-l-2 border-primary' : 'text-muted-foreground hover:text-foreground hover:bg-secondary border-l-2 border-transparent'}`}>
              <LayoutDashboard className="w-4 h-4" />
              Overview
            </div>
          </Link>
          <div className="pt-4 pb-2 px-3 text-[10px] font-mono text-muted-foreground uppercase tracking-widest">
            System
          </div>
          <div className={`flex items-center gap-3 px-3 py-2 text-sm font-medium cursor-pointer transition-colors text-muted-foreground hover:text-foreground hover:bg-secondary border-l-2 border-transparent`}>
            <Settings className="w-4 h-4" />
            Global Settings
          </div>
        </nav>
        
        <div className="p-4 border-t border-border mt-auto">
          <div className="bg-background border border-border p-3 flex flex-col gap-2">
            <div className="flex justify-between items-center text-xs font-mono">
              <span className="text-muted-foreground">STATUS</span>
              <span className="text-green-500 flex items-center gap-1">
                <span className="w-1.5 h-1.5 rounded-full bg-green-500 animate-pulse"></span>
                ONLINE
              </span>
            </div>
            <div className="flex justify-between items-center text-xs font-mono">
              <span className="text-muted-foreground">VERSION</span>
              <span className="text-foreground">v2.4.1</span>
            </div>
            <div className="flex justify-between items-center text-xs font-mono">
              <span className="text-muted-foreground">LATENCY</span>
              <span className="text-primary">42ms</span>
            </div>
          </div>
        </div>
      </aside>
      
      <main className="flex-1 overflow-x-hidden relative">
        {/* Subtle grid pattern background */}
        <div className="absolute inset-0 pointer-events-none opacity-[0.03] z-0" 
             style={{ backgroundImage: 'linear-gradient(var(--border) 1px, transparent 1px), linear-gradient(90deg, var(--border) 1px, transparent 1px)', backgroundSize: '40px 40px' }}>
        </div>
        <div className="relative z-10 p-6 md:p-8">
          {children}
        </div>
      </main>
    </div>
  )
}
