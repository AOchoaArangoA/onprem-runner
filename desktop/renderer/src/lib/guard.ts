import { create } from 'zustand';
import { navigate, type Route } from './router';
import { useEditor } from '../store/editor';

interface GuardState {
  pending: Route | null;
  clear: () => void;
}

export const useGuard = create<GuardState>((set) => ({
  pending: null,
  clear: () => set({ pending: null }),
}));

/** Navigate, unless the editor holds unsaved changes: then ask first. */
export function go(route: Route): void {
  if (useEditor.getState().isDirty()) {
    useGuard.setState({ pending: route });
    return;
  }
  navigate(route);
}
