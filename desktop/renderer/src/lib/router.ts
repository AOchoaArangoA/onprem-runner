import { useEffect, useState } from 'react';

export type Route =
  | { name: 'pipelines' }
  | { name: 'editor'; file: string }
  | { name: 'runs' }
  | { name: 'run'; id: string }
  | { name: 'secrets' }
  | { name: 'settings' };

export function parseHash(hash: string): Route {
  const parts = hash.replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent);
  const [head, ...rest] = parts;
  switch (head) {
    case 'editor':
      return rest.length ? { name: 'editor', file: rest.join('/') } : { name: 'pipelines' };
    case 'runs':
      return rest[0] ? { name: 'run', id: rest[0] } : { name: 'runs' };
    case 'secrets':
      return { name: 'secrets' };
    case 'settings':
      return { name: 'settings' };
    default:
      return { name: 'pipelines' };
  }
}

export function href(route: Route): string {
  switch (route.name) {
    case 'editor':
      return `#/editor/${route.file.split('/').map(encodeURIComponent).join('/')}`;
    case 'run':
      return `#/runs/${encodeURIComponent(route.id)}`;
    case 'pipelines':
      return '#/pipelines';
    default:
      return `#/${route.name}`;
  }
}

export function navigate(route: Route): void {
  window.location.hash = href(route);
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parseHash(window.location.hash));
    window.addEventListener('hashchange', onChange);
    return () => window.removeEventListener('hashchange', onChange);
  }, []);
  return route;
}
