import { useAuth } from './Auth';

export default function usePermissions() {
  const { person } = useAuth();
  const isAdmin = person.system_role === 'admin';
  return { person, isAdmin, isManager: isAdmin || Object.values(person.projects).includes('manager'),
    canManage: (projectId?: string | null) => isAdmin || !!projectId && person.projects[projectId] === 'manager' };
}
