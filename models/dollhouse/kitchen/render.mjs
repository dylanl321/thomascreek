import puppeteer from 'puppeteer-core';
import fs from 'fs';
import { lifeInfo } from './life_collect.mjs';
const jobs = process.argv.slice(2).length ? process.argv.slice(2) : ['all_off:dollhouse'];
const browser = await puppeteer.launch({ executablePath: '/usr/bin/google-chrome', headless: 'new',
  args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist', '--no-sandbox'] });
const page = await browser.newPage();
page.on('console', m => { if (m.type() === 'error' || m.type() === 'warning') console.log('console:', m.text().slice(0, 300)); });
fs.mkdirSync('renders', { recursive: true });
for (const job of jobs) {
  const [state, view, w = 1600, h = 1000] = job.split(':');
  if (state === 'export') {
    await page.goto(`http://localhost:8766/src/render.html?state=all_off&w=400&h=300`);
    await page.waitForFunction('window.__done || window.__err', { timeout: 120000 });
    for (const preset of ['all_off', 'all_on']) {
      const b64 = await page.evaluate(async (preset) => { app.applyPreset(preset); const buf = await app.exportGLB(); let s = ''; const u = new Uint8Array(buf); for (let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode(...u.subarray(i, i + 0x8000)); return btoa(s); }, preset);
      const name = preset === 'all_off' ? 'kitchen.glb' : 'kitchen_all_on.glb';
      fs.writeFileSync(name, Buffer.from(b64, 'base64')); console.log('wrote', name);
    }
    const info = await page.evaluate(() => Object.values(app.devices).map(d => {
      const names = (arr) => [...new Set(arr)];
      const meshNodes = []; d.group && d.group.traverse(o => { if (o.isMesh && !o.name.startsWith('glow_')) meshNodes.push(o.name); });
      return { name: d.name, label: d.label, type: d.type, room: 'kitchen', anchor_node: 'anchor_' + d.name,
        position_m: d.anchor.toArray().map(v => +v.toFixed(3)), group_node: d.group ? d.group.name : null,
        light_nodes: d.lights.map(l => l.light.name), light_on_intensity: d.lights.map(l => l.on),
        glow_decals: d.decals.map(m => m.name), mesh_nodes: names(meshNodes),
        on_state: { emissive: d.emissive.map(e => e.on), shows: d.show.map(m => m.name) } };
    }));
    const presets = await page.evaluate(async () => (await import('./kitchen.js')).PRESETS);
    fs.writeFileSync('devices.json', JSON.stringify({ room: 'kitchen', units: 'meters', up: '+Y', origin: 'corner of sink wall (x=0 plane, long) and cooktop wall (z=0 plane) at floor level; interior x<0, z>0', presets, devices: info, life: await page.evaluate(lifeInfo) }, null, 2));
    console.log('wrote devices.json');
    continue;
  }
  await page.setViewport({ width: +w, height: +h });
  const t0 = Date.now();
  await page.goto(`http://localhost:8766/src/render.html?state=${state}&view=${view}&w=${w}&h=${h}`);
  await page.waitForFunction('window.__done || window.__err', { timeout: 300000 });
  const err = await page.evaluate('window.__err'); if (err) { console.log('ERR', err); continue; }
  const out = `renders/${view}_${state}.png`;
  await page.screenshot({ path: out });
  console.log('wrote', out, Date.now() - t0, 'ms');
}
await browser.close();
