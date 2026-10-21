import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

export interface EngineConfig {
  /** Command to run (the packaged engine .exe, or `opr` from a Python environment in dev). */
  command: string;
  dataDir: string;
  uiDir: string;
  /** Folder for pipelines on first run; afterwards the engine remembers the user's choice. */
  defaultWorkspace: string;
}

export interface RunningEngine {
  port: number;
  token: string;
  process: ChildProcess;
  url: string;
}

const READY_TIMEOUT_MS = 45_000;

export function isWritable(directory: string): boolean {
  try {
    fs.mkdirSync(directory, { recursive: true });
    const probe = path.join(directory, `.write-test-${process.pid}`);
    fs.writeFileSync(probe, '');
    fs.rmSync(probe);
    return true;
  } catch {
    return false;
  }
}

export function startEngine(config: EngineConfig): Promise<RunningEngine> {
  const token = crypto.randomBytes(24).toString('base64url');
  const args = ['serve', '--data-dir', config.dataDir, '--ui-dir', config.uiDir, '--port', '0'];
  // Only the very first start names the workspace; later starts honour the saved setting.
  if (!fs.existsSync(path.join(config.dataDir, 'settings.json'))) {
    fs.mkdirSync(config.defaultWorkspace, { recursive: true });
    args.push('--workspace', config.defaultWorkspace);
  }
  const child = spawn(config.command, args, {
    env: { ...process.env, OPR_TOKEN: token },
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });

  return new Promise((resolve, reject) => {
    let output = '';
    const timer = setTimeout(() => {
      killTree(child);
      reject(new Error(`The engine did not start in time.\n${output}`));
    }, READY_TIMEOUT_MS);

    child.stdout?.on('data', (chunk: Buffer) => {
      output += chunk.toString();
      const line = output.split('\n').find((l) => l.includes('"ready"'));
      if (!line) return;
      clearTimeout(timer);
      const { port } = JSON.parse(line) as { port: number };
      resolve({ port, token, process: child, url: `http://127.0.0.1:${port}` });
    });
    child.stderr?.on('data', (chunk: Buffer) => {
      output += chunk.toString();
    });
    child.once('error', (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.once('exit', (code) => {
      clearTimeout(timer);
      reject(new Error(`The engine exited with code ${code}.\n${output}`));
    });
  });
}

/** Kill a process and everything it started (a venv `opr.exe` is a launcher with a child). */
export function killTree(child: ChildProcess): void {
  if (!child.pid || child.exitCode !== null) return;
  if (process.platform === 'win32')
    spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true });
  else child.kill('SIGKILL');
}

export async function stopEngine(engine: RunningEngine): Promise<void> {
  const child = engine.process;
  if (child.exitCode !== null) return;
  const exited = new Promise<void>((resolve) => child.once('exit', () => resolve()));
  try {
    await fetch(`${engine.url}/api/shutdown`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${engine.token}`, 'Content-Type': 'application/json' },
      body: '{}',
    });
  } catch {
    /* already gone */
  }
  const timeout = new Promise<void>((resolve) => setTimeout(resolve, 10_000));
  await Promise.race([exited, timeout]);
  killTree(child); // no-op if it exited cleanly; ends stragglers otherwise
}

export async function api<T>(engine: RunningEngine, route: string): Promise<T> {
  const response = await fetch(`${engine.url}${route}`, {
    headers: { Authorization: `Bearer ${engine.token}` },
  });
  if (!response.ok) throw new Error(`${route} -> ${response.status}`);
  return (await response.json()) as T;
}

/** Follow the engine's event stream; resolves when the stream ends. */
export async function followEvents(
  engine: RunningEngine,
  onEvent: (event: Record<string, unknown>) => void,
  signal: AbortSignal,
): Promise<void> {
  const response = await fetch(`${engine.url}/api/events`, {
    headers: { Authorization: `Bearer ${engine.token}` },
    signal,
  });
  if (!response.body) return;
  const decoder = new TextDecoder();
  let buffer = '';
  for await (const chunk of response.body as unknown as AsyncIterable<Uint8Array>) {
    buffer += decoder.decode(chunk, { stream: true });
    let index: number;
    while ((index = buffer.indexOf('\n\n')) >= 0) {
      const block = buffer.slice(0, index);
      buffer = buffer.slice(index + 2);
      const data = block.split('\n').find((line) => line.startsWith('data: '));
      if (data) {
        try {
          onEvent(JSON.parse(data.slice(6)) as Record<string, unknown>);
        } catch {
          /* ignore a malformed event */
        }
      }
    }
  }
}
