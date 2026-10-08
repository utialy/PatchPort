const http = require('node:http');
let body = process.argv[2];
try { const event = JSON.parse(body); event.patchport_codex_home = process.env.CODEX_HOME || require('node:path').join(require('node:os').homedir(), '.codex'); body = JSON.stringify(event); } catch { body = ''; }
if (body && body.length <= 131072 && process.env.PATCHPORT_ENDPOINT && process.env.PATCHPORT_TOKEN) {
  const url = new URL(process.env.PATCHPORT_ENDPOINT);
  if (url.hostname === '127.0.0.1') {
    const request = http.request(url, { method: 'POST', timeout: 3000, headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${process.env.PATCHPORT_TOKEN}` } }, response => response.resume());
    request.on('error', () => {}); request.on('timeout', () => request.destroy()); request.end(body);
  }
}
