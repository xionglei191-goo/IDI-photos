import AuthGate, { useAuth } from './Auth';
import DashboardApp from './DashboardApp';
import ProjectWorkspace from './ProjectWorkspace';

function Workspace() {
  const { person } = useAuth();
  return person.system_role === 'admin' ? <DashboardApp /> : <ProjectWorkspace key={JSON.stringify(person.projects)} />;
}
export default function App() {
  return <AuthGate><Workspace /></AuthGate>;
}
