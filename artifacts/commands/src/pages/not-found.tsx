import { Link } from "wouter";

export default function NotFound() {
  return (
    <div className="min-h-screen w-full flex items-center justify-center bg-background px-4">
      <div className="text-center max-w-md w-full p-8 bg-card rounded-3xl border border-border shadow-lg">
        <h1 className="text-5xl font-bold text-primary mb-4 font-mono">404</h1>
        <h2 className="text-xl font-bold text-foreground mb-2">ไม่พบหน้าที่ต้องการ</h2>
        <p className="text-muted-foreground mb-8">
          หน้าที่คุณกำลังค้นหาอาจถูกลบ ย้าย หรือไม่มีอยู่ตั้งแต่แรก
        </p>
        <Link href="/" className="inline-flex items-center justify-center h-12 px-6 rounded-xl bg-primary text-primary-foreground font-semibold hover:opacity-90 transition-opacity w-full">
          กลับสู่หน้าหลัก
        </Link>
      </div>
    </div>
  );
}