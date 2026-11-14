import puppeteer from 'puppeteer-core';
import fs from 'fs';
const b = await puppeteer.launch({ executablePath: '/usr/bin/google-chrome', args: ['--use-angle=swiftshader','--enable-unsafe-swiftshader','--ignore-gpu-blocklist','--no-sandbox'] });
const p = await b.newPage(); await p.setViewport({ width: 1400, height: 860 });
const errs = []; p.on('pageerror', e => errs.push(String(e))); p.on('console', m => { if (m.type() === 'error') errs.push(m.text()); });
await p.goto('http://localhost:8766/viewer.html', { waitUntil: 'networkidle0', timeout: 90000 });
await new Promise(r => setTimeout(r, 4000));
await p.screenshot({ path: 'renders/viewer_screenshot.png' });
// GLB reload check
const r = await p.evaluate(async () => {
  const { GLTFLoader } = await import('https://unpkg.com/three@0.170.0/examples/jsm/loaders/GLTFLoader.js');
  const out = {};
  for (const f of ['bedroom.glb', 'bedroom_all_on.glb']) {
    const g = await new GLTFLoader().loadAsync('/' + f);
    let meshes = 0, lights = 0, anchors = [];
    g.scene.traverse(o => { if (o.isMesh) meshes++; if (o.isLight) lights++; if (o.name.startsWith('anchor_')) anchors.push(o.name); });
    out[f] = { meshes, lights, anchors };
  }
  return out;
});
console.log(JSON.stringify(r)); console.log('errors', errs);
await b.close();
