import { _electron as electron, expect, test, type ElectronApplication, type Page } from '@playwright/test';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const desktop = path.resolve(__dirname, '..');

interface Launched {
  app: ElectronApplication;
  page: Page;
  data: string;
  workspace: string;
  dir: string;
}

async function launch(): Promise<Launched> {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'opr-electron-'));
  const data = path.join(dir, 'data');
  const workspace = path.join(dir, 'pipelines');
  fs.mkdirSync(path.join(workspace, 'scripts'), { recursive: true });
  fs.writeFileSync(path.join(workspace, 'scripts', 'ok.py'), "print('hello from electron')\n");
  fs.writeFileSync(
    path.join(workspace, 'demo.yaml'),
    'name: Electron demo\nsteps:\n  - {id: say, type: python, script: scripts/ok.py}\n',
  );
  const app = await electron.launch({
    args: [desktop, `--user-data-dir=${path.join(dir, "profile")}`, "--lang=en-US"],
    env: { ...process.env, OPR_DATA_DIR: data, OPR_WORKSPACE: workspace, ELECTRON_ENABLE_LOGGING: '' },
    timeout: 60_000,
  });
  const page = await app.firstWindow();
  return { app, page, data, workspace, dir };
}

async function cleanup(l: Launched | undefined): Promise<void> {
  if (!l) return;
  await l.app.close().catch(() => undefined);
  fs.rmSync(l.dir, { recursive: true, force: true });
}

const lock = (data: string): { pid: number; port: number; token: string } | null => {
  try {
    return JSON.parse(fs.readFileSync(path.join(data, 'engine.lock'), 'utf8'));
  } catch {
    return null;
  }
};

test.describe.configure({ mode: 'serial' });

test.describe('desktop shell', () => {
  let l: Launched | undefined;
  test.afterEach(async () => {
    await cleanup(l);
    l = undefined;
  });

  test('starts the engine, loads the UI and exposes the desktop bridge', async () => {
    l = await launch();
    await expect(l.page.getByText('Engine connected')).toBeVisible({ timeout: 40_000 });
    await expect(l.page.locator('.card', { hasText: 'Electron demo' })).toBeVisible();
    expect(await l.page.evaluate(() => window.opr?.isElectron)).toBe(true);
    expect(await l.page.title()).toBe('OPR Studio');

    await l.page.getByRole('link', { name: 'Settings' }).click();
    await expect(l.page.getByText('Keep running in the system tray when the window is closed')).toBeVisible();
    await expect(l.page.getByText('available in the desktop app')).toHaveCount(0);
  });

  test('runs a pipeline from the desktop app', async () => {
    l = await launch();
    await expect(l.page.locator('.card', { hasText: 'Electron demo' })).toBeVisible({ timeout: 40_000 });
    await l.page.getByRole('button', { name: 'Run' }).click();
    await expect(l.page.locator('.card', { hasText: 'Electron demo' })).toContainText('Succeeded', { timeout: 30_000 });
    await l.page.getByRole('link', { name: 'Runs' }).click();
    await l.page.locator('tbody a.name').first().click();
    await expect(l.page.locator('.log-row', { hasText: 'hello from electron' })).toBeVisible();
  });

  test('closing the window hides it to the tray and the engine keeps running', async () => {
    l = await launch();
    await expect(l.page.getByText('Engine connected')).toBeVisible({ timeout: 40_000 });
    const info = lock(l.data);
    expect(info).not.toBeNull();

    await l.app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.close());
    await expect
      .poll(() => l!.app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]?.isVisible() ?? false))
      .toBe(false);
    const health = await fetch(`http://127.0.0.1:${info!.port}/api/health`);
    expect(health.ok).toBe(true); // triggers and runs keep working in the background

    await l.app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.show());
    await expect
      .poll(() => l!.app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.isVisible()))
      .toBe(true);
  });

  test('quitting stops the engine and removes its lock file', async () => {
    l = await launch();
    await expect(l.page.getByText('Engine connected')).toBeVisible({ timeout: 40_000 });
    const info = lock(l.data)!;
    const closed = new Promise<void>((resolve) => l!.app.process().once('exit', () => resolve()));
    await l.app.evaluate(({ app }) => app.quit());
    await closed;
    await expect.poll(() => lock(l!.data), { timeout: 20_000 }).toBeNull();
    await expect
      .poll(async () => fetch(`http://127.0.0.1:${info.port}/api/health`).then(() => true, () => false), {
        timeout: 20_000,
      })
      .toBe(false);
  });

  test('restarts the engine by itself if it dies', async () => {
    l = await launch();
    await expect(l.page.getByText('Engine connected')).toBeVisible({ timeout: 40_000 });
    const first = lock(l.data)!;
    // Kill only the engine, not the app.
    spawnSync('taskkill', ['/PID', String(first.pid), '/T', '/F'], { windowsHide: true });
    await expect
      .poll(() => lock(l!.data)?.port, { timeout: 40_000 })
      .not.toBe(first.port);
    await expect(l.page.getByText('Engine connected')).toBeVisible({ timeout: 40_000 });
    await expect(l.page.locator('.card', { hasText: 'Electron demo' })).toBeVisible();
  });

  test('a second launch focuses the running app instead of starting another engine', async () => {
    l = await launch();
    await expect(l.page.getByText('Engine connected')).toBeVisible({ timeout: 40_000 });
    const profile = path.join(l.dir, 'profile');
    const second = await electron
      .launch({
        args: [desktop, `--user-data-dir=${profile}`, "--lang=en-US"],
        env: { ...process.env, OPR_DATA_DIR: l.data, OPR_WORKSPACE: l.workspace },
        timeout: 20_000,
      })
      .catch(() => null);
    await second?.close().catch(() => undefined);
    expect(lock(l.data)).not.toBeNull();
    const health = await fetch(`http://127.0.0.1:${lock(l.data)!.port}/api/health`);
    expect(health.ok).toBe(true);
  });
});
