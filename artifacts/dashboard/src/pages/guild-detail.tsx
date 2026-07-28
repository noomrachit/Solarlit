import { useState, useRef, useEffect } from "react"
import { useRoute } from "wouter"
import { toast } from "sonner"
import { format } from "date-fns"
import { 
  useGetGuildSettings, 
  getGetGuildSettingsQueryKey,
  useUpdateGuildSettings,
  useListWarnings,
  getListWarningsQueryKey,
  useDeleteWarning,
  useGetWarningStats,
  getGetWarningStatsQueryKey,
  useGetQueue,
  getGetQueueQueryKey,
  useResetQueue,
  useListBannedWords,
  getListBannedWordsQueryKey,
  useAddBannedWord,
  useDeleteBannedWord,
  useListCustomCommands,
  getListCustomCommandsQueryKey,
  useAddCustomCommand,
  useDeleteCustomCommand
} from "@workspace/api-client-react"
import { useQueryClient } from "@tanstack/react-query"

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Switch } from "@/components/ui/switch"
import { Badge } from "@/components/ui/badge"
import { Server, Settings as SettingsIcon, AlertTriangle, ListMusic, ShieldBan, Terminal, Trash2, Plus, RefreshCw } from "lucide-react"

export default function GuildDetail() {
  const [match, params] = useRoute("/guilds/:guildId")
  const guildId = params?.guildId || ""
  
  const queryClient = useQueryClient()

  if (!match) return null

  return (
    <div className="space-y-6 animate-in fade-in duration-500">
      <div className="flex items-center gap-3 border-b border-border/50 pb-6">
        <div className="w-12 h-12 bg-primary/10 border border-primary/30 flex items-center justify-center text-primary">
          <Server className="w-6 h-6" />
        </div>
        <div>
          <h1 className="text-2xl font-bold tracking-tight uppercase">Guild Deployment</h1>
          <p className="text-muted-foreground font-mono text-sm flex items-center gap-2">
            ID: <span className="text-foreground">{guildId}</span>
          </p>
        </div>
      </div>

      <Tabs defaultValue="settings" className="w-full">
        <TabsList className="bg-transparent border-b border-border w-full justify-start h-auto p-0 rounded-none overflow-x-auto flex-nowrap">
          <TabsTrigger value="settings" className="rounded-none border-b-2 border-transparent data-[state=active]:border-primary data-[state=active]:bg-transparent px-6 py-3 font-mono uppercase tracking-wider text-xs">
            <SettingsIcon className="w-3.5 h-3.5 mr-2" /> Settings
          </TabsTrigger>
          <TabsTrigger value="warnings" className="rounded-none border-b-2 border-transparent data-[state=active]:border-primary data-[state=active]:bg-transparent px-6 py-3 font-mono uppercase tracking-wider text-xs">
            <AlertTriangle className="w-3.5 h-3.5 mr-2" /> Warnings
          </TabsTrigger>
          <TabsTrigger value="queue" className="rounded-none border-b-2 border-transparent data-[state=active]:border-primary data-[state=active]:bg-transparent px-6 py-3 font-mono uppercase tracking-wider text-xs">
            <ListMusic className="w-3.5 h-3.5 mr-2" /> Queue
          </TabsTrigger>
          <TabsTrigger value="banned_words" className="rounded-none border-b-2 border-transparent data-[state=active]:border-primary data-[state=active]:bg-transparent px-6 py-3 font-mono uppercase tracking-wider text-xs">
            <ShieldBan className="w-3.5 h-3.5 mr-2" /> Automod Words
          </TabsTrigger>
          <TabsTrigger value="commands" className="rounded-none border-b-2 border-transparent data-[state=active]:border-primary data-[state=active]:bg-transparent px-6 py-3 font-mono uppercase tracking-wider text-xs">
            <Terminal className="w-3.5 h-3.5 mr-2" /> Commands
          </TabsTrigger>
        </TabsList>

        <div className="mt-6">
          <TabsContent value="settings"><SettingsTab guildId={guildId} /></TabsContent>
          <TabsContent value="warnings"><WarningsTab guildId={guildId} /></TabsContent>
          <TabsContent value="queue"><QueueTab guildId={guildId} /></TabsContent>
          <TabsContent value="banned_words"><BannedWordsTab guildId={guildId} /></TabsContent>
          <TabsContent value="commands"><CustomCommandsTab guildId={guildId} /></TabsContent>
        </div>
      </Tabs>
    </div>
  )
}

function SettingsTab({ guildId }: { guildId: string }) {
  const { data: settings, isLoading } = useGetGuildSettings(guildId, { query: { queryKey: getGetGuildSettingsQueryKey(guildId) } })
  const updateSettings = useUpdateGuildSettings()
  const queryClient = useQueryClient()

  const [localSettings, setLocalSettings] = useState<any>(null)

  useEffect(() => {
    if (settings) setLocalSettings(settings)
  }, [settings])

  if (isLoading || !localSettings) return <div className="text-muted-foreground font-mono animate-pulse">Fetching configuration...</div>

  const handleSave = () => {
    updateSettings.mutate(
      { guildId, data: localSettings },
      {
        onSuccess: () => {
          toast.success("Configuration synchronized.")
          queryClient.invalidateQueries({ queryKey: getGetGuildSettingsQueryKey(guildId) })
        },
        onError: () => toast.error("Failed to synchronize configuration.")
      }
    )
  }

  return (
    <div className="grid md:grid-cols-2 gap-8 max-w-5xl">
      <div className="space-y-6">
        <Card className="bg-card/30 border-border/50">
          <CardHeader>
            <CardTitle className="uppercase tracking-wider text-sm font-mono text-primary">Core Settings</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5">
              <label className="text-xs font-mono text-muted-foreground uppercase">Command Prefix</label>
              <Input 
                value={localSettings.prefix || ""} 
                onChange={(e) => setLocalSettings({ ...localSettings, prefix: e.target.value })} 
                className="font-mono w-24"
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-mono text-muted-foreground uppercase">Log Channel ID</label>
              <Input 
                value={localSettings.logChannel || ""} 
                onChange={(e) => setLocalSettings({ ...localSettings, logChannel: e.target.value })} 
                className="font-mono"
                placeholder="000000000000000000"
              />
            </div>
          </CardContent>
        </Card>

        <Card className="bg-card/30 border-border/50">
          <CardHeader>
            <CardTitle className="uppercase tracking-wider text-sm font-mono text-primary">Welcome/Leave System</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5">
              <label className="text-xs font-mono text-muted-foreground uppercase">System Channel ID</label>
              <Input 
                value={localSettings.welcomeChannel || ""} 
                onChange={(e) => setLocalSettings({ ...localSettings, welcomeChannel: e.target.value })} 
                className="font-mono"
                placeholder="000000000000000000"
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-mono text-muted-foreground uppercase">Welcome Message</label>
              <Input 
                value={localSettings.welcomeMessage || ""} 
                onChange={(e) => setLocalSettings({ ...localSettings, welcomeMessage: e.target.value })} 
                className="font-mono"
                placeholder="Welcome to the server, {user}!"
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-mono text-muted-foreground uppercase">Leave Message</label>
              <Input 
                value={localSettings.leaveMessage || ""} 
                onChange={(e) => setLocalSettings({ ...localSettings, leaveMessage: e.target.value })} 
                className="font-mono"
                placeholder="{user} has left the server."
              />
            </div>
          </CardContent>
        </Card>
      </div>

      <div className="space-y-6">
        <Card className="bg-card/30 border-border/50">
          <CardHeader>
            <CardTitle className="uppercase tracking-wider text-sm font-mono text-primary">Automod Engine</CardTitle>
          </CardHeader>
          <CardContent className="space-y-6">
            <div className="flex items-center justify-between">
              <div>
                <div className="font-mono text-sm uppercase">Master Switch</div>
                <div className="text-xs text-muted-foreground">Enable or disable all automod features</div>
              </div>
              <Switch 
                checked={localSettings.automodEnabled} 
                onCheckedChange={(c) => setLocalSettings({ ...localSettings, automodEnabled: c })} 
              />
            </div>
            
            <div className={`space-y-4 pt-4 border-t border-border/50 ${!localSettings.automodEnabled ? 'opacity-50 pointer-events-none' : ''}`}>
              <div className="flex items-center justify-between">
                <div className="font-mono text-sm">Anti-Invite</div>
                <Switch 
                  checked={localSettings.antiInvite} 
                  onCheckedChange={(c) => setLocalSettings({ ...localSettings, antiInvite: c })} 
                />
              </div>
              <div className="flex items-center justify-between">
                <div className="font-mono text-sm">Anti-Mention Spam</div>
                <Switch 
                  checked={localSettings.antiMentionSpam} 
                  onCheckedChange={(c) => setLocalSettings({ ...localSettings, antiMentionSpam: c })} 
                />
              </div>
              <div className="space-y-1.5 pt-2">
                <label className="text-xs font-mono text-muted-foreground uppercase">Mention Limit (per message)</label>
                <Input 
                  type="number"
                  value={localSettings.mentionLimit || 0} 
                  onChange={(e) => setLocalSettings({ ...localSettings, mentionLimit: parseInt(e.target.value) || 0 })} 
                  className="font-mono w-24"
                />
              </div>
            </div>
          </CardContent>
        </Card>

        <Button 
          onClick={handleSave} 
          disabled={updateSettings.isPending}
          className="w-full font-mono uppercase tracking-widest h-12"
        >
          {updateSettings.isPending ? "Synchronizing..." : "Apply Configuration"}
        </Button>
      </div>
    </div>
  )
}

function WarningsTab({ guildId }: { guildId: string }) {
  const queryClient = useQueryClient()
  const { data: warnings, isLoading } = useListWarnings(guildId, { query: { queryKey: getListWarningsQueryKey(guildId) } })
  const { data: stats } = useGetWarningStats(guildId, { query: { queryKey: getGetWarningStatsQueryKey(guildId) } })
  const deleteWarning = useDeleteWarning()

  const handleDelete = (warningId: number) => {
    deleteWarning.mutate({ guildId, warningId }, {
      onSuccess: () => {
        toast.success("Warning expunged from records.")
        queryClient.invalidateQueries({ queryKey: getListWarningsQueryKey(guildId) })
        queryClient.invalidateQueries({ queryKey: getGetWarningStatsQueryKey(guildId) })
      },
      onError: () => toast.error("Failed to delete warning.")
    })
  }

  return (
    <div className="grid md:grid-cols-3 gap-8">
      <div className="md:col-span-2 space-y-4">
        <h2 className="text-sm font-mono uppercase tracking-wider text-primary">Warning Ledger</h2>
        <Card className="border-border/50 bg-card/30">
          <div className="overflow-x-auto">
            <table className="w-full data-table">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>User ID</th>
                  <th>Reason</th>
                  <th>Date</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {isLoading ? (
                  <tr><td colSpan={5} className="text-center py-8 text-muted-foreground animate-pulse">Accessing records...</td></tr>
                ) : warnings?.length === 0 ? (
                  <tr><td colSpan={5} className="text-center py-8 text-muted-foreground">No warnings logged for this deployment.</td></tr>
                ) : (
                  warnings?.map((w) => (
                    <tr key={w.id}>
                      <td className="text-muted-foreground">#{w.id}</td>
                      <td className="text-foreground">{w.userId}</td>
                      <td className="whitespace-normal min-w-[200px] font-sans">{w.reason || "No reason provided"}</td>
                      <td className="text-muted-foreground text-xs whitespace-nowrap">{format(new Date(w.createdAt), "MMM d, yyyy HH:mm")}</td>
                      <td>
                        <Button 
                          variant="ghost" 
                          size="icon" 
                          className="h-8 w-8 text-muted-foreground hover:text-destructive hover:bg-destructive/10"
                          onClick={() => handleDelete(w.id)}
                          disabled={deleteWarning.isPending}
                        >
                          <Trash2 className="w-4 h-4" />
                        </Button>
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
        <h2 className="text-sm font-mono uppercase tracking-wider text-primary">Top Offenders</h2>
        <Card className="border-border/50 bg-card/30">
          <div className="p-4 space-y-4">
            {!stats || stats.length === 0 ? (
              <div className="text-sm text-muted-foreground text-center py-4 font-mono">Clean record.</div>
            ) : (
              stats.slice(0, 5).map((stat, i) => (
                <div key={stat.userId} className="flex items-center justify-between border-b border-border/50 last:border-0 pb-3 last:pb-0">
                  <div className="font-mono text-sm">{stat.userId}</div>
                  <Badge variant="destructive" className="font-mono bg-destructive/10 text-destructive">{stat.count} strikes</Badge>
                </div>
              ))
            )}
          </div>
        </Card>
      </div>
    </div>
  )
}

function QueueTab({ guildId }: { guildId: string }) {
  const queryClient = useQueryClient()
  const { data: queue, isLoading } = useGetQueue(guildId, { query: { queryKey: getGetQueueQueryKey(guildId) } })
  const resetQueue = useResetQueue()

  const handleReset = () => {
    resetQueue.mutate({ guildId }, {
      onSuccess: () => {
        toast.success("Queue buffer flushed.")
        queryClient.invalidateQueries({ queryKey: getGetQueueQueryKey(guildId) })
      },
      onError: () => toast.error("Failed to flush queue.")
    })
  }

  return (
    <div className="max-w-4xl space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-mono uppercase tracking-wider text-primary">Audio Buffer</h2>
        <Button 
          variant="destructive" 
          size="sm" 
          onClick={handleReset}
          disabled={resetQueue.isPending || !queue?.length}
          className="font-mono uppercase text-xs tracking-wider"
        >
          <RefreshCw className={`w-3.5 h-3.5 mr-2 ${resetQueue.isPending ? 'animate-spin' : ''}`} />
          Flush Buffer
        </Button>
      </div>

      <Card className="border-border/50 bg-card/30">
        <div className="overflow-x-auto">
          <table className="w-full data-table">
            <thead>
              <tr>
                <th className="w-24">Pos</th>
                <th>User ID</th>
                <th>Time Added</th>
              </tr>
            </thead>
            <tbody>
              {isLoading ? (
                <tr><td colSpan={3} className="text-center py-8 text-muted-foreground animate-pulse">Reading buffer...</td></tr>
              ) : queue?.length === 0 ? (
                <tr><td colSpan={3} className="text-center py-8 text-muted-foreground">Buffer is empty.</td></tr>
              ) : (
                queue?.map((q) => (
                  <tr key={`${q.userId}-${q.position}`}>
                    <td>
                      <Badge variant={q.position === 1 ? "default" : "outline"} className="font-mono">
                        #{q.position}
                      </Badge>
                    </td>
                    <td className="text-foreground">{q.userId}</td>
                    <td className="text-muted-foreground text-xs">{format(new Date(q.joinedAt), "HH:mm:ss")}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  )
}

function BannedWordsTab({ guildId }: { guildId: string }) {
  const queryClient = useQueryClient()
  const { data: words, isLoading } = useListBannedWords(guildId, { query: { queryKey: getListBannedWordsQueryKey(guildId) } })
  const addWord = useAddBannedWord()
  const deleteWord = useDeleteBannedWord()
  const [newWord, setNewWord] = useState("")

  const handleAdd = (e: React.FormEvent) => {
    e.preventDefault()
    if (!newWord.trim()) return
    
    addWord.mutate({ guildId, data: { word: newWord.trim().toLowerCase() } }, {
      onSuccess: () => {
        setNewWord("")
        toast.success("Pattern added to filter.")
        queryClient.invalidateQueries({ queryKey: getListBannedWordsQueryKey(guildId) })
      },
      onError: () => toast.error("Failed to add pattern.")
    })
  }

  const handleDelete = (word: string) => {
    deleteWord.mutate({ guildId, word }, {
      onSuccess: () => {
        toast.success("Pattern removed from filter.")
        queryClient.invalidateQueries({ queryKey: getListBannedWordsQueryKey(guildId) })
      },
      onError: () => toast.error("Failed to remove pattern.")
    })
  }

  return (
    <div className="max-w-3xl space-y-6">
      <Card className="border-border/50 bg-card/30">
        <CardHeader>
          <CardTitle className="uppercase tracking-wider text-sm font-mono text-primary">Content Filter Patterns</CardTitle>
          <CardDescription className="font-mono text-xs">Words matching these patterns will be blocked by Automod.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <form onSubmit={handleAdd} className="flex gap-3">
            <Input 
              value={newWord}
              onChange={(e) => setNewWord(e.target.value)}
              placeholder="Enter word or pattern..."
              className="font-mono"
            />
            <Button type="submit" disabled={addWord.isPending || !newWord.trim()} className="font-mono uppercase text-xs tracking-wider">
              <Plus className="w-4 h-4 mr-2" /> Add Pattern
            </Button>
          </form>

          <div className="flex flex-wrap gap-2 pt-4 border-t border-border/50">
            {isLoading ? (
              <div className="text-muted-foreground font-mono text-sm animate-pulse">Loading patterns...</div>
            ) : words?.length === 0 ? (
              <div className="text-muted-foreground font-mono text-sm">No patterns configured.</div>
            ) : (
              words?.map((w) => (
                <div key={w.word} className="flex items-center gap-2 bg-destructive/10 border border-destructive/20 text-destructive px-3 py-1.5 rounded-sm font-mono text-sm group">
                  {w.word}
                  <button 
                    onClick={() => handleDelete(w.word)}
                    disabled={deleteWord.isPending}
                    className="opacity-50 hover:opacity-100 focus:outline-none transition-opacity"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
              ))
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}

function CustomCommandsTab({ guildId }: { guildId: string }) {
  const queryClient = useQueryClient()
  const { data: commands, isLoading } = useListCustomCommands(guildId, { query: { queryKey: getListCustomCommandsQueryKey(guildId) } })
  const addCmd = useAddCustomCommand()
  const deleteCmd = useDeleteCustomCommand()
  
  const [newCmd, setNewCmd] = useState({ name: "", response: "" })

  const handleAdd = (e: React.FormEvent) => {
    e.preventDefault()
    if (!newCmd.name.trim() || !newCmd.response.trim()) return
    
    addCmd.mutate({ guildId, data: { name: newCmd.name.trim().toLowerCase(), response: newCmd.response } }, {
      onSuccess: () => {
        setNewCmd({ name: "", response: "" })
        toast.success("Command sequence registered.")
        queryClient.invalidateQueries({ queryKey: getListCustomCommandsQueryKey(guildId) })
      },
      onError: () => toast.error("Failed to register command sequence.")
    })
  }

  const handleDelete = (name: string) => {
    deleteCmd.mutate({ guildId, commandName: name }, {
      onSuccess: () => {
        toast.success("Command sequence purged.")
        queryClient.invalidateQueries({ queryKey: getListCustomCommandsQueryKey(guildId) })
      },
      onError: () => toast.error("Failed to purge sequence.")
    })
  }

  return (
    <div className="grid md:grid-cols-3 gap-8">
      <div className="md:col-span-2 space-y-4">
        <h2 className="text-sm font-mono uppercase tracking-wider text-primary">Registered Sequences</h2>
        <Card className="border-border/50 bg-card/30">
          <div className="overflow-x-auto">
            <table className="w-full data-table">
              <thead>
                <tr>
                  <th className="w-1/4">Trigger</th>
                  <th>Payload</th>
                  <th className="w-16">Action</th>
                </tr>
              </thead>
              <tbody>
                {isLoading ? (
                  <tr><td colSpan={3} className="text-center py-8 text-muted-foreground animate-pulse">Loading sequences...</td></tr>
                ) : commands?.length === 0 ? (
                  <tr><td colSpan={3} className="text-center py-8 text-muted-foreground">No custom sequences registered.</td></tr>
                ) : (
                  commands?.map((cmd) => (
                    <tr key={cmd.name}>
                      <td className="text-primary font-bold">!{cmd.name}</td>
                      <td className="font-sans text-sm text-foreground/80">{cmd.response}</td>
                      <td>
                        <Button 
                          variant="ghost" 
                          size="icon" 
                          className="h-8 w-8 text-muted-foreground hover:text-destructive hover:bg-destructive/10"
                          onClick={() => handleDelete(cmd.name)}
                          disabled={deleteCmd.isPending}
                        >
                          <Trash2 className="w-4 h-4" />
                        </Button>
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
        <h2 className="text-sm font-mono uppercase tracking-wider text-primary">New Sequence</h2>
        <Card className="border-border/50 bg-card/30">
          <CardContent className="pt-6">
            <form onSubmit={handleAdd} className="space-y-4">
              <div className="space-y-1.5">
                <label className="text-xs font-mono text-muted-foreground uppercase">Trigger Phrase</label>
                <div className="relative">
                  <span className="absolute left-3 top-2.5 text-muted-foreground font-mono">!</span>
                  <Input 
                    value={newCmd.name}
                    onChange={(e) => setNewCmd({ ...newCmd, name: e.target.value.replace(/\s+/g, '') })}
                    className="font-mono pl-7"
                    placeholder="command"
                  />
                </div>
              </div>
              <div className="space-y-1.5">
                <label className="text-xs font-mono text-muted-foreground uppercase">Payload Response</label>
                <textarea 
                  value={newCmd.response}
                  onChange={(e) => setNewCmd({ ...newCmd, response: e.target.value })}
                  className="flex min-h-[100px] w-full border border-border bg-input px-3 py-2 text-sm font-sans placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary transition-colors resize-y"
                  placeholder="Enter the bot's response..."
                />
              </div>
              <Button type="submit" className="w-full font-mono uppercase tracking-widest" disabled={addCmd.isPending || !newCmd.name || !newCmd.response}>
                <Plus className="w-4 h-4 mr-2" /> Register
              </Button>
            </form>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
