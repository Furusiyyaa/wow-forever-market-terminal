const { app, BrowserWindow } = require('electron');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const http = require('http');

const API_PORT = 8765;
const REPO_ROOT = path.resolve(__dirname, '..', '..');
const API_SCRIPT = path.join(REPO_ROOT, 'telemetry', 'api_server.py');

function findPythonExe() {
  const candidates = [
    path.join(REPO_ROOT, '.venv', 'Scripts', 'python.exe'),
    path.join(REPO_ROOT, '.venv-manager', 'Scripts', 'python.exe'),
  ];
  for (const c of candidates) {
    if (fs.existsSync(c)) return c;
  }
  return 'python';
}

const PYTHON_EXE = findPythonExe();

let apiProc = null;
let mainWindow = null;

function waitForApi(retries = 40) {
  return new Promise((resolve, reject) => {
    const attempt = (left) => {
      const req = http.get(`http://127.0.0.1:${API_PORT}/api/status`, (res) => {
        res.resume();
        resolve();
      });
      req.on('error', () => {
        if (left <= 0) return reject(new Error('API server failed to start'));
        setTimeout(() => attempt(left - 1), 250);
      });
      req.end();
    };
    attempt(retries);
  });
}

function startApiServer() {
  apiProc = spawn(PYTHON_EXE, [API_SCRIPT], {
    cwd: REPO_ROOT,
    env: { ...process.env, TELEMETRY_API_PORT: String(API_PORT) },
    stdio: 'ignore',
    windowsHide: true,
  });
}

function stopApiServer() {
  if (apiProc && !apiProc.killed) {
    try {
      apiProc.kill();
    } catch (_) {}
    apiProc = null;
  }
}

async function createWindow() {
  startApiServer();
  try {
    await waitForApi();
  } catch (err) {
    console.error(err);
  }

  mainWindow = new BrowserWindow({
    width: 1380,
    height: 860,
    minWidth: 1080,
    minHeight: 680,
    backgroundColor: '#131722',
    title: 'WoW Forever — Market Terminal',
    autoHideMenuBar: true,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
    mainWindow.focus();
  });
  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

app.whenReady().then(createWindow);
app.on('before-quit', stopApiServer);
app.on('window-all-closed', () => {
  stopApiServer();
  app.quit();
});
