/** What the Electron preload script exposes; absent when the UI runs in a plain browser. */
export interface OprBridge {
  isElectron: true;
  platform: string;
  appVersion: () => Promise<string>;
  pickFolder: () => Promise<string | null>;
  openPath: (path: string) => Promise<void>;
  showInFolder: (path: string) => Promise<void>;
  setCloseToTray: (value: boolean) => Promise<void>;
  setStartWithWindows: (value: boolean) => Promise<void>;
}

declare global {
  interface Window {
    opr?: OprBridge;
  }
}
