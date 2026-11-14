// Smoke test for a room viewer + GLB reload through three's GLTFLoader, listing the life node names.
// Usage: node smoke_life.mjs <url-dir under :8766, e.g. room> <glb> <glb_all_on> [screenshot.png]
import puppeteer from 'puppeteer-core';
const [dir, g1, g2, shot] = process.argv.slice(2);
const b = await puppeteer.launch({ executablePath: '/usr/bin/google-chrome', args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist', '--no-sandbox'] });
const p = await b.newPage(); await p.setViewport({ width: 1400, height: 860 });
const errs = []; p.on('pageerror', e => errs.push(String(e))); p.on('console', m => { if (m.type() === 'error' && !/favicon|404/.test(m.text())) errs.push(m.text()); });
await p.goto(`http://localhost:8766/${dir}/viewer.html`, { waitUntil: 'networkidle0', timeout: 120000 });
await new Promise(r => setTimeout(r, 3000));
const clicked = await p.evaluate(async () => { const lbl = [...document.querySelectorAll('.lbl')].find(e => e.textContent === 'Life'); const bs = [...lbl.nextElementSibling.querySelectorAll('button')];
  for (const x of bs) { x.click(); await new Promise(r => setTimeout(r, 400)); } return bs.map(x => x.textContent); });
await new Promise(r => setTimeout(r, 1500));
if (shot) await p.screenshot({ path: shot });
const r = await p.evaluate(async (dir, files) => {
  const { GLTFLoader } = await import('https://unpkg.com/three@0.170.0/examples/jsm/loaders/GLTFLoader.js');
  const RX = /^(pet_cat(_pose_(sleep|sit|stretch|walk_1|walk_2))?(_head|_tail)?$|cat_spot_|prop_[a-z0-9_]+$|(washer|dryer)_drum$|ceiling_fan_blades$|window_sill_low$|[a-z_]*curtain(_[a-z0-9]+)*$|chandelier_bulb_1$|flower_light_head$)/;
  const out = {};
  for (const f of files) { const g = await new GLTFLoader().loadAsync(`/${dir}/${f}`); const life = [], anchors = [], hidden = [];
    g.scene.traverse(o => { if (RX.test(o.name)) life.push(o.name); if (o.name.startsWith('anchor_')) anchors.push(o.name); if (o.userData.defaultVisible === false) hidden.push(o.name); });
    out[f] = { anchors: anchors.length, life, defaultHidden: hidden }; }
  return out;
}, dir, [g1, g2]);
console.log('life buttons:', clicked.join(' | '));
for (const [f, v] of Object.entries(r)) console.log(`${f}: anchors=${v.anchors} life(${v.life.length})=${v.life.join(', ')}\n   defaultHidden=${v.defaultHidden.join(', ')}`);
console.log('errors', errs); await b.close();
