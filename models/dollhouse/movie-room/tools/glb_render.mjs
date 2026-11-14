// Renders an exported GLB (the file the app converts) in headless Chrome with three's GLTFLoader.
// --app-converter: draw only primitive 0 of every multi-primitive mesh, as ios/scripts/gltf_to_usdz.py does,
// so the picture shows what the app draws.
// Usage (server on :8766): node tools/glb_render.mjs <glb-url-path> <out.png> <px,py,pz> <tx,ty,tz> [fov] [--app-converter] [w h]
import puppeteer from 'puppeteer-core';
const args = process.argv.slice(2), emulate = args.includes('--app-converter'), a = args.filter(s => s !== '--app-converter');
const [url, out, pos, tgt, fov = '50', w = '1400', h = '1000'] = a;
const br = await puppeteer.launch({ executablePath: '/usr/bin/google-chrome', args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--no-sandbox'] });
const p = await br.newPage(); await p.setViewport({ width: +w, height: +h });
await p.goto('http://localhost:8766/src/render.html?state=all_off&w=50&h=50');
const multi = await p.evaluate(async (url) => { const b = await (await fetch(url)).arrayBuffer(), dv = new DataView(b);
  const j = JSON.parse(new TextDecoder().decode(new Uint8Array(b, 20, dv.getUint32(12, true))));
  return j.nodes.filter(n => n.mesh !== undefined && j.meshes[n.mesh].primitives.length > 1).map(n => n.name); }, url);
await p.evaluate(async (url, pos, tgt, fov, w, h, multi, emulate) => {
  const THREE = await import('three'); const { GLTFLoader } = await import('three/addons/loaders/GLTFLoader.js');
  document.body.innerHTML = ''; document.body.style.margin = 0;
  const r = new THREE.WebGLRenderer({ antialias: true }); r.setSize(w, h); r.outputColorSpace = THREE.SRGBColorSpace; r.toneMapping = THREE.ACESFilmicToneMapping; document.body.appendChild(r.domElement);
  const s = new THREE.Scene(); s.background = new THREE.Color('#20242c');
  const g = await new GLTFLoader().loadAsync(url); s.add(g.scene);
  let dropped = 0;
  if (emulate) g.scene.traverse(o => { if (multi.includes(o.name) && o.isGroup) o.children.forEach((c, i) => { if (i > 0 && c.isMesh) { c.visible = false; dropped++; } }); });
  g.scene.traverse(o => { if (o.isLight) o.intensity *= 0.6; });
  s.add(new THREE.HemisphereLight('#fff6e8', '#404050', 1.6)); const d = new THREE.DirectionalLight('#ffffff', 1.4); d.position.set(4, 6, 6); s.add(d);
  const c = new THREE.PerspectiveCamera(fov, w / h, 0.02, 100); c.position.set(...pos); c.lookAt(...tgt);
  r.render(s, c); window.__dropped = dropped;
}, url, pos.split(',').map(Number), tgt.split(',').map(Number), +fov, +w, +h, multi, emulate);
await new Promise(res => setTimeout(res, 300));
await (await p.$('canvas')).screenshot({ path: out });
console.log(`${out}: ${emulate ? `app-converter view, ${await p.evaluate(() => window.__dropped)} extra primitives dropped` : 'full GLB'} (${multi.length} multi-primitive meshes in file)`);
await br.close();
