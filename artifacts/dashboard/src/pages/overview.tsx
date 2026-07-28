import { useState } from "react"
import { useLocation } from "wouter"
import { useGetStats, getGetStatsQueryKey, useListGuilds, getListGuildsQueryKey } from "@workspace/api-client-react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Search, Server, AlertTriangle, ShieldBan, Terminal, ListMusic, ChevronRight, Activity } from "lucide-react"

export default function Overview() {
  const [_, setLocation] = useLocation()
  const [searchId, setSearchId] = useState("")
  
  const { data: stats, isLoading: statsLoading } = useGetStats({ query: { queryKey: getGetStatsQueryKey() } })
  const { data: guilds, isLoading: guildsLoading } = useListGuilds({ query: { queryKey: getListGuildsQueryKey() } })

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault()
    if (searchId.trim()) {
      setLocation(`/guilds/${searchId.trim()}`)
    }
  }

  const statCards = [
    { label: "Total Guilds", value: stats?.totalGuilds, icon: Server, color: "text-blue-400" },
    { label: "Active Warnings", value: stats?.totalWarnings, icon: AlertTriangle, color: "text-yellow-400" },
    { label: "Banned Words", value: stats?.totalBannedWords, icon: ShieldBan, color: "text-red-400" },
    { label: "Custom Commands", value: stats?.totalCustomCommands, icon: Terminal, color: "text-green-400" },
    { label: "Queue Entries", value: stats?.totalQueueEntries, icon: ListMusic, color: "text-purple-400" },
  ]

  return (
    <div className="space-y-8 animate-in fade-in duration-500">
      <header className="space-y-2">
        <h1 className="text-3xl font-bold tracking-tight uppercase">System Overview</h1>
        <p className="text-muted-foreground font-mono text-sm">Real-time telemetry across all connected Discord environments.</p>
      </header>

      {/* Stats Grid */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
        {statCards.map((stat, i) => (
          <Card key={i} className="bg-card/40 backdrop-blur border-border/50 hover:border-primary/30 transition-colors">
            <CardContent className="p-4 flex flex-col gap-2">
              <div className="flex justify-between items-start">
                <stat.icon className={`w-4 h-4 ${stat.color} opacity-80`} />
              </div>
              <div>
                <div className="text-2xl font-mono font-bold">
                  {statsLoading ? <span className="animate-pulse">--</span> : stat.value?.toLocaleString() || "0"}
                </div>
                <div className="text-[10px] text-muted-foreground font-mono uppercase tracking-wider mt-1">{stat.label}</div>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      <div className="grid md:grid-cols-3 gap-8">
        <div className="md:col-span-2 space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold flex items-center gap-2 uppercase tracking-wide">
              <Activity className="w-4 h-4 text-primary" />
              Guild Directory
            </h2>
            <div className="text-xs font-mono text-muted-foreground">
              {guilds?.length || 0} active deployments
            </div>
          </div>
          
          <Card className="border-border/50 overflow-hidden bg-card/30">
            <div className="overflow-x-auto">
              <table className="w-full data-table">
                <thead>
                  <tr>
                    <th>Guild ID</th>
                    <th>Warnings</th>
                    <th>Queue</th>
                    <th>Automod</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {guildsLoading ? (
                    <tr><td colSpan={5} className="text-center py-8 text-muted-foreground animate-pulse">Loading telemetry data...</td></tr>
                  ) : guilds?.length === 0 ? (
                    <tr><td colSpan={5} className="text-center py-8 text-muted-foreground">No active guilds found.</td></tr>
                  ) : (
                    guilds?.map((guild, idx) => (
                      <tr key={guild.guildId} className="group cursor-pointer" onClick={() => setLocation(`/guilds/${guild.guildId}`)} style={{ animationDelay: `${idx * 50}ms` }}>
                        <td className="font-mono text-primary/80 group-hover:text-primary transition-colors">{guild.guildId}</td>
                        <td>{guild.warningCount}</td>
                        <td>{guild.queueCount}</td>
                        <td>
                          {guild.automodEnabled ? (
                            <span className="inline-flex items-center gap-1 text-green-400 bg-green-400/10 px-2 py-0.5 rounded text-[10px]">
                              <span className="w-1.5 h-1.5 rounded-full bg-green-400"></span> ON
                            </span>
                          ) : (
                            <span className="inline-flex items-center gap-1 text-muted-foreground bg-muted px-2 py-0.5 rounded text-[10px]">
                              <span className="w-1.5 h-1.5 rounded-full bg-muted-foreground"></span> OFF
                            </span>
                          )}
                        </td>
                        <td>
                          <ChevronRight className="w-4 h-4 text-muted-foreground group-hover:text-primary transition-colors" />
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </Card>
        </div>

        <div className="space-y-4">
          <h2 className="text-lg font-semibold flex items-center gap-2 uppercase tracking-wide">
            <Search className="w-4 h-4 text-primary" />
            Direct Access
          </h2>
          <Card className="bg-card/40 border-border/50">
            <CardHeader className="pb-4">
              <CardTitle className="text-sm font-mono uppercase text-muted-foreground tracking-wider">Navigate to Deployment</CardTitle>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleSearch} className="space-y-4">
                <div className="space-y-2">
                  <Input 
                    placeholder="Enter Guild ID..." 
                    value={searchId}
                    onChange={(e) => setSearchId(e.target.value)}
                    className="font-mono bg-background/50 border-primary/20 focus-visible:border-primary/50"
                  />
                  <p className="text-[10px] text-muted-foreground font-mono">Accepts 18-19 digit Discord Snowflake.</p>
                </div>
                <Button type="submit" className="w-full font-mono uppercase tracking-widest" disabled={!searchId.trim()}>
                  Initialize Connection
                </Button>
              </form>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  )
}
