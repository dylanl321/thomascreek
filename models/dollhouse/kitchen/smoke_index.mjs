import puppeteer from 'puppeteer-core';
const b = await puppeteer.launch({ executablePath: '/usr/bin/google-chrome', args: ['--use-angle=swiftshader','--enable-unsafe-swiftshader','--ignore-gpu-blocklist','--no-sandbox'] });
const p = await b.newPage(); await p.setViewport({ width: 1400, height: 860 });
const errs = []; p.on('pageerror', e => errs.push(String(e)));
await p.goto('http://localhost:8766/index.html', { waitUntil: 'networkidle0', timeout: 90000 });
await new Promise(r => setTimeout(r, 3000)); await p.screenshot({ path: '/workspace/index_den.png' });
await p.click('button[data-k=kitchen]'); await new Promise(r => setTimeout(r, 9000)); await p.screenshot({ path: '/workspace/index_kitchen.png' });
console.log('errors', errs); await b.close();
