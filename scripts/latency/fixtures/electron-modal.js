const {app, BrowserWindow, ipcMain} = require('electron');
app.whenReady().then(() => {
  const win = new BrowserWindow({width:1000,height:700,
    webPreferences:{nodeIntegration:true,contextIsolation:false}});
  win.loadURL('data:text/html,'+encodeURIComponent(`
    <title>Parent unchanged</title><input aria-label="Main field">
    <button onclick="document.title='BACKGROUND ACTIVATED'">Count up</button>
    <select aria-label="Fruit" onchange="document.title='BACKGROUND ACTIVATED'">
      <option>apple</option><option>banana</option></select>
    <script>document.onkeydown=e=>{if(e.ctrlKey&&e.shiftKey&&e.key.toLowerCase()==='y')require('electron').ipcRenderer.send('open')}</script>`));
  ipcMain.on('open', () => new BrowserWindow({parent:win,modal:true,width:420,height:260,
    webPreferences:{nodeIntegration:true,contextIsolation:false}}).loadURL('data:text/html,'+
      encodeURIComponent('<title>Settings Modal</title><input aria-label="Name">')));
});
