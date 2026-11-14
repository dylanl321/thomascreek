// Loads a GLB (as the app would) via three's GLTFLoader in headless Chrome and prints world-space bounds of nodes matching a regex.
// Usage: node tools/glb_bounds.mjs <url-path-under-:8766> <regex>
import puppeteer from 'puppeteer-core';
const [url, rx] = process.argv.slice(2);
const b = await puppeteer.launch({ executablePath: '/usr/bin/google-chrome', args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--no-sandbox'] });
const p = await b.newPage();
await p.goto('http://localhost:8766/src/render.html?state=all_off&w=50&h=50');
const out = await p.evaluate(async (url, rx) => {
  const THREE = await import('three'); const { GLTFLoader } = await import('three/addons/loaders/GLTFLoader.js');
  const g = await new GLTFLoader().loadAsync(url); g.scene.updateMatrixWorld(true); const R = new RegExp(rx); const res = [];
  g.scene.traverse(o => { if (!R.test(o.name)) return; const bx = new THREE.Box3().setFromObject(o); const wp = new THREE.Vector3(); o.getWorldPosition(wp);
    const chain = []; let q = o.parent; while (q && q !== g.scene) { chain.push(q.name + (q.position.lengthSq() || q.rotation.x || q.rotation.y || q.rotation.z || q.scale.x !== 1 ? `[p=${q.position.toArray().map(v => v.toFixed(2))} r=${[q.rotation.x, q.rotation.y, q.rotation.z].map(v => v.toFixed(2))} s=${q.scale.x.toFixed(2)}]` : '')); q = q.parent; }
    res.push({ n: o.name, type: o.type, min: bx.isEmpty() ? null : bx.min.toArray().map(v => +v.toFixed(3)), max: bx.isEmpty() ? null : bx.max.toArray().map(v => +v.toFixed(3)), wp: wp.toArray().map(v => +v.toFixed(3)), chain: chain.join(' < ') }); });
  return res; }, url, rx);
for (const r of out) console.log(JSON.stringify(r));
await b.close();
