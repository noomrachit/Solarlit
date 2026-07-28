import { Route, Switch, Router as WouterRouter } from 'wouter';
import CommandsPage from './pages/Commands';
import NotFound from './pages/not-found';

function Router() {
  return (
    <Switch>
      <Route path="/" component={CommandsPage} />
      <Route component={NotFound} />
    </Switch>
  );
}

function App() {
  return (
    <WouterRouter base={import.meta.env.BASE_URL.replace(/\/$/, '')}>
      <Router />
    </WouterRouter>
  );
}

export default App;