// Life kit, shared by all four dollhouse rooms: cat poses, lived-in props, animation pivots and empties.
// Naming contract (see /workspace/handoff/LIFE_NOTE.md):
//   prop groups      prop_<name>              (child meshes are named pm_<name>_<part>, so only the group matches /^prop_/)
//   steam empties    steam_spot_<name>        (Object3D at the point where steam should start)
//   cat poses        pet_cat_pose_<pose>      (+ pivots pet_cat_pose_<pose>_head and pet_cat_pose_<pose>_tail)
//   cat spots        cat_spot_<where>         (Object3D: position = where the pose origin goes, rotation.y = facing)
// Cat local frame: origin on the surface under the cat's middle, the cat faces local +X, +Y up.
import * as THREE from 'three';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';

export function makeLifeKit() {
  const cache = {};
  const lstd = (color, rough = 0.8, metal = 0, extra = {}) => { const k = color + rough + metal + JSON.stringify(Object.keys(extra)); if (!extra.map && cache[k]) return cache[k]; const m = new THREE.MeshStandardMaterial({ color, roughness: rough, metalness: metal, ...extra }); if (!extra.map) cache[k] = m; return m; };
  const LG = (name, parent, x = 0, y = 0, z = 0, ry = 0) => { const g = new THREE.Group(); g.name = name; g.position.set(x, y, z); g.rotation.y = ry; parent.add(g); return g; };
  const LM = (name, geo, mat, parent, x = 0, y = 0, z = 0, shadow = true) => { const m = new THREE.Mesh(geo, mat); m.name = name; m.position.set(x, y, z); m.castShadow = shadow; m.receiveShadow = true; parent.add(m); return m; };
  const LB = (name, parent, mat, x0, x1, y0, y1, z0, z1, r = 0) => { const w = x1 - x0, h = y1 - y0, d = z1 - z0, rr = Math.min(r, w / 2 - 1e-4, h / 2 - 1e-4, d / 2 - 1e-4);
    return LM(name, rr > 0.002 ? new RoundedBoxGeometry(w, h, d, 2, rr) : new THREE.BoxGeometry(w, h, d), mat, parent, (x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2); };
  const LS = (name, parent, mat, r, x, y, z, sx = 1, sy = 1, sz = 1) => { const m = LM(name, new THREE.SphereGeometry(r, 18, 12), mat, parent, x, y, z); m.scale.set(sx, sy, sz); return m; };
  const LC = (name, parent, mat, rt, rb, h, x, y, z, seg = 20, open = false) => LM(name, new THREE.CylinderGeometry(rt, rb, h, seg, 1, open), mat, parent, x, y, z);
  const limb = (name, parent, mat, a, b, r) => { const A = new THREE.Vector3(...a), Bv = new THREE.Vector3(...b), d = Bv.clone().sub(A);
    const m = LM(name, new THREE.CylinderGeometry(r, r * 0.9, d.length(), 10), mat, parent, (A.x + Bv.x) / 2, (A.y + Bv.y) / 2, (A.z + Bv.z) / 2);
    m.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), d.normalize()); return m; };
  const empty = (name, parent, x, y, z, ry = 0) => { const o = new THREE.Object3D(); o.name = name; o.position.set(x, y, z); o.rotation.y = ry; parent.add(o); return o; };
  const tex = (w, h, draw) => { const c = document.createElement('canvas'); c.width = w; c.height = h; draw(c.getContext('2d'), w, h); const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 4; return t; };
  const plaid = (base, a, b) => tex(128, 128, (g, w, h) => { g.fillStyle = base; g.fillRect(0, 0, w, h); g.globalAlpha = 0.55; g.fillStyle = a;
    for (let i = 0; i < 4; i++) { g.fillRect(i * 32 + 4, 0, 10, h); g.fillRect(0, i * 32 + 4, w, 10); } g.globalAlpha = 0.6; g.fillStyle = b; for (let i = 0; i < 4; i++) { g.fillRect(i * 32 + 22, 0, 3, h); g.fillRect(0, i * 32 + 22, w, 3); } });
  const stripes = (a, b, n = 8, vertical = true) => tex(64, 64, (g, w, h) => { for (let i = 0; i < n; i++) { g.fillStyle = i % 2 ? b : a; if (vertical) g.fillRect(i * w / n, 0, w / n + 1, h); else g.fillRect(0, i * h / n, w, h / n + 1); } });
  const knit = (base) => tex(128, 128, (g, w, h) => { g.fillStyle = base; g.fillRect(0, 0, w, h); g.strokeStyle = 'rgba(0,0,0,0.13)'; g.lineWidth = 2;
    for (let x = 0; x < w; x += 12) for (let y = 0; y < h; y += 10) { g.beginPath(); g.moveTo(x, y); g.lineTo(x + 6, y + 10); g.lineTo(x + 12, y); g.stroke(); } });
  // cloth sheet: a plane draped by a height function f(u,v) -> local offset along +Y (u,v in [0,1])
  const cloth = (name, parent, mat, w, d, f, segs = 24) => { const geo = new THREE.PlaneGeometry(w, d, segs, segs); geo.rotateX(-Math.PI / 2); const p = geo.attributes.position;
    for (let i = 0; i < p.count; i++) { const u = p.getX(i) / w + 0.5, v = p.getZ(i) / d + 0.5; const [dx, dy, dz] = f(u, v); p.setXYZ(i, p.getX(i) + dx, p.getY(i) + dy, p.getZ(i) + dz); }
    geo.computeVertexNormals(); return LM(name, geo, mat, parent); };

  // ---------------- props (group named prop_*, origin on the supporting surface)
  const P = {};
  P.mug = (name, parent, x, y, z, color, steamName, ry = 0) => { const g = LG(name, parent, x, y, z, ry), s = name.replace(/^prop_/, 'pm_');
    LC(`${s}_body`, g, lstd(color, 0.35), 0.043, 0.038, 0.095, 0, 0.0475, 0, 24);
    LC(`${s}_coffee`, g, lstd('#3b2416', 0.2), 0.039, 0.039, 0.004, 0, 0.083, 0, 24).castShadow = false;
    const h = LM(`${s}_handle`, new THREE.TorusGeometry(0.026, 0.008, 8, 18, Math.PI * 1.2), lstd(color, 0.35), g, 0.043, 0.05, 0); h.rotation.z = -Math.PI * 0.6;
    if (steamName) empty(steamName, g, 0, 0.12, 0); return g; };
  P.book = (name, parent, x, y, z, ry, cover = '#7d2e2a', open = true) => { const g = LG(name, parent, x, y, z, ry), s = name.replace(/^prop_/, 'pm_'), paper = lstd('#f3ecdc', 0.9);
    if (open) { LB(`${s}_cover`, g, lstd(cover, 0.7), -0.16, 0.16, 0, 0.006, -0.115, 0.115, 0.002);
      for (const [k, sgn] of [['left', -1], ['right', 1]]) { const pg = LB(`${s}_pages_${k}`, g, paper, -0.075, 0.075, -0.008, 0.008, -0.105, 0.105, 0.004); pg.position.set(sgn * 0.077, 0.016, 0); pg.rotation.z = -sgn * 0.09; }
      LB(`${s}_ribbon`, g, lstd('#c9a046', 0.6), 0.0, 0.006, 0.006, 0.02, 0.1, 0.16).castShadow = false; }
    else { LB(`${s}_cover`, g, lstd(cover, 0.7), -0.075, 0.075, 0, 0.032, -0.11, 0.11, 0.004); LB(`${s}_pages`, g, paper, -0.07, 0.077, 0.004, 0.028, -0.105, 0.105); }
    return g; };
  P.remote = (name, parent, x, y, z, ry) => { const g = LG(name, parent, x, y, z, ry), s = name.replace(/^prop_/, 'pm_');
    LB(`${s}_body`, g, lstd('#222326', 0.45), -0.025, 0.025, 0, 0.018, -0.085, 0.085, 0.008);
    LC(`${s}_power`, g, lstd('#c8352e', 0.4), 0.007, 0.007, 0.004, 0, 0.019, -0.066, 12);
    for (let i = 0; i < 6; i++) LC(`${s}_btn_${i}`, g, lstd('#8c8f96', 0.5), 0.005, 0.005, 0.003, (i % 2 ? 0.01 : -0.01), 0.019, -0.03 + Math.floor(i / 2) * 0.022, 8);
    return g; };
  P.slippers = (name, parent, x, y, z, ry, color = '#c98c8c') => { const g = LG(name, parent, x, y, z, ry), s = name.replace(/^prop_/, 'pm_'), fur = lstd(color, 1), sole = lstd('#efe6d6', 0.9);
    [[-0.065, 0.1, 0.03], [0.07, -0.02, -0.22]].forEach(([dx, dz, rot], i) => { const sg = LG(`${s}_${i ? 'right' : 'left'}`, g, dx, 0, dz, rot);
      LB(`${s}_${i ? 'right' : 'left'}_sole`, sg, sole, -0.05, 0.05, 0, 0.022, -0.12, 0.12, 0.012);
      LS(`${s}_${i ? 'right' : 'left'}_toe`, sg, fur, 0.055, 0, 0.022, 0.06, 0.95, 0.75, 1.15);
      LB(`${s}_${i ? 'right' : 'left'}_heel`, sg, fur, -0.048, 0.048, 0.02, 0.05, -0.12, -0.08, 0.012); });
    return g; };
  P.fruitBowl = (name, parent, x, y, z) => { const g = LG(name, parent, x, y, z), s = name.replace(/^prop_/, 'pm_');
    const bowl = LM(`${s}_bowl`, new THREE.SphereGeometry(0.15, 28, 10, 0, Math.PI * 2, Math.PI * 0.6, Math.PI * 0.4), lstd('#e9e1d2', 0.35, 0, { side: THREE.DoubleSide }), g, 0, 0.15, 0); bowl.scale.y = 0.9;
    LC(`${s}_foot`, g, lstd('#e9e1d2', 0.35), 0.06, 0.07, 0.015, 0, 0.0075, 0, 20);
    [['#f08a24', 0.045, -0.04, 0.085, 0.02], ['#f08a24', 0.043, 0.05, 0.09, -0.04], ['#c0262c', 0.042, 0.01, 0.1, 0.06], ['#7fb03d', 0.04, -0.06, 0.1, -0.05], ['#c0262c', 0.04, 0.07, 0.11, 0.05], ['#f08a24', 0.04, 0.0, 0.14, -0.005]]
      .forEach(([c, r, fx, fy, fz], i) => LS(`${s}_fruit_${i + 1}`, g, lstd(c, 0.45), r, fx, fy, fz));
    const ban = LM(`${s}_banana`, new THREE.TorusGeometry(0.09, 0.017, 8, 20, Math.PI * 0.75), lstd('#f2cf3e', 0.5), g, -0.01, 0.16, -0.01); ban.rotation.set(-1.2, 0, 0.6);
    return g; };
  // big turned-wood catch-all bowl (keys, sunglasses, a wallet); origin on the counter under the bowl's centre
  P.catchAllBowl = (name, parent, x, y, z, ry = 0) => { const g = LG(name, parent, x, y, z, ry), s = name.replace(/^prop_/, 'pm_');
    const wood = lstd('#ffffff', 0.55, 0, { map: tex(256, 64, (c, w, h) => { c.fillStyle = '#8a5530'; c.fillRect(0, 0, w, h); for (let i = 0; i < 14; i++) { c.fillStyle = i % 2 ? 'rgba(60,30,12,0.22)' : 'rgba(190,130,80,0.18)'; c.fillRect(0, (i * h) / 14 + Math.sin(i) * 1.5, w, 2); } }), side: THREE.DoubleSide });
    const prof = [[0, 0.0], [0.08, 0.0], [0.085, 0.012], [0.16, 0.035], [0.205, 0.075], [0.222, 0.11], [0.214, 0.112], [0.196, 0.08], [0.15, 0.045], [0.0, 0.03]].map(([r, h]) => new THREE.Vector2(r, h));
    LM(`${s}_bowl`, new THREE.LatheGeometry(prof, 40), wood, g, 0, 0, 0);
    const brass = lstd('#c9a046', 0.3, 0.9);
    LM(`${s}_key_ring`, new THREE.TorusGeometry(0.022, 0.003, 6, 20), brass, g, -0.05, 0.05, 0.02).rotation.x = Math.PI / 2 - 0.2;
    LB(`${s}_key_1`, g, brass, -0.03, 0.025, 0.047, 0.051, 0.012, 0.024, 0.001).rotation.y = 0.4;
    LB(`${s}_key_fob`, g, lstd('#1f2a44', 0.5), -0.1, -0.06, 0.044, 0.056, 0.0, 0.03, 0.005);
    LB(`${s}_wallet`, g, lstd('#5a3420', 0.6), 0.0, 0.11, 0.035, 0.055, -0.09, -0.02, 0.006).rotation.y = -0.25;
    const sg = LG(`${s}_sunglasses`, g, 0.04, 0.06, 0.07, 0.5); const lens = lstd('#16181c', 0.15, 0.3);
    LM(`${s}_sunglasses_lens_l`, new THREE.CylinderGeometry(0.022, 0.022, 0.004, 18), lens, sg, -0.026, 0, 0).rotation.x = 0.25;
    LM(`${s}_sunglasses_lens_r`, new THREE.CylinderGeometry(0.022, 0.022, 0.004, 18), lens, sg, 0.026, 0, 0).rotation.x = 0.25;
    LB(`${s}_sunglasses_bridge`, sg, lens, -0.006, 0.006, 0.0, 0.004, -0.002, 0.002);
    return g; };
  // holiday table runner (requested exception to the no-seasonal rule): red linen, green bands, gold pinstripes, white snowflakes + little trees,
  // tasselled ends that drape over the counter edge. Origin at the runner's centre on the counter; long axis = local Z.
  P.tableRunner = (name, parent, x, y, z, ry, len = 2.3, wid = 0.34, drop = 0.1) => { const g = LG(name, parent, x, y, z, ry), s = name.replace(/^prop_/, 'pm_');
    const t = tex(128, 512, (c, w, h) => { c.fillStyle = '#a3212b'; c.fillRect(0, 0, w, h);
      c.fillStyle = '#1f5a3a'; c.fillRect(0, 0, 16, h); c.fillRect(w - 16, 0, 16, h);
      c.fillStyle = '#d8b04a'; c.fillRect(18, 0, 3, h); c.fillRect(w - 21, 0, 3, h);
      for (let i = 0; i < 6; i++) { const cy = 40 + i * 86;
        if (i % 2) { c.fillStyle = '#2e6b45'; c.beginPath(); c.moveTo(w / 2, cy - 22); c.lineTo(w / 2 + 16, cy + 14); c.lineTo(w / 2 - 16, cy + 14); c.closePath(); c.fill(); c.fillStyle = '#6b3f22'; c.fillRect(w / 2 - 3, cy + 14, 6, 7); c.fillStyle = '#e8c55a'; c.beginPath(); c.arc(w / 2, cy - 23, 3, 0, 7); c.fill(); }
        else { c.strokeStyle = '#f4efe6'; c.lineWidth = 3; for (let k = 0; k < 3; k++) { const a = k * Math.PI / 3; c.beginPath(); c.moveTo(w / 2 - Math.cos(a) * 18, cy - Math.sin(a) * 18); c.lineTo(w / 2 + Math.cos(a) * 18, cy + Math.sin(a) * 18); c.stroke(); } } } });
    t.wrapS = t.wrapT = THREE.RepeatWrapping; t.repeat.set(1, len / 1.15);
    const cloth = lstd('#ffffff', 0.95, 0, { map: t, side: THREE.DoubleSide });
    LB(`${s}_top`, g, cloth, -wid / 2, wid / 2, 0, 0.003, -len / 2, len / 2).castShadow = false;
    const gold = lstd('#d8b04a', 0.5, 0.2);
    for (const [k, sg] of [['a', -1], ['b', 1]]) {
      const end = LB(`${s}_end_${k}`, g, cloth, -wid / 2, wid / 2, -drop, 0.003, sg * len / 2, sg * len / 2 + sg * 0.004); end.castShadow = false;
      for (let i = 0; i < 5; i++) LC(`${s}_tassel_${k}_${i + 1}`, g, gold, 0.005, 0.009, 0.035, -wid / 2 + 0.03 + i * (wid - 0.06) / 4, -drop - 0.017, sg * (len / 2 + 0.002), 8);
    }
    return g; };
  P.mailStack = (name, parent, x, y, z, ry) => { const g = LG(name, parent, x, y, z, ry), s = name.replace(/^prop_/, 'pm_');
    LB(`${s}_magazine`, g, lstd('#ffffff', 0.6, 0, { map: tex(64, 84, (c, w, h) => { c.fillStyle = '#e7d6b8'; c.fillRect(0, 0, w, h); c.fillStyle = '#2f6f8f'; c.fillRect(0, 0, w, 20); c.fillStyle = '#c0262c'; c.fillRect(8, 30, 48, 34); }) }), -0.105, 0.105, 0, 0.008, -0.14, 0.14);
    [['#f6f3ec', 0.1, 0.12], ['#d8b98a', -0.2, 0.1], ['#f6f3ec', 0.35, 0.11], ['#cfe0f0', -0.05, 0.095]].forEach(([c, rot, w], i) => { const e = LB(`${s}_envelope_${i + 1}`, g, lstd(c, 0.85), -w, w, 0.008 + i * 0.004, 0.011 + i * 0.004, -0.055, 0.055); e.rotation.y = rot; e.position.x += (i - 1.5) * 0.012; });
    LB(`${s}_stamp`, g, lstd('#c0262c', 0.7), 0.06, 0.08, 0.0245, 0.0255, -0.04, -0.02).castShadow = false;
    return g; };
  P.sinkDishes = (name, parent, x, y, z, ry) => { const g = LG(name, parent, x, y, z, ry), s = name.replace(/^prop_/, 'pm_'), plate = lstd('#f4f1ea', 0.3), blue = lstd('#5d7fa3', 0.35);
    for (let i = 0; i < 3; i++) { const p = LC(`${s}_plate_${i + 1}`, g, plate, 0.11, 0.085, 0.014, -0.08, 0.008 + i * 0.016, 0.02, 28); p.rotation.z = 0.05 * i; }
    LM(`${s}_bowl`, new THREE.SphereGeometry(0.075, 20, 8, 0, Math.PI * 2, Math.PI / 2, Math.PI / 2), lstd('#5d7fa3', 0.35, 0, { side: THREE.DoubleSide }), g, 0.1, 0.08, -0.05).rotation.x = Math.PI + 0.4;
    LC(`${s}_cup`, g, blue, 0.035, 0.03, 0.08, 0.1, 0.04, 0.09, 18);
    LC(`${s}_glass`, g, new THREE.MeshPhysicalMaterial({ color: '#dfeff5', roughness: 0.05, transparent: true, opacity: 0.45 }), 0.03, 0.026, 0.11, -0.12, 0.1, -0.08, 18).rotation.z = 1.1;
    const spoon = LB(`${s}_spoon`, g, lstd('#c9ccd0', 0.25, 0.9), -0.006, 0.006, 0.05, 0.056, -0.09, 0.09); spoon.rotation.set(0.3, 0.6, 0); spoon.position.set(-0.08, 0.07, 0.02);
    for (let i = 0; i < 5; i++) LS(`${s}_suds_${i + 1}`, g, lstd('#ffffff', 0.2), 0.02 + i * 0.004, -0.02 + i * 0.03, 0.01, -0.12 + i * 0.05, 1.4, 0.6, 1.4);
    return g; };
  P.towel = (name, parent, x, y, z, ry, w = 0.26, drop = 0.3) => { const g = LG(name, parent, x, y, z, ry), s = name.replace(/^prop_/, 'pm_'), mat = lstd('#ffffff', 0.95, 0, { map: stripes('#f3eee4', '#b63a35', 10, false), side: THREE.DoubleSide });
    // origin = bar the towel hangs over; +X = toward the room (front layer)
    LB(`${s}_front`, g, mat, 0.012, 0.022, -drop, 0.01, -w / 2, w / 2, 0.004); LB(`${s}_back`, g, mat, -0.022, -0.012, -drop * 0.7, 0.01, -w / 2, w / 2, 0.004);
    LM(`${s}_fold`, new THREE.CylinderGeometry(0.022, 0.022, w, 12, 1, true, Math.PI / 2, Math.PI), mat, g, 0, 0.01, 0).rotation.set(Math.PI / 2, 0, 0);
    return g; };
  P.kettle = (name, parent, x, y, z, ry, steamName) => { const g = LG(name, parent, x, y, z, ry), s = name.replace(/^prop_/, 'pm_'), red = lstd('#c43a2f', 0.3, 0.1), black = lstd('#1d1d1d', 0.5);
    const body = LS(`${s}_body`, g, red, 0.1, 0, 0.085, 0, 1, 0.82, 1); LC(`${s}_base`, g, red, 0.085, 0.08, 0.025, 0, 0.0125, 0, 24);
    LC(`${s}_lid`, g, red, 0.055, 0.06, 0.02, 0, 0.165, 0, 24); LS(`${s}_knob`, g, black, 0.018, 0, 0.185, 0);
    const handle = LM(`${s}_handle`, new THREE.TorusGeometry(0.07, 0.011, 8, 20, Math.PI), black, g, 0, 0.165, 0); handle.rotation.y = Math.PI / 2;
    const spout = new THREE.CatmullRomCurve3([[0.07, 0.07, 0], [0.12, 0.1, 0], [0.155, 0.15, 0]].map(p => new THREE.Vector3(...p)));
    LM(`${s}_spout`, new THREE.TubeGeometry(spout, 10, 0.014, 8), red, g);
    if (steamName) empty(steamName, g, 0.16, 0.17, 0); void body; return g; };
  P.popcornBowl = (name, parent, x, y, z, seed = 1) => { const g = LG(name, parent, x, y, z), s = name.replace(/^prop_/, 'pm_');
    LC(`${s}_bowl`, g, lstd('#ffffff', 0.5, 0, { map: stripes('#ffffff', '#c8191f', 12), side: THREE.DoubleSide }), 0.11, 0.075, 0.1, 0, 0.05, 0, 28, true);
    LC(`${s}_bottom`, g, lstd('#c8191f', 0.5), 0.075, 0.075, 0.006, 0, 0.003, 0, 24);
    let r = seed * 9301 + 49297; const rnd = () => (r = (r * 9301 + 49297) % 233280) / 233280;
    const corn = lstd('#fbeab3', 0.85), butter = lstd('#f2c75c', 0.8);
    for (let i = 0; i < 34; i++) { const a = rnd() * 6.28, d = Math.sqrt(rnd()) * 0.095; LS(`${s}_kernel_${i}`, g, i % 5 ? corn : butter, 0.017 + rnd() * 0.008, Math.cos(a) * d, 0.09 + rnd() * 0.04 * (1 - d / 0.1), Math.sin(a) * d).castShadow = false; }
    return g; };
  P.sodaCup = (name, parent, x, y, z, color = '#c8191f') => { const g = LG(name, parent, x, y, z), s = name.replace(/^prop_/, 'pm_');
    LC(`${s}_cup`, g, lstd('#ffffff', 0.45, 0, { map: tex(64, 32, (c, w, h) => { c.fillStyle = '#ffffff'; c.fillRect(0, 0, w, h); c.fillStyle = color; c.fillRect(0, 8, w, 16); c.fillStyle = '#ffffff'; c.beginPath(); c.arc(w / 2, 16, 6, 0, 7); c.fill(); }) }), 0.045, 0.034, 0.16, 0, 0.08, 0, 24);
    LC(`${s}_lid`, g, lstd('#f2f2f2', 0.3), 0.048, 0.048, 0.012, 0, 0.166, 0, 24);
    const straw = LC(`${s}_straw`, g, lstd('#ffffff', 0.4, 0, { map: stripes('#ffffff', color, 6, false) }), 0.005, 0.005, 0.12, 0.01, 0.22, 0, 8); straw.rotation.z = -0.15;
    return g; };
  P.shoes = (name, parent, x, y, z, ry, color = '#3f6fb0') => { const g = LG(name, parent, x, y, z, ry), s = name.replace(/^prop_/, 'pm_'), sole = lstd('#f4f2ee', 0.6), up = lstd(color, 0.75), lace = lstd('#ffffff', 0.7);
    [[-0.07, 0, 0.0], [0.08, 0.06, -0.5]].forEach(([dx, dz, rot], i) => { const k = i ? 'right' : 'left', sg = LG(`${s}_${k}`, g, dx, 0, dz, rot);
      if (i) sg.rotation.z = 1.35, sg.position.y = 0.05;      // second shoe kicked over on its side
      LB(`${s}_${k}_sole`, sg, sole, -0.048, 0.048, 0, 0.028, -0.135, 0.135, 0.014);
      LB(`${s}_${k}_upper`, sg, up, -0.044, 0.044, 0.026, 0.09, -0.125, 0.07, 0.03);
      LS(`${s}_${k}_toe`, sg, up, 0.045, 0, 0.04, 0.08, 1, 0.8, 1.2);
      for (let j = 0; j < 3; j++) LB(`${s}_${k}_lace_${j}`, sg, lace, -0.03, 0.03, 0.088 - j * 0.008, 0.094 - j * 0.008, 0.0 + j * 0.025, 0.008 + j * 0.025).castShadow = false; });
    return g; };
  P.laundryBasket = (name, parent, x, y, z, ry) => { const g = LG(name, parent, x, y, z, ry), s = name.replace(/^prop_/, 'pm_');
    const wick = lstd('#ffffff', 0.9, 0, { map: tex(128, 64, (c, w, h) => { c.fillStyle = '#c9a774'; c.fillRect(0, 0, w, h); c.strokeStyle = '#8f6c40'; c.lineWidth = 3; for (let x = 0; x < w; x += 8) { c.beginPath(); c.moveTo(x, 0); c.lineTo(x, h); c.stroke(); } c.strokeStyle = 'rgba(255,240,210,0.5)'; for (let y = 4; y < h; y += 8) { c.beginPath(); c.moveTo(0, y); c.lineTo(w, y); c.stroke(); } }), side: THREE.DoubleSide });
    LC(`${s}_basket`, g, wick, 0.22, 0.18, 0.36, 0, 0.18, 0, 28, true); LC(`${s}_base`, g, wick, 0.18, 0.18, 0.01, 0, 0.005, 0, 24);
    LM(`${s}_rim`, new THREE.TorusGeometry(0.22, 0.014, 8, 32), lstd('#8f6c40', 0.8), g, 0, 0.36, 0).rotation.x = Math.PI / 2;
    [['#f1f1f1', -0.06, 0.37, 0.05, 1.5], ['#5b7fa8', 0.07, 0.38, -0.04, 1.3], ['#d8a8b8', 0.0, 0.42, 0.0, 1.2], ['#3c3c3c', -0.08, 0.39, -0.08, 1.1], ['#e6c86b', 0.09, 0.36, 0.09, 1.0]]
      .forEach(([c, cx, cy, cz, sc], i) => LS(`${s}_clothes_${i + 1}`, g, lstd(c, 0.95), 0.1, cx, cy, cz, 1.2 * sc, 0.45, 1.0 * sc));
    // a sleeve / sock hanging over the rim
    const sleeve = new THREE.CatmullRomCurve3([[0.12, 0.4, 0.1], [0.2, 0.38, 0.16], [0.25, 0.3, 0.19], [0.25, 0.2, 0.2]].map(p => new THREE.Vector3(...p)));
    LM(`${s}_sleeve`, new THREE.TubeGeometry(sleeve, 12, 0.03, 8), lstd('#5b7fa8', 0.95), g);
    return g; };
  P.toy = (name, parent, x, y, z, ry = 0) => { const g = LG(name, parent, x, y, z, ry), s = name.replace(/^prop_/, 'pm_');   // cat toy: yarn ball + loose strand + felt mouse
    const yarn = lstd('#ffffff', 0.9, 0, { map: tex(64, 64, (c, w, h) => { c.fillStyle = '#c8353a'; c.fillRect(0, 0, w, h); c.strokeStyle = 'rgba(255,190,190,0.55)'; c.lineWidth = 2; for (let i = -h; i < w; i += 6) { c.beginPath(); c.moveTo(i, 0); c.lineTo(i + h, h); c.stroke(); } }) });
    LS(`${s}_yarn_ball`, g, yarn, 0.045, 0, 0.045, 0);
    const strand = new THREE.CatmullRomCurve3([[0.03, 0.03, 0.03], [0.1, 0.004, 0.06], [0.18, 0.004, 0.02], [0.24, 0.004, 0.09], [0.3, 0.004, 0.06]].map(p => new THREE.Vector3(...p)));
    LM(`${s}_yarn_strand`, new THREE.TubeGeometry(strand, 24, 0.003, 5), lstd('#c8353a', 0.9), g).castShadow = false;
    const mg = LG(`${s}_mouse`, g, -0.12, 0, 0.1, 0.9), felt = lstd('#9a9aa2', 1);
    LS(`${s}_mouse_body`, mg, felt, 0.03, 0, 0.022, 0, 1.6, 0.8, 1); LS(`${s}_mouse_ear_l`, mg, lstd('#e4a5ad', 0.9), 0.012, 0.03, 0.04, -0.015, 0.5, 1, 1); LS(`${s}_mouse_ear_r`, mg, lstd('#e4a5ad', 0.9), 0.012, 0.03, 0.04, 0.015, 0.5, 1, 1);
    const tail = LC(`${s}_mouse_tail`, mg, lstd('#e4a5ad', 0.9), 0.002, 0.002, 0.08, -0.08, 0.006, 0, 4); tail.rotation.z = Math.PI / 2;
    return g; };
  P.cuttingBoard = (name, parent, x, y, z, ry = 0) => { const g = LG(name, parent, x, y, z, ry), s = name.replace(/^prop_/, 'pm_');
    LB(`${s}_board`, g, lstd('#c08a52', 0.6), -0.2, 0.2, 0, 0.022, -0.13, 0.13, 0.01);
    LC(`${s}_handle_hole`, g, lstd('#7d5532', 0.7), 0.014, 0.014, 0.023, 0.17, 0.0115, 0, 12);
    [['#e9e3c7', -0.08, 0.02], ['#e9e3c7', -0.05, 0.03], ['#e9e3c7', -0.02, 0.025]].forEach(([c, dx, dz], i) => LC(`${s}_onion_${i + 1}`, g, lstd(c, 0.5), 0.022, 0.022, 0.006, dx, 0.025, dz, 14));
    [['#d2482f', 0.04, -0.05], ['#d2482f', 0.075, -0.04], ['#d2482f', 0.06, -0.075]].forEach(([c, dx, dz], i) => LC(`${s}_tomato_${i + 1}`, g, lstd(c, 0.45), 0.024, 0.024, 0.007, dx, 0.026, dz, 14));
    LS(`${s}_tomato_half`, g, lstd('#d2482f', 0.45), 0.03, 0.1, 0.03, 0.05, 1, 0.8, 1);
    const blade = LB(`${s}_knife_blade`, g, lstd('#d9dde1', 0.2, 0.9), -0.09, 0.05, 0.023, 0.026, -0.016, 0.016, 0.002); blade.rotation.y = 0.35; blade.position.set(-0.03, 0.0245, -0.07);
    const kh = LB(`${s}_knife_handle`, g, lstd('#1e1e1e', 0.5), -0.05, 0.05, 0.022, 0.034, -0.012, 0.012, 0.005); kh.rotation.y = 0.35; kh.position.set(-0.14, 0.028, -0.03);
    return g; };
  P.detergent = (name, parent, x, y, z, ry = 0) => { const g = LG(name, parent, x, y, z, ry), s = name.replace(/^prop_/, 'pm_');
    const jug = lstd('#ffffff', 0.4, 0, { map: tex(64, 64, (c, w, h) => { c.fillStyle = '#ff8a1f'; c.fillRect(0, 0, w, h); c.fillStyle = '#1f5fbf'; c.fillRect(0, 22, w, 22); c.fillStyle = '#ffd83a'; c.beginPath(); c.arc(w / 2, 33, 8, 0, 7); c.fill(); }) });
    LB(`${s}_jug`, g, jug, -0.06, 0.06, 0, 0.24, -0.045, 0.045, 0.02);
    LB(`${s}_shoulder`, g, lstd('#ff8a1f', 0.4), -0.055, 0.055, 0.235, 0.27, -0.04, 0.04, 0.015);
    LC(`${s}_cap`, g, lstd('#1f5fbf', 0.35), 0.022, 0.024, 0.035, -0.025, 0.285, 0, 16);
    const h = LM(`${s}_handle`, new THREE.TorusGeometry(0.035, 0.011, 8, 16, Math.PI), lstd('#ff8a1f', 0.4), g, 0.025, 0.24, 0); h.rotation.z = -0.3;
    return g; };
  P.lamp = (name, parent, x, y, z, ry = 0) => { const g = LG(name, parent, x, y, z, ry), s = name.replace(/^prop_/, 'pm_');   // small moon night-lamp; the glow is the prop itself
    LC(`${s}_stand`, g, lstd('#c9a46a', 0.5, 0.3), 0.04, 0.045, 0.012, 0, 0.006, 0, 20);
    LC(`${s}_stem`, g, lstd('#c9a46a', 0.5, 0.3), 0.004, 0.004, 0.04, 0, 0.03, 0, 8);
    const moon = lstd('#ffffff', 0.85, 0, { map: tex(64, 32, (c, w, h) => { c.fillStyle = '#f4ecd6'; c.fillRect(0, 0, w, h); c.fillStyle = 'rgba(160,140,110,0.35)'; for (let i = 0; i < 14; i++) { c.beginPath(); c.arc((i * 37) % w, (i * 13) % h, 2 + (i % 4), 0, 7); c.fill(); } }), emissive: new THREE.Color('#ffd9a0'), emissiveIntensity: 0.55 });
    LS(`${s}_moon`, g, moon, 0.06, 0, 0.11, 0);
    return g; };
  // rename a prop built with P.* into permanent (non-prop) decor: group + pm_ children
  const decor = (g, newName) => { const from = g.name.replace(/^prop_/, 'pm_'); g.name = newName; g.traverse(o => { if (o !== g && o.name.startsWith(from)) o.name = newName + o.name.slice(from.length); }); return g; };
  // ---------------- cat poses (tuxedo: black coat, white chest/muzzle/belly/paws, gold eyes)
  const catMats = { black: lstd('#17161a', 0.75), white: lstd('#f2efe8', 0.85), pink: lstd('#d9a0a0', 0.8), eye: lstd('#c9b53a', 0.3, 0, { emissive: new THREE.Color('#5a4d10'), emissiveIntensity: 0.4 }), whisker: lstd('#e9e6e0', 0.5) };
  const catHead = (P_, parent, at, { tilt = 0, yaw = 0, closed = false, yawn = false } = {}) => {
    const { black, white, pink, eye, whisker } = catMats;
    const pivot = LG(`${P_}_head`, parent, ...at);            // pivot at the neck joint; identity rotation
    const h = LG(`${P_}_head_shape`, pivot); h.rotation.set(0, yaw, tilt);   // rest expression lives on this inner group
    LS(`${P_}_head_ball`, h, black, 0.085, 0.05, 0.03, 0, 1.05, 0.95, 1.05);
    LS(`${P_}_muzzle_white`, h, white, 0.035, 0.123, 0.008, 0, 0.8, 0.7, 1.1);
    LS(`${P_}_blaze_white`, h, white, 0.03, 0.105, 0.05, 0, 0.9, 0.5, 0.45);
    LS(`${P_}_nose`, h, pink, 0.009, 0.152, 0.024, 0);
    if (yawn) LS(`${P_}_mouth`, h, lstd('#8a3a42', 0.6), 0.022, 0.14, -0.012, 0, 0.6, 0.9, 1);
    for (const [k, dz] of [['left', -1], ['right', 1]]) {
      LS(`${P_}_eye_${k}`, h, eye, 0.014, 0.115, 0.055, dz * 0.04, 0.6, closed ? 0.18 : 1, 1);
      const ear = LM(`${P_}_ear_${k}`, new THREE.ConeGeometry(0.035, 0.07, 4), black, h, 0.03, 0.115, dz * 0.052); ear.rotation.set(dz * 0.35, 0, -0.15);
      const inner = LM(`${P_}_ear_inner_${k}`, new THREE.ConeGeometry(0.018, 0.04, 4), pink, h, 0.045, 0.105, dz * 0.052); inner.rotation.copy(ear.rotation);
      for (let j = 0; j < 2; j++) { const w = LC(`${P_}_whisker_${k}_${j}`, h, whisker, 0.0015, 0.0015, 0.09, 0.13, 0.0 + j * 0.012, dz * 0.07, 4); w.rotation.set(Math.PI / 2, 0, (j - 0.5) * 0.2); w.castShadow = false; } }
    return pivot; };
  const catTail = (P_, parent, at, pts) => {
    const pivot = LG(`${P_}_tail`, parent, ...at);           // pivot at the tail root; identity rotation
    const curve = new THREE.CatmullRomCurve3(pts.map(p => new THREE.Vector3(...p)));
    LM(`${P_}_tail_tube`, new THREE.TubeGeometry(curve, 28, 0.03, 10), catMats.black, pivot);
    const tip = pts[pts.length - 1]; LS(`${P_}_tail_tip_white`, pivot, catMats.white, 0.031, tip[0], tip[1], tip[2], 1.1, 1.1, 1.1);
    return pivot; };
  const buildCatPose = (P_, parent, pose, scale = 1.15) => {
    const { black, white } = catMats, g = LG(P_, parent); const S = (n, mat, ...a) => LS(`${P_}_${n}`, g, mat, ...a);
    const leg = (n, a, b, pawMat = white) => { limb(`${P_}_leg_${n}`, g, black, a, b, 0.022); LS(`${P_}_paw_${n}`, g, pawMat, 0.028, b[0] + 0.012, Math.max(0.014, b[1] - 0.004), b[2], 1.4, 0.6, 1); };
    if (pose === 'sleep') {
      S('body', black, 0.16, 0, 0.11, 0, 1.35, 0.8, 1.0); S('haunch', black, 0.11, -0.1, 0.1, -0.05, 1, 0.85, 1);
      S('belly_white', white, 0.09, 0.02, 0.055, 0.1, 1.3, 0.55, 0.6); S('chest_white', white, 0.075, 0.16, 0.1, 0.06, 0.8, 1, 0.9);
      S('paw_front_left', white, 0.035, 0.27, 0.03, 0.1, 1.4, 0.7, 1); S('paw_front_right', white, 0.033, 0.25, 0.03, -0.03, 1.4, 0.7, 1);
      catHead(P_, g, [0.17, 0.15, 0.02], { tilt: -0.4, yaw: 0.35, closed: true });
      catTail(P_, g, [-0.2, 0.07, -0.06], [[0, 0, 0], [-0.03, -0.02, 0.14], [0.1, -0.03, 0.23], [0.28, -0.03, 0.24], [0.42, -0.03, 0.2]]);
    } else if (pose === 'sit') {
      S('haunch', black, 0.13, -0.05, 0.12, 0, 1.05, 0.95, 1.15);
      const t = S('body', black, 0.11, 0.03, 0.25, 0, 0.9, 1.45, 0.95); t.rotation.z = -0.25;
      const c = S('chest_white', white, 0.07, 0.095, 0.25, 0, 0.6, 1.45, 0.9); c.rotation.z = -0.25;
      for (const dz of [-0.042, 0.042]) leg(dz < 0 ? 'front_left' : 'front_right', [0.09, 0.2, dz], [0.13, 0.025, dz]);
      for (const dz of [-0.1, 0.1]) { S(`foot_rear_${dz < 0 ? 'left' : 'right'}`, black, 0.035, 0.03, 0.022, dz, 1.6, 0.55, 0.9); S(`toes_rear_${dz < 0 ? 'left' : 'right'}`, white, 0.022, 0.085, 0.016, dz, 1.2, 0.6, 1); }
      catHead(P_, g, [0.07, 0.37, 0], { tilt: 0.08 });
      catTail(P_, g, [-0.15, 0.04, -0.05], [[0, 0, 0], [-0.06, -0.01, 0.1], [0.03, -0.02, 0.2], [0.18, -0.02, 0.2], [0.25, -0.02, 0.13]]);
    } else if (pose === 'stretch') {
      const b = S('body', black, 0.16, 0, 0.165, 0, 1.45, 0.62, 0.8); b.rotation.z = -0.32;
      S('haunch', black, 0.11, -0.13, 0.22, 0, 1, 0.85, 1.1); S('chest_white', white, 0.07, 0.15, 0.1, 0, 0.9, 0.8, 0.9);
      for (const dz of [-0.05, 0.05]) leg(dz < 0 ? 'front_left' : 'front_right', [0.13, 0.08, dz], [0.33, 0.03, dz]);
      for (const dz of [-0.065, 0.065]) leg(dz < 0 ? 'rear_left' : 'rear_right', [-0.16, 0.2, dz], [-0.17, 0.025, dz]);
      catHead(P_, g, [0.22, 0.13, 0], { tilt: 0.25, closed: true, yawn: true });
      catTail(P_, g, [-0.22, 0.27, 0], [[0, 0, 0], [-0.07, 0.1, 0], [-0.06, 0.24, 0.02], [0.0, 0.31, 0.03]]);
    } else {   // walk_a / walk_b: diagonal leg pairs swapped
      const ph = (pose === 'walk_a' || pose === 'walk_1') ? 1 : -1;
      S('body', black, 0.16, 0, 0.26, 0, 1.45, 0.7, 0.72); S('belly_white', white, 0.09, 0.02, 0.205, 0, 1.4, 0.45, 0.6); S('chest_white', white, 0.07, 0.17, 0.25, 0, 0.7, 1.1, 0.85);
      const L = 0.22, foot = (x, z, a, lift) => [x + L * Math.sin(a), lift ? 0.07 : 0.03, z];
      leg('front_left', [0.14, 0.24, -0.055], foot(0.14, -0.055, 0.32 * ph, ph > 0)); leg('front_right', [0.14, 0.24, 0.055], foot(0.14, 0.055, -0.25 * ph, ph < 0));
      leg('rear_left', [-0.15, 0.24, -0.06], foot(-0.15, -0.06, -0.28 * ph, ph < 0)); leg('rear_right', [-0.15, 0.24, 0.06], foot(-0.15, 0.06, 0.3 * ph, ph > 0));
      catHead(P_, g, [0.22, 0.32, 0], { tilt: 0.05, yaw: 0.12 * ph });
      catTail(P_, g, [-0.22, 0.29, 0], ph > 0 ? [[0, 0, 0], [-0.07, 0.1, 0], [-0.06, 0.23, 0.02], [-0.01, 0.29, 0.04]] : [[0, 0, 0], [-0.08, 0.09, -0.02], [-0.09, 0.22, -0.04], [-0.04, 0.29, -0.06]]);
    }
    g.scale.setScalar(scale); return g; };
  return { lstd, LG, LM, LB, LS, LC, limb, empty, tex, plaid, stripes, knit, cloth, P, decor, buildCatPose, catHead, catTail, catMats };
}

// Export-time fix for the app's USDZ converter (ios/scripts/gltf_to_usdz.py), which draws ONLY primitives[0] of each glTF mesh.
// three's GLTFExporter writes a mesh with a material array (geometry groups) as one mesh with one primitive per material, so in the app
// only the first material's faces were drawn (e.g. the movie-room stone surround showed just its front face, 0.45 m off the wall).
// For the export we give each such mesh a single primitive: the node keeps its name and the material covering the largest area;
// faces with any other material move to child meshes `<name>_part_<n>` (n = 2, 3, ...) with an identity transform. Restored afterwards.
export function splitMultiMaterialForExport(root) {
  const undo = [];
  const triArea = (pos, a, b, c) => { const A = new THREE.Vector3().fromBufferAttribute(pos, a), B = new THREE.Vector3().fromBufferAttribute(pos, b), C = new THREE.Vector3().fromBufferAttribute(pos, c); return B.sub(A).cross(C.sub(A)).length() / 2; };
  const meshes = []; root.traverse(o => { if (o.isMesh && Array.isArray(o.material) && o.geometry.groups.length) meshes.push(o); });
  for (const m of meshes) {
    const g = m.geometry, pos = g.attributes.position, idx = g.index, byMat = new Map();
    for (const gr of g.groups) { const list = byMat.get(gr.materialIndex) || []; for (let i = gr.start; i < gr.start + gr.count; i++) list.push(idx ? idx.getX(i) : i); byMat.set(gr.materialIndex, list); }
    const area = new Map([...byMat].map(([k, l]) => { let a = 0; for (let i = 0; i < l.length; i += 3) a += triArea(pos, l[i], l[i + 1], l[i + 2]); return [k, a]; }));
    const order = [...byMat.keys()].sort((a, b) => area.get(b) - area.get(a));
    const sub = (list) => { const ng = g.clone(); ng.setIndex(list); ng.clearGroups(); return ng; };
    const orig = { geometry: g, material: m.material }, kids = [];
    m.geometry = sub(byMat.get(order[0])); m.material = orig.material[order[0]];
    order.slice(1).forEach((k, i) => { const c = new THREE.Mesh(sub(byMat.get(k)), orig.material[k]); c.name = `${m.name}_part_${i + 2}`; c.castShadow = m.castShadow; c.receiveShadow = m.receiveShadow; m.add(c); kids.push(c); });
    undo.push(() => { kids.forEach(c => m.remove(c)); m.geometry = orig.geometry; m.material = orig.material; });
  }
  return () => undo.reverse().forEach(f => f());
}
