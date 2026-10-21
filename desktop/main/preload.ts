import { contextBridge, ipcRenderer } from 'electron';

// The only door between the web UI and the desktop shell. Everything is request/response
// with a fixed set of channels; the UI never gets Node or Electron objects.
contextBridge.exposeInMainWorld('opr', {
  isElectron: true,
  platform: process.platform,
  appVersion: (): Promise<string> => ipcRenderer.invoke('opr:appVersion'),
  pickFolder: (): Promise<string | null> => ipcRenderer.invoke('opr:pickFolder'),
  openPath: (target: string): Promise<void> => ipcRenderer.invoke('opr:openPath', target),
  showInFolder: (target: string): Promise<void> => ipcRenderer.invoke('opr:showInFolder', target),
  setCloseToTray: (value: boolean): Promise<void> => ipcRenderer.invoke('opr:setCloseToTray', value),
  setStartWithWindows: (value: boolean): Promise<void> =>
    ipcRenderer.invoke('opr:setStartWithWindows', value),
});
