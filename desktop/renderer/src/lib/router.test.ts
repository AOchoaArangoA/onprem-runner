import { describe, expect, it } from 'vitest';
import { href, parseHash, type Route } from './router';

describe('router', () => {
  it.each<[string, Route]>([
    ['', { name: 'pipelines' }],
    ['#/', { name: 'pipelines' }],
    ['#/unknown', { name: 'pipelines' }],
    ['#/runs', { name: 'runs' }],
    ['#/runs/20260101-000000-abcdef', { name: 'run', id: '20260101-000000-abcdef' }],
    ['#/secrets', { name: 'secrets' }],
    ['#/settings', { name: 'settings' }],
    ['#/editor', { name: 'pipelines' }],
    ['#/editor/demo.yaml', { name: 'editor', file: 'demo.yaml' }],
    ['#/editor/sub%20dir/mi%20flujo.yaml', { name: 'editor', file: 'sub dir/mi flujo.yaml' }],
  ])('parses %j', (hash, expected) => {
    expect(parseHash(hash)).toEqual(expected);
  });

  it('round-trips routes through href and parseHash', () => {
    const routes: Route[] = [
      { name: 'pipelines' },
      { name: 'runs' },
      { name: 'run', id: 'a b' },
      { name: 'editor', file: 'sub dir/ñ & x.yaml' },
      { name: 'secrets' },
      { name: 'settings' },
    ];
    for (const route of routes) expect(parseHash(href(route))).toEqual(route);
  });
});
