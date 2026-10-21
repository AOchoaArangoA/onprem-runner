import { test as base, expect, type Locator, type Page } from '@playwright/test';
import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const root = path.resolve(__dirname, '..', '..');
const OPR = process.env.OPR_BIN ?? path.join(root, '.venv', 'Scripts', 'opr.exe');
const UI = path.join(root, 'desktop', 'renderer', 'dist');
export const TOKEN = 'e2e-token';

export interface Studio {
  page: Page;
  url: string;
  port: number;
  workspace: string;
  dataDir: string;
  /** Write a pipeline YAML (and return its path) into the workspace. */
  writePipeline: (file: string, yaml: string) => string;
  readPipeline: (file: string) => string;
  api: <T = unknown>(method: string, route: string, body?: unknown) => Promise<T>;
  /** Open the app on the given hash route. */
  open: (hash?: string) => Promise<void>;
}

const SCRIPTS: Record<string, string> = {
  'ok.py': "print('ok')\n",
  'chatty.py':
    "import sys\nprint('starting')\nprint('{\"level\":\"warn\",\"msg\":\"careful\",\"n\":3}')\nprint('oops', file=sys.stderr)\n",
  'fail.py': "import sys\nprint('about to fail')\nsys.exit(3)\n",
  'slow.py': "import time\nprint('slow start', flush=True)\ntime.sleep(60)\n",
  'secret.py': "import os\nprint('token is', os.environ['T'])\n",
};

async function startEngine(workspace: string, dataDir: string): Promise<{ proc: ChildProcess; port: number }> {
  const proc = spawn(
    OPR,
    ['serve', '--data-dir', dataDir, '--workspace', workspace, '--port', '0', '--ui-dir', UI],
    { env: { ...process.env, OPR_TOKEN: TOKEN }, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true },
  );
  const port = await new Promise<number>((resolve, reject) => {
    let output = '';
    const timer = setTimeout(() => reject(new Error(`engine did not start:\n${output}`)), 30_000);
    proc.stdout?.on('data', (chunk: Buffer) => {
      output += chunk.toString();
      const line = output.split('\n').find((l) => l.includes('"ready"'));
      if (line) {
        clearTimeout(timer);
        resolve((JSON.parse(line) as { port: number }).port);
      }
    });
    proc.stderr?.on('data', (chunk: Buffer) => (output += chunk.toString()));
    proc.on('exit', (code) => reject(new Error(`engine exited early (${code}):\n${output}`)));
  });
  return { proc, port };
}

function stopEngine(proc: ChildProcess, port: number): Promise<void> {
  return fetch(`http://127.0.0.1:${port}/api/shutdown`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${TOKEN}`, 'Content-Type': 'application/json' },
    body: '{}',
  })
    .catch(() => undefined)
    .then(
      () =>
        new Promise<void>((resolve) => {
          const done = () => {
            if (proc.pid && process.platform === 'win32')
              spawnSync('taskkill', ['/PID', String(proc.pid), '/T', '/F'], { windowsHide: true });
            resolve();
          };
          const timer = setTimeout(done, 8000);
          proc.once('exit', () => {
            clearTimeout(timer);
            done();
          });
        }),
    );
}

export const test = base.extend<{ studio: Studio }>({
  studio: async ({ page }, use) => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'opr-e2e-'));
    const workspace = path.join(dir, 'ws');
    const dataDir = path.join(dir, 'data');
    fs.mkdirSync(path.join(workspace, 'scripts'), { recursive: true });
    for (const [name, body] of Object.entries(SCRIPTS))
      fs.writeFileSync(path.join(workspace, 'scripts', name), body);

    const { proc, port } = await startEngine(workspace, dataDir);
    const url = `http://127.0.0.1:${port}`;
    const api = async <T>(method: string, route: string, body?: unknown): Promise<T> => {
      const response = await fetch(`${url}${route}`, {
        method,
        headers: { Authorization: `Bearer ${TOKEN}`, 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const text = await response.text();
      if (!response.ok) throw new Error(`${method} ${route} -> ${response.status} ${text}`);
      return (text ? JSON.parse(text) : null) as T;
    };

    const studio: Studio = {
      page,
      url,
      port,
      workspace,
      dataDir,
      writePipeline: (file, yaml) => {
        const target = path.join(workspace, file);
        fs.mkdirSync(path.dirname(target), { recursive: true });
        fs.writeFileSync(target, yaml);
        return target;
      },
      readPipeline: (file) => fs.readFileSync(path.join(workspace, file), 'utf8'),
      api,
      open: async (hash = '#/pipelines') => {
        await page.goto(`${url}/?token=${TOKEN}${hash}`);
        await expect(page.getByText('Engine connected')).toBeVisible();
      },
    };
    try {
      await use(studio);
    } finally {
      await stopEngine(proc, port);
      fs.rmSync(dir, { recursive: true, force: true });
    }
  },
});

export { expect };

// -- helpers shared by the specs ----------------------------------------------------------------

export const node = (page: Page, id: string): Locator =>
  page.locator('.react-flow__node').filter({ has: page.locator('.step-id', { hasText: new RegExp(`^${id}$`) }) });

/** Drag a connection from the right handle of `from` to the left handle of `to`. */
export async function connect(page: Page, from: string, to: string): Promise<void> {
  const source = await node(page, from).locator('.react-flow__handle.source').boundingBox();
  const target = await node(page, to).locator('.react-flow__handle.target').boundingBox();
  if (!source || !target) throw new Error('handles not found');
  await page.mouse.move(source.x + source.width / 2, source.y + source.height / 2);
  await page.mouse.down();
  await page.mouse.move(target.x + target.width / 2, target.y + target.height / 2, { steps: 12 });
  await page.mouse.up();
}

export async function waitForRunToFinish(studio: Studio): Promise<void> {
  await expect
    .poll(async () => (await studio.api<{ active: unknown[] }>('GET', '/api/state')).active.length, {
      timeout: 40_000,
    })
    .toBe(0);
}

export const SIMPLE_FLOW = `name: Demo flow
steps:
  - {id: extract, type: python, script: scripts/chatty.py}
  - {id: publish, type: shell, command: echo published, needs: [extract]}
`;
