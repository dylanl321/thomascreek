import fs from 'fs';
const common = fs.readFileSync('src/common.js', 'utf8').replace(/^import .*life\.js';$/gm, '').replace(/^export /gm, '');
const kitchen = fs.readFileSync('src/bedroom.js', 'utf8').replace(/^import .*$/gm, '').replace(/^export /gm, '');
const life = fs.readFileSync('src/life.js', 'utf8').replace(/^import .*$/gm, '').replace(/^export /gm, '');
const lifeViewer = fs.readFileSync('src/life_viewer.js', 'utf8').replace(/^export /gm, '');
const scene = common + '\n' + life + '\n' + lifeViewer + '\n' + kitchen;
const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Basement Bedroom · Smart Home Dollhouse</title>
<style>
  :root{--ink:#3a2a1e;--paper:#f6f1e9;--gold:#c9a046;--warm:#ffc77d}
  html,body{margin:0;height:100%;overflow:hidden;background:#e6ded2;font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--ink)}
  #stage{position:fixed;inset:0}
  #panel{position:fixed;top:16px;left:16px;width:260px;background:rgba(246,241,233,.92);backdrop-filter:blur(8px);border-radius:16px;padding:14px 14px 10px;box-shadow:0 8px 30px rgba(60,40,20,.18)}
  h1{font-size:15px;margin:0 0 2px;letter-spacing:.02em}
  .sub{font-size:11px;opacity:.6;margin-bottom:10px}
  .row{display:flex;align-items:center;justify-content:space-between;padding:6px 2px;border-top:1px solid rgba(58,42,30,.08);font-size:13px;cursor:pointer;user-select:none}
  .row .t{font-size:10px;opacity:.5;margin-left:6px;text-transform:uppercase;letter-spacing:.06em}
  .sw{width:34px;height:20px;border-radius:10px;background:#cbbfae;position:relative;transition:.2s;flex:none}
  .sw::after{content:"";position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;background:#fff;transition:.2s;box-shadow:0 1px 3px rgba(0,0,0,.25)}
  .on .sw{background:var(--gold)} .on .sw::after{left:16px}
  .btns{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
  button{border:0;border-radius:999px;padding:6px 10px;font-size:12px;background:#e7dccb;color:var(--ink);cursor:pointer}
  button:hover{background:#dccdb6} button.active{background:var(--ink);color:#fff}
  .lbl{font-size:10px;text-transform:uppercase;letter-spacing:.08em;opacity:.55;margin-top:8px}
  .hs{position:fixed;width:26px;height:26px;margin:-13px 0 0 -13px;border-radius:50%;border:2px solid #fff;background:rgba(58,42,30,.55);box-shadow:0 2px 8px rgba(0,0,0,.35);cursor:pointer;display:grid;place-items:center;font-size:13px;transition:background .2s,transform .15s}
  .hs:hover{transform:scale(1.15)} .hs.on{background:var(--warm);box-shadow:0 0 16px 4px rgba(255,199,125,.8)}
  .hs .tip{position:absolute;bottom:30px;white-space:nowrap;background:rgba(30,22,16,.85);color:#fff;font-size:11px;padding:3px 7px;border-radius:6px;opacity:0;pointer-events:none;transition:.15s}
  .hs:hover .tip{opacity:1}
  #hint{position:fixed;bottom:12px;left:50%;transform:translateX(-50%);font-size:11px;opacity:.55}
</style>
<script type="importmap">{"imports":{"three":"https://unpkg.com/three@0.170.0/build/three.module.js","three/addons/":"https://unpkg.com/three@0.170.0/examples/jsm/"}}</script>
</head><body>
<div id="stage"></div>
<div id="panel"><h1>Basement bedroom</h1><div class="sub">Room 04 · dollhouse view</div>
  <div class="lbl">Scenes</div><div class="btns" id="presets"></div>
  <div class="lbl">Camera</div><div class="btns" id="views"></div>
  <div class="lbl">Devices</div><div id="list"></div></div>
<div id="hint">Drag to orbit · scroll to zoom · click a dot or a switch to toggle</div>
<script type="module">
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
${scene}
// ------------------------------------------------------------ viewer
const stage = document.getElementById('stage');
const app = createBedroom(stage, { width: innerWidth, height: innerHeight, pixelRatio: Math.min(devicePixelRatio, 2) });
const controls = new OrbitControls(app.camera, app.renderer.domElement);
controls.enableDamping = true; controls.maxPolarAngle = Math.PI * 0.49; controls.minDistance = 2; controls.maxDistance = 32;
const goView = (v) => { app.setView(v); controls.target.copy(app.camera.userData.target); controls.update(); };
goView('dollhouse');
const icons = { light: '💡', fan: '🌀' };
const list = document.getElementById('list'), rows = {}, dots = {};
const refresh = () => Object.values(app.devices).forEach(d => { rows[d.name].classList.toggle('on', d.on); dots[d.name].classList.toggle('on', d.on); });
const toggle = (n) => { app.setDevice(n, !app.devices[n].on); refresh(); setActive('presets', null); };
Object.values(app.devices).forEach(d => {
  const r = document.createElement('div'); r.className = 'row';
  r.innerHTML = '<span>' + icons[d.type] + ' ' + d.label + '<span class="t">' + d.type + '</span></span><span class="sw"></span>';
  r.onclick = () => toggle(d.name); list.appendChild(r); rows[d.name] = r;
  const h = document.createElement('div'); h.className = 'hs'; h.innerHTML = icons[d.type] + '<span class="tip">' + d.label + '</span>';
  h.onclick = () => toggle(d.name); document.body.appendChild(h); dots[d.name] = h;
});
const setActive = (id, key) => document.querySelectorAll('#' + id + ' button').forEach(b => b.classList.toggle('active', b.dataset.k === key));
const mk = (id, entries, fn) => entries.forEach(([k, label]) => { const b = document.createElement('button'); b.textContent = label; b.dataset.k = k; b.onclick = () => { fn(k); setActive(id, k); }; document.getElementById(id).appendChild(b); });
mk('presets', [['all_off', 'All off'], ['all_on', 'All on'], ['bedtime', 'Bedtime'], ['reading', 'Reading']], k => { app.applyPreset(k); refresh(); });
mk('views', [['dollhouse', 'Dollhouse'], ['hero', 'Closer'], ['photo', 'Photo angle'], ['sconces', 'Bed wall'], ['sconce_close', 'Sconce'], ['ceiling', 'Fan + skylight'], ['dresser', 'Dresser'], ['top', 'Top'], ['life_props', 'Lived-in'], ['life_bedside', 'Bedside']], goView);
app.applyPreset('reading'); refresh(); setActive('presets', 'reading'); setActive('views', 'dollhouse');
addEventListener('resize', () => app.resize(innerWidth, innerHeight));
const lifeTick = setupLife(app, { spin: { ceiling_fan_blades: ['ceiling_fan', 9] } });
const v = new THREE.Vector3();
const blades = app.scene.getObjectByName('ceiling_fan_blades');
const clock = new THREE.Clock();
(function loop() {
  requestAnimationFrame(loop); controls.update(); lifeTick();
    // fan spin now handled by setupLife (fan_blades pivot)
  Object.values(app.devices).forEach(d => { v.copy(d.anchor).project(app.camera); const el = dots[d.name];
    el.style.display = v.z < 1 ? 'grid' : 'none'; el.style.left = ((v.x + 1) / 2 * innerWidth) + 'px'; el.style.top = ((1 - v.y) / 2 * innerHeight) + 'px'; });
  app.render();
})();
</script></body></html>`;
fs.writeFileSync('viewer.html', html); console.log('viewer.html', html.length);
