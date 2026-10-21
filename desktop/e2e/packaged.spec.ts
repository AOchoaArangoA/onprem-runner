import { _electron as electron, expect, test } from '@playwright/test';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

// Tests of the BUILT application. They need `npm run dist` first and are skipped otherwise.
const release = path.resolve(__dirname, '..', 'release');
const unpacked = path.join(release, 'win-unpacked', 'OPR Studio.exe');
const portable = fs.existsSync(release)
  ? fs.readdirSync(release).find((f) => /^OPR-Studio-.*-portable\.exe$/.test(f))
  : undefined;

test.describe.configure({ mode: 'serial' });

const lockOf = (data: string): { pid: number; port: number; token: string } | null => {
  try {
    return JSON.parse(fs.readFileSync(path.join(data, 'engine.lock'), 'utf8'));
  } catch {
    return null;
  }
};

test.describe('packaged app', () => {
  test.skip(!fs.existsSync(unpacked), 'run `npm run dist` first');

  test('starts without a Python install and runs a pipeline with its own engine', async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'opr-packaged-'));
    const app = await electron.launch({
      executablePath: unpacked,
      args: [`--user-data-dir=${path.join(dir, 'profile')}`, '--lang=en-US'],
      env: {
        ...process.env,
        OPR_DATA_DIR: path.join(dir, 'data'),
        OPR_WORKSPACE: path.join(dir, 'pipelines'),
        // Make sure nothing from a development setup leaks in.
        PYTHONPATH: '',
        OPR_ENGINE: '',
      },
      timeout: 90_000,
    });
    try {
      const page = await app.firstWindow();
      await expect(page.getByText('Engine connected')).toBeVisible({ timeout: 60_000 });
      await expect(page.getByText('No pipelines yet')).toBeVisible();

      await page.getByRole('button', { name: 'New pipeline' }).first().click();
      await page.getByLabel('Pipeline name').fill('Packaged smoke test');
      await page.getByRole('button', { name: 'Create', exact: true }).click();
      await expect(page).toHaveURL(/#\/editor\/packaged-smoke-test\.yaml/);
      await page.getByRole('button', { name: 'Run', exact: true }).click();
      await expect(page.locator('.log-row', { hasText: 'Hello from OPR Studio' })).toBeVisible({ timeout: 30_000 });
      await expect(page.locator('.step-node.ns-success')).toHaveCount(1);
      await page.screenshot({ path: path.join(release, 'packaged-editor.png') });

      // A Python step with the embedded interpreter (no Python installed is required).
      fs.mkdirSync(path.join(dir, 'pipelines', 'scripts'), { recursive: true });
      fs.writeFileSync(path.join(dir, 'pipelines', 'scripts', 'py.py'), "import sys\nprint('python', sys.version_info[0])\n");
      fs.writeFileSync(
        path.join(dir, 'pipelines', 'py.yaml'),
        'name: Embedded python\nsteps:\n  - {id: py, type: python, script: scripts/py.py, python: embedded}\n',
      );
      await page.getByRole('navigation').getByRole('link', { name: 'Pipelines' }).click();
      const card = page.locator('.card', { hasText: 'Embedded python' });
      await expect(card).toBeVisible({ timeout: 15_000 });
      await card.getByRole('button', { name: 'Run' }).click();
      await expect(card).toContainText('Succeeded', { timeout: 30_000 });
    } finally {
      await app.close().catch(() => undefined);
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
});

test.describe('portable executable', () => {
  test.skip(!portable, 'run `npm run dist` first');

  test('is one file, keeps its data next to itself and stops cleanly', async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'opr-portable-'));
    const exe = path.join(dir, portable!);
    fs.copyFileSync(path.join(release, portable!), exe); // as if copied to a USB stick
    expect(fs.readdirSync(dir)).toEqual([portable]);

    const started = Date.now();
    const child = spawn(exe, ['--lang=en-US'], {
      cwd: dir,
      env: { ...process.env, PORTABLE_EXECUTABLE_DIR: dir, OPR_DATA_DIR: '', OPR_WORKSPACE: '' },
      stdio: 'ignore',
      detached: false,
      windowsHide: true,
    });
    try {
      const dataDir = path.join(dir, 'data');
      await expect.poll(() => lockOf(dataDir)?.port, { timeout: 90_000, intervals: [500] }).toBeGreaterThan(0);
      const info = lockOf(dataDir)!;
      console.log(`portable startup to engine ready: ${((Date.now() - started) / 1000).toFixed(1)} s`);

      const health = await fetch(`http://127.0.0.1:${info.port}/api/health`);
      expect((await health.json()).ok).toBe(true);
      const state = await fetch(`http://127.0.0.1:${info.port}/api/state`, {
        headers: { Authorization: `Bearer ${info.token}` },
      }).then((r) => r.json());
      expect(fs.realpathSync.native(state.workspace)).toBe(fs.realpathSync.native(path.join(dir, 'pipelines')));

      // Data lives beside the .exe: database, settings, pipelines folder.
      expect(fs.existsSync(path.join(dataDir, 'opr.duckdb'))).toBe(true);
      expect(fs.existsSync(path.join(dataDir, 'settings.json'))).toBe(true);
      expect(fs.existsSync(path.join(dir, 'pipelines'))).toBe(true);

      // The engine answers only with the session token.
      const denied = await fetch(`http://127.0.0.1:${info.port}/api/state`);
      expect(denied.status).toBe(401);
    } finally {
      if (child.pid) spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true });
      await new Promise((r) => setTimeout(r, 1500));
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
});
