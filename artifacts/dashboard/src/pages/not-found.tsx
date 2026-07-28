import { Link } from "wouter"
import { Button } from "@/components/ui/button"
import { Terminal } from "lucide-react"

export default function NotFound() {
  return (
    <div className="min-h-[80vh] flex flex-col items-center justify-center space-y-6 text-center animate-in fade-in zoom-in-95 duration-500">
      <div className="w-16 h-16 bg-destructive/10 text-destructive rounded-none border border-destructive/20 flex items-center justify-center mb-4">
        <Terminal className="w-8 h-8" />
      </div>
      <h1 className="text-6xl font-mono font-bold text-destructive tracking-tighter">404</h1>
      <div className="space-y-2">
        <h2 className="text-xl font-bold uppercase tracking-widest text-foreground">ไม่พบหน้านี้</h2>
        <p className="text-muted-foreground font-mono max-w-md mx-auto">
          หน้าที่คุณขอไม่มีอยู่หรือถูกจำกัดการเข้าถึง กรุณากลับไปยังหน้าหลัก
        </p>
      </div>
      <Link href="/">
        <Button className="font-mono uppercase tracking-widest mt-4">
          กลับหน้าหลัก
        </Button>
      </Link>
    </div>
  )
}
