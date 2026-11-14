// Viewer-side "life" runtime shared by all four room viewers (inlined by each build_viewer.mjs). Mirrors what the app does with the
// node names in ios/Rooms/README.md: hides userData.defaultVisible === false nodes, moves pet_cat between cat_spot_* and swaps its pose
// groups (pet_cat_pose_*; fallback meshes hidden while a pose shows), idles the pose head/tail joints, spins drums / fan blades while
// their device is on, sways curtain meshes from their heading, and puffs steam above prop_mug / prop_kettle.
export function setupLife(app, opts = {}) {
  const root = app.room ? app.room.root : app.den.root;
  root.traverse(o => { if (o.userData.defaultVisible === false) o.visible = false; });
  const AX = { x: new THREE.Vector3(1, 0, 0), y: new THREE.Vector3(0, 1, 0), z: new THREE.Vector3(0, 0, 1) };
  const panel = document.getElementById('panel');
  const lbl = document.createElement('div'); lbl.className = 'lbl'; lbl.textContent = 'Life'; panel.appendChild(lbl);
  const row = document.createElement('div'); row.className = 'btns'; panel.appendChild(row);
  const btn = (label, fn) => { const b = document.createElement('button'); b.textContent = label; b.onclick = () => fn(b); row.appendChild(b); return b; };
  // ---- cat
  const cat = root.getObjectByName('pet_cat');
  const poses = {}; if (cat) cat.children.forEach(o => { if (o.name.startsWith('pet_cat_pose_')) poses[o.name.slice(13)] = o; });
  const spots = {}; root.traverse(o => { if (o.name.startsWith('cat_spot_')) spots[o.name.slice(9)] = o; });
  const FITS = { sleep: ['sofa', 'sofa_main', 'fireplace'], sit: ['window', 'rug', 'fireplace'], stretch: ['rug', 'fireplace'], walk: ['rug'], default: ['sofa'] };
  const HOME = { default: 'sofa', sleep: 'fireplace', sit: 'window', stretch: 'rug', walk: 'rug' };
  let mode = 'default', walkFrom = null;
  const show = (k) => { cat.children.forEach(o => { if (o.isMesh) o.visible = k === 'default'; }); Object.entries(poses).forEach(([n, g]) => { g.visible = n === k || (k === 'walk' && n === 'walk_1'); }); };
  const setCat = (k, spot) => { const s = spots[spot || HOME[k]]; mode = k; if (s) { cat.position.copy(s.position); cat.rotation.y = s.rotation.y; } walkFrom = k === 'walk' ? s : null; show(k); };
  if (cat && Object.keys(poses).length) {
    const cb = {}; const mark = (k) => Object.entries(cb).forEach(([n, b]) => b.classList.toggle('active', n === k));
    [['default', 'Cat: curled'], ['sleep', 'Sleep'], ['sit', 'Sit (window)'], ['stretch', 'Stretch'], ['walk', 'Walk']].forEach(([k, l]) => { cb[k] = btn(l, () => { setCat(k); mark(k); }); });
    btn('🎲 Random', () => { const ks = Object.keys(FITS); const k = ks[Math.floor(Math.random() * ks.length)]; const f = FITS[k].filter(n => spots[n]); setCat(k, f[Math.floor(Math.random() * f.length)]); mark(k); });
    mark('default');
  }
  // ---- props toggle
  const props = []; root.traverse(o => { if (/^prop_/.test(o.name)) props.push(o); });
  let propsOn = true;
  if (props.length) { const b = btn('Props: on', () => { propsOn = !propsOn; props.forEach(p => { p.visible = propsOn; }); b.textContent = 'Props: ' + (propsOn ? 'on' : 'off'); }); b.classList.add('active'); }
  // ---- spinning pivots (drums, fan blades) and curtain meshes
  const spin = []; Object.entries(opts.spin || {}).forEach(([n, [dev, speed]]) => { const o = root.getObjectByName(n); if (o) spin.push([o, dev, speed, AX[o.userData.pivotAxis || (/drum/.test(n) ? 'z' : 'y')]]); });
  const curtains = []; root.traverse(o => { if (o.isMesh && /curtain/.test(o.name) && !/rod|track|light/.test(o.name)) curtains.push([o, Math.random() * 6, o.quaternion.clone()]); });
  const joints = []; root.traverse(o => { const m = /^pet_cat_pose_([a-z0-9_]+?)_(head|tail)$/.exec(o.name); if (m) joints.push([o, m[2], m[1] === 'sleep' ? 0.2 : 1, Math.random() * 6]); });
  // ---- steam above prop_mug* / prop_kettle* (viewer only, never exported)
  const steam = [], box = new THREE.Box3(), smat = new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.25, depthWrite: false });
  root.updateMatrixWorld(true);
  props.filter(p => /^prop_(mug|kettle)/.test(p.name)).forEach(p => { box.setFromObject(p); const top = new THREE.Vector3((box.min.x + box.max.x) / 2, box.max.y + 0.01, (box.min.z + box.max.z) / 2);
    for (let i = 0; i < 4; i++) { const m = new THREE.Mesh(new THREE.SphereGeometry(0.018, 10, 8), smat.clone()); m.userData = { top, ph: i / 4, prop: p }; app.scene.add(m); steam.push(m); } });
  const clock = new THREE.Clock(), q = new THREE.Quaternion();
  return function tick() {
    const dt = Math.min(clock.getDelta(), 0.05), t = clock.elapsedTime;
    joints.forEach(([o, kind, k, p]) => { if (kind === 'head') o.rotation.set(0, 0.22 * k * Math.sin(t * 0.6 + p) * Math.sin(t * 0.23 + p), 0.05 * k * Math.sin(t * 0.9 + p)); else o.rotation.set(0, 0.35 * k * Math.sin(t * 1.7 + p), 0.08 * k * Math.sin(t * 1.1 + p)); });
    if (mode === 'walk' && walkFrom) {
      const s = Math.sin(t * 0.45), back = Math.cos(t * 0.45) < 0, step = Math.floor(t * 3.2) % 2;
      const fwd = new THREE.Vector3(Math.cos(walkFrom.rotation.y), 0, -Math.sin(walkFrom.rotation.y));
      cat.position.copy(walkFrom.position).addScaledVector(fwd, 0.7 * s); cat.rotation.y = walkFrom.rotation.y + (back ? Math.PI : 0);
      if (poses.walk_1 && poses.walk_2) { poses.walk_1.visible = step === 0; poses.walk_2.visible = step === 1; }
    }
    spin.forEach(([o, dev, speed, ax]) => { const d = app.devices[dev]; if (d && d.on) o.rotateOnAxis(ax, dt * speed); });
    curtains.forEach(([o, p, q0]) => { q.setFromAxisAngle(AX.x, 0.025 * Math.sin(t * 0.8 + p) + 0.012 * Math.sin(t * 1.9 + p * 2)); o.quaternion.copy(q0).multiply(q); });
    steam.forEach(m => { const { top, ph, prop } = m.userData, f = (t * 0.35 + ph) % 1;
      m.visible = prop.visible; m.position.set(top.x + 0.02 * Math.sin(t * 2 + f * 9), top.y + f * 0.22, top.z + 0.02 * Math.cos(t * 1.7 + f * 7)); m.scale.setScalar(0.7 + f * 1.6); m.material.opacity = 0.28 * Math.sin(Math.PI * f); });
  };
}
