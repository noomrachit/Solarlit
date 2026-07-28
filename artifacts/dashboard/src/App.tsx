import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Toaster } from '@/components/ui/toaster';
import { Toaster as SonnerToaster } from '@/components/ui/sonner';
import { TooltipProvider } from '@/components/ui/tooltip';
import { Route, Switch, Router as WouterRouter } from 'wouter';

import { Layout } from '@/components/layout';
import Overview from '@/pages/overview';
import GuildDetail from '@/pages/guild-detail';
import Login from '@/pages/login';
import NotFound from '@/pages/not-found';
import { useAuth } from '@/hooks/use-auth';

const queryClient = new QueryClient();

function Router() {
  const { data: user, isPending } = useAuth();

  if (isPending) {
    return (
      <div className="min-h-screen bg-background flex items-center justify-center">
        <div className="flex flex-col items-center gap-4">
          <div className="w-8 h-8 border-2 border-primary border-t-transparent rounded-full animate-spin" />
          <p className="text-xs font-mono text-muted-foreground uppercase tracking-widest">
            กำลังโหลด...
          </p>
        </div>
      </div>
    );
  }

  if (!user) {
    return <Login />;
  }

  return (
    <Layout user={user}>
      <Switch>
        <Route path="/" component={Overview} />
        <Route path="/guilds/:guildId" component={GuildDetail} />
        <Route component={NotFound} />
      </Switch>
    </Layout>
  );
}

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <WouterRouter base={import.meta.env.BASE_URL.replace(/\/$/, '')}>
          <Router />
        </WouterRouter>
        <Toaster />
        <SonnerToaster />
      </TooltipProvider>
    </QueryClientProvider>
  );
}

export default App;
