import {
  app,
  BrowserWindow,
  dialog,
  ipcMain,
  Menu,
  nativeImage,
  Notification,
  shell,
  Tray,
} from 'electron';
import fs from 'node:fs';
import path from 'node:path';
import {
  api,
  followEvents,
  isWritable,
  killTree,
  startEngine,
  stopEngine,
  type RunningEngine,
} from './engine';

app.setName('OPR Studio');

const SINGLE_INSTANCE = app.requestSingleInstanceLock();
const START_HIDDEN = process.argv.includes('--hidden');
const MAX_ENGINE_RESTARTS = 3;

let engine: RunningEngine | null = null;
let win: BrowserWindow | null = null;
let tray: Tray | null = null;
let quitting = false;
let closeToTray = true;
let restarts = 0;
let trayHintShown = false;
let eventsAbort: AbortController | null = null;

const spanish = () => app.getLocale().toLowerCase().startsWith('es');
const text = (es: string, en: string) => (spanish() ? es : en);

// -- locations ---------------------------------------------------------------------------------

const repoRoot = path.resolve(__dirname, '..', '..', '..');
const assets = app.isPackaged
  ? path.join(process.resourcesPath, 'assets')
  : path.resolve(__dirname, '..', '..', 'assets');

/** The folder holding the portable .exe (electron-builder exports it; else the exe's folder). */
function portableDir(): string {
  return process.env.PORTABLE_EXECUTABLE_DIR ?? path.dirname(app.getPath('exe'));
}

function dataDir(): string {
  if (process.env.OPR_DATA_DIR) return process.env.OPR_DATA_DIR;
  if (!app.isPackaged) return path.join(repoRoot, 'desktop', '.dev-data', 'data');
  const portable = path.join(portableDir(), 'data');
  return isWritable(portable) ? portable : path.join(app.getPath('appData'), 'OPR Studio');
}

function defaultWorkspace(): string {
  if (process.env.OPR_WORKSPACE) return process.env.OPR_WORKSPACE;
  if (!app.isPackaged) return path.join(repoRoot, 'desktop', '.dev-data', 'pipelines');
  const next = path.join(portableDir(), 'pipelines');
  return isWritable(next) ? next : path.join(dataDir(), 'pipelines');
}

function engineCommand(): string {
  if (process.env.OPR_ENGINE) return process.env.OPR_ENGINE;
  if (app.isPackaged) return path.join(process.resourcesPath, 'engine', 'opr-engine.exe');
  return path.join(repoRoot, '.venv', 'Scripts', 'opr.exe');
}

const uiDir = () =>
  app.isPackaged ? path.join(process.resourcesPath, 'ui') : path.join(repoRoot, 'desktop', 'renderer', 'dist');

// -- engine lifecycle ----------------------------------------------------------------------------

async function bootEngine(): Promise<RunningEngine> {
  const running = await startEngine({
    command: engineCommand(),
    dataDir: dataDir(),
    uiDir: uiDir(),
    defaultWorkspace: defaultWorkspace(),
  });
  running.process.once('exit', () => void onEngineExit(running));
  return running;
}

async function onEngineExit(exited: RunningEngine): Promise<void> {
  if (quitting || engine !== exited) return;
  eventsAbort?.abort();
  if (restarts >= MAX_ENGINE_RESTARTS) {
    dialog.showErrorBox(
      'OPR Studio',
      text(
        'El motor se detuvo varias veces seguidas. Reinicia la aplicación.',
        'The engine stopped several times in a row. Please restart the app.',
      ),
    );
    quitting = true;
    app.quit();
    return;
  }
  restarts += 1;
  try {
    engine = await bootEngine();
    await win?.loadURL(appUrl());
    watchEvents();
  } catch (error) {
    dialog.showErrorBox('OPR Studio', String(error));
    quitting = true;
    app.quit();
  }
}

const appUrl = () => `${engine?.url}/?token=${engine?.token}`;

async function activeRunCount(): Promise<number> {
  if (!engine) return 0;
  try {
    return (await api<{ active: unknown[] }>(engine, '/api/state')).active.length;
  } catch {
    return 0;
  }
}

async function requestQuit(): Promise<void> {
  if (quitting) return;
  const active = await activeRunCount();
  if (active > 0) {
    const answer = await dialog.showMessageBox({
      type: 'warning',
      buttons: [text('Salir y cancelar', 'Quit and cancel'), text('Seguir ejecutando', 'Keep running')],
      defaultId: 1,
      cancelId: 1,
      title: 'OPR Studio',
      message: text(
        `Hay ${active} pipeline(s) en ejecución.`,
        `${active} pipeline(s) are running.`,
      ),
      detail: text(
        'Si sales ahora se cancelarán.',
        'They will be cancelled if you quit now.',
      ),
    });
    if (answer.response !== 0) return;
  }
  quitting = true;
  eventsAbort?.abort();
  if (engine) await stopEngine(engine);
  app.quit();
}

// -- startup feedback ------------------------------------------------------------------------------

/** Appends to `<data>/startup.log`, so a slow or failed start on another PC can be diagnosed. */
function logStartup(message: string): void {
  try {
    const dir = dataDir();
    fs.mkdirSync(dir, { recursive: true });
    fs.appendFileSync(path.join(dir, 'startup.log'), `${new Date().toISOString()} ${message}\n`);
  } catch {
    /* logging must never break the start */
  }
}

let splash: BrowserWindow | null = null;

/** A small window shown at once, because the engine needs a few seconds to boot. */
function showSplash(): void {
  splash = new BrowserWindow({
    width: 360,
    height: 180,
    frame: false,
    resizable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    backgroundColor: '#0e1117',
    icon: path.join(assets, 'icon.png'),
  });
  const html = `<body style="margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;
    justify-content:center;gap:12px;background:#0e1117;color:#e6edf3;font:15px Segoe UI,sans-serif">
    <b style="font-size:20px">OPR Studio</b><span>${text('Iniciando el motor…', 'Starting the engine…')}</span></body>`;
  void splash.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
  splash.on('closed', () => {
    splash = null;
  });
}

function closeSplash(): void {
  if (splash && !splash.isDestroyed()) splash.close();
}

// -- window ----------------------------------------------------------------------------------------

function showWindow(): void {
  if (!win) return;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

function createWindow(): void {
  win = new BrowserWindow({
    width: 1360,
    height: 860,
    minWidth: 980,
    minHeight: 620,
    show: false,
    title: 'OPR Studio',
    icon: path.join(assets, 'icon.png'),
    backgroundColor: '#0e1117',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    },
  });

  win.once('ready-to-show', () => {
    closeSplash();
    logStartup('window ready');
    if (!START_HIDDEN) win?.show();
  });

  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url)) void shell.openExternal(url);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (event, url) => {
    if (engine && !url.startsWith(engine.url)) {
      event.preventDefault();
      if (/^https?:\/\//.test(url)) void shell.openExternal(url);
    }
  });

  win.on('close', (event) => {
    if (quitting) return;
    event.preventDefault();
    if (closeToTray) {
      win?.hide();
      if (!trayHintShown && Notification.isSupported()) {
        trayHintShown = true;
        new Notification({
          title: 'OPR Studio',
          body: text(
            'Sigue ejecutándose en segundo plano. Ábrelo desde la bandeja del sistema.',
            'Still running in the background. Open it from the system tray.',
          ),
        }).show();
      }
    } else {
      void requestQuit();
    }
  });
  win.on('closed', () => {
    win = null;
  });

  if (app.isPackaged) {
    // The packaged app has no developer tooling and no menu bar.
    Menu.setApplicationMenu(null);
    win.webContents.on('before-input-event', (event, input) => {
      if (input.key === 'F12' || (input.control && input.shift && input.key.toLowerCase() === 'i'))
        event.preventDefault();
    });
  }
  void win.loadURL(appUrl());
}

// -- tray ----------------------------------------------------------------------------------------------

function createTray(): void {
  tray = new Tray(nativeImage.createFromPath(path.join(assets, 'tray.png')));
  tray.setToolTip('OPR Studio');
  const refresh = async () => {
    const running = await activeRunCount();
    tray?.setToolTip(
      running > 0 ? `OPR Studio — ${text(`${running} en ejecución`, `${running} running`)}` : 'OPR Studio',
    );
    tray?.setContextMenu(
      Menu.buildFromTemplate([
        { label: text('Abrir OPR Studio', 'Open OPR Studio'), click: showWindow },
        {
          label: running > 0 ? text(`${running} en ejecución`, `${running} running`) : text('Nada en ejecución', 'Nothing running'),
          enabled: false,
        },
        { type: 'separator' },
        { label: text('Salir', 'Quit'), click: () => void requestQuit() },
      ]),
    );
  };
  tray.on('click', showWindow);
  tray.on('double-click', showWindow);
  void refresh();
  setInterval(() => void refresh(), 5000).unref();
}

// -- desktop notifications ----------------------------------------------------------------------------

function watchEvents(): void {
  eventsAbort?.abort();
  const abort = new AbortController();
  eventsAbort = abort;
  const current = engine;
  if (!current) return;
  void followEvents(
    current,
    (event) => {
      if (event.type !== 'run_finished' || (win?.isFocused() ?? false)) return;
      const status = String(event.status);
      if (status === 'cancelled' || !Notification.isSupported()) return;
      void api<{ pipeline_name: string; run_id: string }>(current, `/api/runs/${String(event.run_id)}`)
        .then((run) => {
          const note = new Notification({
            title: run.pipeline_name,
            body:
              status === 'success'
                ? text('Terminó correctamente.', 'Finished successfully.')
                : text('Terminó con errores.', 'Finished with errors.'),
          });
          note.on('click', () => {
            showWindow();
            void win?.webContents.executeJavaScript(`location.hash = '#/runs/${run.run_id}'`);
          });
          note.show();
        })
        .catch(() => undefined);
    },
    abort.signal,
  ).catch(() => undefined);
}

// -- IPC --------------------------------------------------------------------------------------------------

function registerIpc(): void {
  ipcMain.handle('opr:appVersion', () => app.getVersion());
  ipcMain.handle('opr:pickFolder', async () => {
    const options = { properties: ['openDirectory' as const, 'createDirectory' as const] };
    const result = win ? await dialog.showOpenDialog(win, options) : await dialog.showOpenDialog(options);
    return result.canceled ? null : (result.filePaths[0] ?? null);
  });
  ipcMain.handle('opr:openPath', (_event, target: unknown) => {
    if (typeof target === 'string') return shell.openPath(target).then(() => undefined);
    return undefined;
  });
  ipcMain.handle('opr:showInFolder', (_event, target: unknown) => {
    if (typeof target === 'string') shell.showItemInFolder(target);
  });
  ipcMain.handle('opr:setCloseToTray', (_event, value: unknown) => {
    closeToTray = value === true;
  });
  ipcMain.handle('opr:setStartWithWindows', (_event, value: unknown) => {
    app.setLoginItemSettings({ openAtLogin: value === true, args: ['--hidden'] });
  });
}

// -- start ----------------------------------------------------------------------------------------------------

if (!SINGLE_INSTANCE) {
  app.quit();
} else {
  app.on('second-instance', () => showWindow());
  app.on('window-all-closed', () => {
    /* the tray keeps the app (and its triggers) alive */
  });
  app.on('before-quit', (event) => {
    if (!quitting) {
      event.preventDefault();
      void requestQuit();
    }
  });
  process.on('exit', () => {
    if (engine) killTree(engine.process);
  });

  void app.whenReady().then(async () => {
    logStartup(`start v${app.getVersion()} (${process.versions.electron}) from ${app.getPath('exe')}`);
    if (!START_HIDDEN) showSplash();
    registerIpc();
    try {
      engine = await bootEngine();
      logStartup(`engine ready on port ${engine.port}`);
    } catch (error) {
      logStartup(`engine failed: ${String(error)}`);
      closeSplash();
      dialog.showErrorBox(
        'OPR Studio',
        text('No se pudo iniciar el motor.\n\n', 'The engine could not start.\n\n') + String(error),
      );
      quitting = true;
      app.quit();
      return;
    }
    try {
      const settings = await api<{ close_to_tray: boolean }>(engine, '/api/settings');
      closeToTray = settings.close_to_tray;
    } catch {
      /* keep the default */
    }
    createWindow();
    createTray();
    watchEvents();
  });
}
