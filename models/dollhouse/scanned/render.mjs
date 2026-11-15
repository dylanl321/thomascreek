// Render or export scanned rooms. Server root is models/dollhouse on port 8766 (see ../README.md):
//   cd models/dollhouse && npx --yes serve -l 8766 .
//   cd scanned
//   node render.mjs <room>                 -> renders/<room>_dollhouse.png
//   node render.mjs <room>:top             -> any view: dollhouse, close, top
//   node render.mjs export <room>          -> <room>/<room>.glb, devices.json, catalog-entry.json
//   node render.mjs house[:<story>]        -> renders/house_<story>.png (whole floor plan)
//   node render.mjs export house           -> house.glb
import puppeteer from 'puppeteer-core';
import fs from 'fs';
const CHROME = process.env.CHROME || ['/usr/bin/google-chrome', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'].find(p => fs.existsSync(p));
const BASE = process.env.DOLLHOUSE_URL || 'http://localhost:8766';
const args = process.argv.slice(2);
if (!args.length) { console.log('usage: node render.mjs <room>[:view] | house[:story] | export <room>|house'); process.exit(1); }
const browser = await puppeteer.launch({ executablePath: CHROME, headless: 'new',
  args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist', '--no-sandbox'] });
const page = await browser.newPage();
page.on('console', m => { if (m.type() === 'error') console.log('console:', m.text().slice(0, 300)); });
fs.mkdirSync('renders', { recursive: true });
const open = async (target, view, w, h) => {
  const q = target.startsWith('house')
    ? `house=/scanned/house.json${target.includes(':') ? `&story=${target.split(':')[1]}` : ''}`
    : `plan=/scanned/${target}/plan.json`;
  await page.setViewport({ width: w, height: h });
  await page.goto(`${BASE}/scanned/src/render?${q}&view=${view}&w=${w}&h=${h}`);
  await page.waitForFunction('window.__done || window.__err', { timeout: 300000 });
  const err = await page.evaluate('window.__err'); if (err) throw new Error(err);
};
const exportGLB = async () => {
  const b64 = await page.evaluate(async () => { const buf = await app.exportGLB(); let s = ''; const u = new Uint8Array(buf); for (let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode(...u.subarray(i, i + 0x8000)); return btoa(s); });
  return Buffer.from(b64, 'base64');
};
for (let i = 0; i < args.length; i++) {
  if (args[i] === 'export') {
    const target = args[++i];
    await open(target, 'dollhouse', 400, 300);
    const views = await page.evaluate(() => app.views);
    const v = (k) => ({ position: views[k].pos, target: views[k].target, fov: views[k].fov });
    const devices = (id, origin) => JSON.stringify({ room: id, units: 'meters', up: '+Y', origin, presets: { all_off: {}, all_on: {} }, devices: [] }, null, 2) + '\n';
    if (target.startsWith('house')) {
      fs.writeFileSync('house.glb', await exportGLB());
      fs.writeFileSync('house-devices.json', devices('floor_plan', 'house.json frame: the root room\'s back corner (scanner/tools/floorplan.py)'));
      fs.writeFileSync('house-catalog-entry.json', JSON.stringify({ id: 'floor_plan', title: 'Floor Plan', symbol: 'map.fill',
        views: { dollhouse: v('dollhouse'), close: v('close'), top: v('top') } }, null, 2) + '\n');
      console.log('wrote house.glb, house-devices.json, house-catalog-entry.json'); continue;
    }
    const plan = JSON.parse(fs.readFileSync(`${target}/plan.json`, 'utf8'));
    fs.writeFileSync(`${target}/${target}.glb`, await exportGLB());
    const id = target.replace(/-/g, '_');
    fs.writeFileSync(`${target}/devices.json`, devices(id, 'style.md back corner: kept walls on the x=0 and z=0 planes, interior x<0 and z>0 (from scanner/tools/scan2room.py)'));
    fs.writeFileSync(`${target}/catalog-entry.json`, JSON.stringify({ id, title: plan.name, symbol: 'square.split.bottomrightquarter',
      views: { dollhouse: v('dollhouse'), close: v('close'), top: v('top') } }, null, 2) + '\n');
    console.log(`wrote ${target}/${target}.glb, devices.json, catalog-entry.json`);
    continue;
  }
  // 'house' or 'house:-1' is the floor plan (optionally one story); '<room>:<view>' is one room
  const [target, view] = args[i].startsWith('house') ? [args[i], 'dollhouse'] : [args[i].split(':')[0], args[i].split(':')[1] || 'dollhouse'];
  const t0 = Date.now();
  await open(target, view, 1600, 1000);
  const out = `renders/${target.replace(':', '_')}_${view}.png`;
  await page.screenshot({ path: out });
  console.log('wrote', out, Date.now() - t0, 'ms');
}
await browser.close();
