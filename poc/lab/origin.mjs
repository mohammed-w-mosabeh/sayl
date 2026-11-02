// Lab origin on 127.0.0.1 speaking HTTP/2 and HTTP/1.1 over TLS, measuring what the proxy does.
// Usage: node origin.mjs <port> <key.pem> <cert.pem>
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { createSecureServer } from 'node:http2';

const [port, keyPath, certPath] = process.argv.slice(2);
const SIZE = 8 * 1024 * 1024;
const CHUNK = 64 * 1024;
const TICK_MS = 15;
const body = Buffer.alloc(SIZE);
for (let i = 0; i < SIZE; i += 4) body.writeUInt32LE(i >>> 0, i);
const sha256 = createHash('sha256').update(body).digest('hex');

const stats = { sha256, hits: {}, completed: {}, aborted: {}, bytesSent: {} };
const bump = (map, key, by = 1) => { map[key] = (map[key] ?? 0) + by; };

const PAGE = `<!doctype html><title>lab</title>
<a id="range" href="/range.bin">range</a>
<a id="once" href="/once.bin?token=abc">once</a>
<a id="noranges" href="/noranges.bin">noranges</a>
<a id="next" href="/next">next</a>
<form id="post" method="post" action="/post.bin"><button id="postbtn">post</button></form>`;

function slowSend(req, res, key, start, end) {
  let offset = start;
  let closed = false;
  res.on('close', () => {
    closed = true;
    if (offset < end + 1) bump(stats.aborted, key);
  });
  const pump = () => {
    if (closed) return;
    if (offset > end) { bump(stats.completed, key); res.end(); return; }
    const next = Math.min(offset + CHUNK, end + 1);
    bump(stats.bytesSent, key, next - offset);
    res.write(body.subarray(offset, next));
    offset = next;
    setTimeout(pump, TICK_MS);
  };
  pump();
}

function handler(req, res) {
  const url = new URL(req.url, 'https://lab');
  const key = `${req.method} ${url.pathname}`;
  bump(stats.hits, key);
  const attachment = { 'content-type': 'application/octet-stream',
    'content-disposition': `attachment; filename="${url.pathname.slice(1)}"` };
  if (url.pathname === '/stats') {
    res.writeHead(200, { 'content-type': 'application/json' });
    return res.end(JSON.stringify(stats));
  }
  if (url.pathname === '/page' || url.pathname === '/next') {
    res.writeHead(200, { 'content-type': 'text/html' });
    return res.end(url.pathname === '/page' ? PAGE : '<!doctype html><title>next</title>ok');
  }
  if (url.pathname === '/range.bin') {
    const match = /bytes=(\d+)-(\d*)/.exec(req.headers.range ?? '');
    if (match) {
      const start = Number(match[1]);
      const end = match[2] ? Math.min(Number(match[2]), SIZE - 1) : SIZE - 1;
      res.writeHead(206, { ...attachment, 'accept-ranges': 'bytes',
        'content-range': `bytes ${start}-${end}/${SIZE}`, 'content-length': end - start + 1 });
      return slowSend(req, res, key, start, end);
    }
    res.writeHead(200, { ...attachment, 'accept-ranges': 'bytes', 'content-length': SIZE });
    return slowSend(req, res, key, 0, SIZE - 1);
  }
  if (url.pathname === '/once.bin') {
    if (stats.hits[key] > 1) { res.writeHead(410); return res.end('used'); }
    res.writeHead(200, { ...attachment, 'content-length': SIZE });
    return slowSend(req, res, key, 0, SIZE - 1);
  }
  if (url.pathname === '/noranges.bin' || url.pathname === '/post.bin') {
    req.resume();
    res.writeHead(200, { ...attachment, 'content-length': SIZE });
    return slowSend(req, res, key, 0, SIZE - 1);
  }
  res.writeHead(404);
  res.end();
}

createSecureServer(
  { key: readFileSync(keyPath), cert: readFileSync(certPath), allowHTTP1: true }, handler,
).listen(Number(port), '127.0.0.1', () => console.log('origin ready', sha256));
