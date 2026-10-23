import AuthGate, { useAuth } from './Auth';
import DashboardApp from './DashboardApp';
import ProjectWorkspace from './ProjectWorkspace';

function Workspace() {
  const { person } = useAuth();
  const key = JSON.stringify([person.id, person.system_role, person.projects]);
  return person.system_role === 'admin' || Object.values(person.projects).includes('manager')
    ? <DashboardApp key={key} /> : <ProjectWorkspace key={key} />;
}
export default function App() {
  return <AuthGate><Workspace /></AuthGate>;
}
