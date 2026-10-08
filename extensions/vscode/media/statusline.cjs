const http = require('node:http');
let input = '';
process.stdin.on('data', chunk => { if (input.length < 131072) input += chunk; });
process.stdin.on('end', () => {
  try {
    const event = JSON.parse(input); event.patchport_statusline = true;
    const request = http.request(process.env.PATCHPORT_ENDPOINT, { method: 'POST', timeout: 2500, headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${process.env.PATCHPORT_TOKEN}` } }, response => response.resume());
    request.on('error', () => {}); request.on('timeout', () => request.destroy()); request.end(JSON.stringify(event));
    process.stdout.write(`PatchPort - ${event.model?.display_name || 'usage'}\n`);
  } catch { process.stdout.write('PatchPort - usage unknown\n'); }
});
