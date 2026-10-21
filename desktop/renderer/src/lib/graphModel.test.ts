import { describe, expect, it } from 'vitest';
import type { Graph } from '../api/types';
import {
  addStep,
  autoLayout,
  comparable,
  connect,
  downstream,
  edgeId,
  freePosition,
  isDirty,
  removeEdge,
  removeStep,
  renameStep,
  setEdgeWhen,
  setPosition,
  uniqueId,
  updateStep,
  validateClient,
  wouldCreateCycle,
} from './graphModel';

const step = (id: string, type = 'shell', params: Record<string, unknown> = { command: 'echo' }) => ({
  id,
  type,
  params,
  timeout: null,
  retries: 0,
  retry_delay: 0,
  ignore_failure: false,
});

function graph(): Graph {
  return {
    file: 'x.yaml',
    etag: 'e1',
    name: 'Demo',
    fail_fast: true,
    max_parallel: 4,
    steps: [step('a'), step('b'), step('c')],
    edges: [
      { id: 'a->b', source: 'a', target: 'b', when: 'success' },
      { id: 'b->c', source: 'b', target: 'c', when: 'failure' },
    ],
    positions: { a: [0, 0], b: [300, 0], c: [600, 0] },
    auto_layout: false,
    schedule: null,
    notifiers: [],
  };
}

describe('adding and removing steps', () => {
  it('creates unique ids and a default position', () => {
    const first = addStep(graph(), 'shell', [10, 20]);
    expect(first.id).toBe('shell');
    expect(first.graph.positions.shell).toEqual([10, 20]);
    const second = addStep(first.graph, 'shell', [0, 0]);
    expect(second.id).toBe('shell_1');
    expect(uniqueId(second.graph, 'shell')).toBe('shell_2');
  });

  it('python steps start with a script and shell steps with an empty command', () => {
    expect(addStep(graph(), 'python', [0, 0]).graph.steps.at(-1)?.params).toEqual({
      script: 'scripts/new_script.py',
    });
    expect(addStep(graph(), 'shell', [0, 0]).graph.steps.at(-1)?.params).toEqual({ command: '' });
  });

  it('removing a step drops its edges and position but keeps the others', () => {
    const next = removeStep(graph(), 'b');
    expect(next.steps.map((s) => s.id)).toEqual(['a', 'c']);
    expect(next.edges).toEqual([]);
    expect(Object.keys(next.positions)).toEqual(['a', 'c']);
  });

  it('does not mutate its input', () => {
    const g = graph();
    const snapshot = JSON.stringify(g);
    removeStep(g, 'a');
    addStep(g, 'shell', [1, 1]);
    renameStep(g, 'a', 'z');
    connect(g, 'a', 'c');
    expect(JSON.stringify(g)).toBe(snapshot);
  });
});

describe('renaming', () => {
  it('updates steps, edges (including their ids) and positions', () => {
    const result = renameStep(graph(), 'b', 'middle');
    if (!('graph' in result)) throw new Error('expected success');
    const g = result.graph;
    expect(g.steps.map((s) => s.id)).toEqual(['a', 'middle', 'c']);
    expect(g.edges.map((e) => [e.id, e.source, e.target, e.when])).toEqual([
      ['a->middle', 'a', 'middle', 'success'],
      ['middle->c', 'middle', 'c', 'failure'],
    ]);
    expect(g.positions.middle).toEqual([300, 0]);
    expect(g.positions.b).toBeUndefined();
  });

  it.each([
    ['', 'empty'],
    ['has space', 'invalid'],
    ['ñandú', 'invalid'],
    ['c', 'duplicate'],
  ])('rejects %j as %s', (name, error) => {
    expect(renameStep(graph(), 'b', name)).toEqual({ error });
  });

  it('renaming to the same id is a no-op', () => {
    const g = graph();
    expect(renameStep(g, 'a', 'a')).toEqual({ graph: g });
  });
});

describe('connecting', () => {
  it('adds an edge with the given condition', () => {
    const result = connect(graph(), 'a', 'c', 'always');
    if (!('graph' in result)) throw new Error('expected success');
    expect(result.graph.edges.at(-1)).toEqual({ id: 'a->c', source: 'a', target: 'c', when: 'always' });
  });

  it('refuses self loops, duplicates, cycles and unknown steps', () => {
    expect(connect(graph(), 'a', 'a')).toEqual({ error: 'self' });
    expect(connect(graph(), 'a', 'b')).toEqual({ error: 'duplicate' });
    expect(connect(graph(), 'c', 'a')).toEqual({ error: 'cycle' });
    expect(connect(graph(), 'a', 'zzz')).toEqual({ error: 'unknown' });
  });

  it('detects cycles through longer paths and allows parallel diamonds', () => {
    expect(wouldCreateCycle(graph(), 'c', 'a')).toBe(true);
    expect(wouldCreateCycle(graph(), 'c', 'b')).toBe(true);
    expect(wouldCreateCycle(graph(), 'a', 'c')).toBe(false); // a->b->c plus a->c is a diamond
  });

  it('removes edges and changes their condition', () => {
    expect(removeEdge(graph(), 'a->b').edges.map((e) => e.id)).toEqual(['b->c']);
    expect(setEdgeWhen(graph(), 'a->b', 'always').edges[0]?.when).toBe('always');
  });
});

describe('layout', () => {
  it('puts dependents one column to the right and branches in separate rows', () => {
    const g = graph();
    g.steps.push(step('d'));
    g.edges.push({ id: 'a->d', source: 'a', target: 'd', when: 'success' });
    const laid = autoLayout(g);
    expect(laid.positions.a).toEqual([0, 0]);
    expect(laid.positions.b?.[0]).toBe(300);
    expect(laid.positions.d?.[0]).toBe(300);
    expect(laid.positions.b?.[1]).not.toBe(laid.positions.d?.[1]);
    expect(laid.positions.c?.[0]).toBe(600);
    expect(laid.auto_layout).toBe(false);
  });

  it('survives an accidental cycle without hanging', () => {
    const g = graph();
    g.edges.push({ id: 'c->a', source: 'c', target: 'a', when: 'success' });
    expect(() => autoLayout(g)).not.toThrow();
  });
});

describe('validation', () => {
  it('accepts a healthy graph', () => {
    expect(validateClient(graph())).toEqual([]);
  });

  it('reports every kind of problem', () => {
    const g = graph();
    g.name = ' ';
    g.steps = [
      step('ok'),
      step('bad id'),
      step('ok'),
      step('py', 'python', { script: ' ' }),
      step('sh', 'shell', { command: '' }),
    ];
    const keys = validateClient(g).map((i) => i.key);
    expect(keys).toEqual(
      expect.arrayContaining(['empty_name', 'bad_id', 'duplicate_id', 'missing_script', 'missing_command']),
    );
    expect(validateClient({ ...graph(), steps: [] }).map((i) => i.key)).toContain('no_steps');
  });

  it('points at the offending step', () => {
    const g = graph();
    g.steps = [step('sh', 'shell', { command: '' })];
    expect(validateClient(g)).toEqual([{ key: 'missing_command', stepId: 'sh' }]);
  });
});

describe('dirty tracking', () => {
  it('ignores the etag and layout bookkeeping but notices real changes', () => {
    const saved = graph();
    expect(isDirty({ ...saved, etag: 'other', auto_layout: true }, saved)).toBe(false);
    expect(isDirty(updateStep(saved, 'a', { retries: 2 }), saved)).toBe(true);
    expect(isDirty(setPosition(saved, 'a', [5, 5]), saved)).toBe(true);
    expect(comparable(saved)).not.toContain('e1');
  });
});

describe('downstream', () => {
  it('lists a step and everything that depends on it, in declaration order', () => {
    expect(downstream(graph(), 'a')).toEqual(['a', 'b', 'c']);
    expect(downstream(graph(), 'b')).toEqual(['b', 'c']);
    expect(downstream(graph(), 'c')).toEqual(['c']);
  });
});

describe('edge ids', () => {
  it('matches the engine format', () => {
    expect(edgeId('x', 'y')).toBe('x->y');
  });
});

describe('freePosition', () => {
  it('keeps the desired spot when it is empty', () => {
    expect(freePosition(graph(), [0, 400])).toEqual([0, 400]);
  });

  it('moves a new node out from under existing ones', () => {
    const g = graph();
    const spot = freePosition(g, [310, 10]); // overlaps step b at [300, 0]
    const hits = Object.values(g.positions).some(
      ([x, y]) => Math.abs(x - spot[0]) < 224 && Math.abs(y - spot[1]) < 96,
    );
    expect(hits).toBe(false);
  });

  it('never returns a spot that overlaps, even in a crowded canvas', () => {
    let g = graph();
    for (let i = 0; i < 25; i += 1) {
      const added = addStep(g, 'shell', freePosition(g, [300, 0]));
      g = added.graph;
    }
    const positions = Object.values(g.positions);
    for (let i = 0; i < positions.length; i += 1) {
      for (let j = i + 1; j < positions.length; j += 1) {
        const [ax, ay] = positions[i] as [number, number];
        const [bx, by] = positions[j] as [number, number];
        expect(Math.abs(ax - bx) >= 224 || Math.abs(ay - by) >= 96).toBe(true);
      }
    }
  });
});
