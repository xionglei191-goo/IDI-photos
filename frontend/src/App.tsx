import AuthGate, { useAuth } from './Auth';
import DashboardApp from './DashboardApp';
import ProjectWorkspace from './ProjectWorkspace';
import ReviewLink from './ReviewLink';

function Workspace() {
  const { person } = useAuth();
  const key = JSON.stringify([person.id, person.system_role, person.projects]);
  const reviewId = new URLSearchParams(window.location.search).get('review');
  if (reviewId) return <ReviewLink key={key + reviewId} reviewId={reviewId} />;
  return person.system_role === 'admin' || Object.values(person.projects).includes('manager')
    ? <DashboardApp key={key} /> : <ProjectWorkspace key={key} />;
}
export default function App() {
  return <AuthGate><Workspace /></AuthGate>;
}
