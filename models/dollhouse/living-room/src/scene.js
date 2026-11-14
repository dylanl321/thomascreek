// Den corner — "dollhouse miniature" digital twin. Room 01 of the house.
// Coordinates: metres, Y up. Room corner at origin. Bookshelf wall = plane z=0, fireplace wall = plane x=0.
// Room interior is x in [-4.4, 0], z in [0, 4.4].
import * as THREE from 'three';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { GLTFExporter } from 'three/addons/exporters/GLTFExporter.js';

// ---------------------------------------------------------------- style tokens
export const STYLE = {
  background: ['#f1ece4', '#d9d0c3'],
  cut: '#efe3cf', plaster: '#ebe0cc', leather: '#7a4a2b',          // section-cut faces (wall tops, slab edges)
  slab: '#e2d3bb',
  walnut: '#4b2c18', walnutPanel: '#5b3720', trim: '#6c4325', shelfBack: '#7b4d2c', counter: '#2e1a0e',
  gold: '#c9a046', brass: '#b8893a',
  carpet: '#cdbb9f', sofa: '#6f7377', sofaCushion: '#7c8085',
  ceramic: '#efe8dc', shade: '#efe2c8', blackShade: '#1f1a17', hearth: '#3b3532', soot: '#17110d',
  warm: '#ffc77d',        // ON light colour (2700K-ish)
  warmHot: '#ffe2b0',     // bulb core
  fire: '#ff8a2a',
  led: '#7fd3ff',
};

const rng = (seed) => { let s = seed; return () => (s = (s * 16807) % 2147483647) / 2147483647; };

function canvasTex(w, h, draw, srgb = true) {
  const c = document.createElement('canvas'); c.width = w; c.height = h;
  draw(c.getContext('2d'), w, h);
  const t = new THREE.CanvasTexture(c);
  if (srgb) t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 4;
  return t;
}

function makeTextures() {
  const glow = canvasTex(256, 256, (g, w, h) => {
    const gr = g.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, w / 2);
    gr.addColorStop(0, 'rgba(255,255,255,1)'); gr.addColorStop(0.35, 'rgba(255,255,255,0.55)');
    gr.addColorStop(0.7, 'rgba(255,255,255,0.15)'); gr.addColorStop(1, 'rgba(255,255,255,0)');
    g.fillStyle = gr; g.fillRect(0, 0, w, h);
  });
  // scallop: hot at the top edge centre, fading downward (wall-washer look)
  const scallop = canvasTex(256, 256, (g, w, h) => {
    g.save(); g.translate(w / 2, 0); g.scale(0.55, 1);
    const gr = g.createRadialGradient(0, 0, 0, 0, 0, h);
    gr.addColorStop(0, 'rgba(255,255,255,1)'); gr.addColorStop(0.3, 'rgba(255,255,255,0.5)');
    gr.addColorStop(0.7, 'rgba(255,255,255,0.12)'); gr.addColorStop(1, 'rgba(255,255,255,0)');
    g.fillStyle = gr; g.fillRect(-w, 0, 2 * w, h); g.restore();
  });
  const rug = canvasTex(1024, 768, (g, w, h) => {
    g.fillStyle = '#b9a385'; g.fillRect(0, 0, w, h);
    const band = (pos, horiz, width, col) => { g.fillStyle = col; horiz ? g.fillRect(0, pos, w, width) : g.fillRect(pos, 0, width, h); };
    for (let i = 0; i < 14; i++) { band(i * 76 + 20, false, 20, 'rgba(110,86,60,0.55)'); band(i * 76 + 46, false, 5, 'rgba(240,228,205,0.7)'); }
    for (let i = 0; i < 11; i++) { band(i * 76 + 20, true, 20, 'rgba(110,86,60,0.55)'); band(i * 76 + 46, true, 5, 'rgba(240,228,205,0.7)'); }
    g.strokeStyle = '#7a644a'; g.lineWidth = 26; g.strokeRect(13, 13, w - 26, h - 26);
  });
  const tv = canvasTex(1024, 576, (g, w, h) => {
    const sky = g.createLinearGradient(0, 0, 0, h * 0.62);
    sky.addColorStop(0, '#2a4f86'); sky.addColorStop(0.55, '#e98d5a'); sky.addColorStop(1, '#ffd08a');
    g.fillStyle = sky; g.fillRect(0, 0, w, h);
    g.fillStyle = '#fff2c8'; g.beginPath(); g.arc(w * 0.66, h * 0.5, 46, 0, Math.PI * 2); g.fill();
    const mtn = (pts, col) => { g.fillStyle = col; g.beginPath(); g.moveTo(0, h); pts.forEach(([x, y]) => g.lineTo(x * w, y * h)); g.lineTo(w, h); g.fill(); };
    mtn([[0, .5], [.12, .38], [.25, .5], [.4, .33], [.55, .52], [.75, .4], [.9, .5], [1, .45]], '#6b4e6e');
    mtn([[0, .6], [.18, .5], [.33, .6], [.5, .48], [.7, .62], [.85, .52], [1, .6]], '#3d3552');
    const lake = g.createLinearGradient(0, h * .62, 0, h);
    lake.addColorStop(0, '#f1a26a'); lake.addColorStop(1, '#1f2a44');
    g.fillStyle = lake; g.fillRect(0, h * .62, w, h * .38);
    g.fillStyle = 'rgba(0,0,0,0.45)'; g.fillRect(0, h - 62, w, 62);
    g.fillStyle = '#ffffff'; g.font = 'bold 26px sans-serif'; g.fillText('Now playing · Nature Escapes', 32, h - 23);
    g.fillStyle = 'rgba(255,255,255,0.35)'; g.fillRect(560, h - 36, 420, 6); g.fillStyle = '#ffffff'; g.fillRect(560, h - 36, 170, 6);
  });
  const background = canvasTex(16, 512, (g, w, h) => {
    const gr = g.createLinearGradient(0, 0, 0, h);
    gr.addColorStop(0, STYLE.background[0]); gr.addColorStop(1, STYLE.background[1]);
    g.fillStyle = gr; g.fillRect(0, 0, w, h);
  });
  // wash: soft horizontal band, hot along the top edge (LED strip under a shelf)
  const wash = canvasTex(256, 128, (g, w, h) => {
    const v = g.createLinearGradient(0, 0, 0, h); v.addColorStop(0, 'rgba(255,255,255,1)'); v.addColorStop(0.45, 'rgba(255,255,255,0.35)'); v.addColorStop(1, 'rgba(255,255,255,0)');
    g.fillStyle = v; g.fillRect(0, 0, w, h);
    g.globalCompositeOperation = 'destination-in';
    const hz = g.createLinearGradient(0, 0, w, 0); hz.addColorStop(0, 'rgba(0,0,0,0)'); hz.addColorStop(0.12, 'rgba(0,0,0,1)'); hz.addColorStop(0.88, 'rgba(0,0,0,1)'); hz.addColorStop(1, 'rgba(0,0,0,0)');
    g.fillStyle = hz; g.fillRect(0, 0, w, h);
  });
  const sky = canvasTex(1024, 768, (g, w, h) => {
    const gr = g.createLinearGradient(0, 0, 0, h);
    gr.addColorStop(0, '#070d22'); gr.addColorStop(0.55, '#16264f'); gr.addColorStop(0.78, '#2a4078'); gr.addColorStop(1, '#2a4078');
    g.fillStyle = gr; g.fillRect(0, 0, w, h);
    const r = rng(11); g.fillStyle = '#ffffff';
    for (let i = 0; i < 140; i++) { const x = r() * w, y = r() * h * 0.62, s = r() * 1.4 + 0.5; g.globalAlpha = 0.4 + r() * 0.6; g.beginPath(); g.arc(x, y, s, 0, 7); g.fill(); }
    g.globalAlpha = 1;
    const mx = w * 0.3, my = h * 0.18;
    const mg = g.createRadialGradient(mx, my, 0, mx, my, 70); mg.addColorStop(0, 'rgba(220,230,255,0.35)'); mg.addColorStop(1, 'rgba(220,230,255,0)');
    g.fillStyle = mg; g.fillRect(0, 0, w, h); g.fillStyle = '#f3f1e6'; g.beginPath(); g.arc(mx, my, 16, 0, 7); g.fill();
    g.fillStyle = '#0b1226'; g.beginPath(); g.moveTo(0, h);
    for (let x = 0; x <= w; x += 12) g.lineTo(x, h * 0.72 - 30 * Math.abs(Math.sin(x * 0.013)) - 22 * Math.abs(Math.sin(x * 0.041 + 1)) - r() * 10);
    g.lineTo(w, h); g.fill();
  });
  return { glow, scallop, wash, rug, tv, background, sky };
}

// ---------------------------------------------------------------- build
export const ROOM = { RX: 6.6, RZ: 6.6, HW: 4.3, VR: 2.0 }; // room size x/z, paneled wall height, vault rise

export function buildDen() {
  const T = makeTextures();
  const { RX, RZ, HW, VR } = ROOM;
  const std = (color, rough = 0.7, metal = 0, extra = {}) => new THREE.MeshStandardMaterial({ color, roughness: rough, metalness: metal, ...extra });
  const M = {
    cut: std(STYLE.cut, 0.95), slab: std(STYLE.slab, 0.95), plaster: std(STYLE.plaster, 0.95, 0, { side: THREE.DoubleSide }),
    walnut: std(STYLE.walnut, 0.65), panel: std(STYLE.walnutPanel, 0.6), trim: std(STYLE.trim, 0.55),
    shelfBack: std(STYLE.shelfBack, 0.7), counter: std(STYLE.counter, 0.4),
    gold: std(STYLE.gold, 0.32, 0.9), brass: std(STYLE.brass, 0.35, 0.85),
    carpet: std(STYLE.carpet, 1), rug: std('#ffffff', 1, 0, { map: T.rug }),
    sofa: std(STYLE.sofa, 0.95), sofaCushion: std(STYLE.sofaCushion, 0.95), legs: std('#2a1a10', 0.6),
    leather: std(STYLE.leather, 0.45), leatherDark: std('#5e3820', 0.5),
    ceramic: std(STYLE.ceramic, 0.35), hearth: std(STYLE.hearth, 0.85), soot: std(STYLE.soot, 1),
    firebrick: std('#4a2e24', 0.95), log: std('#5b3b22', 0.9), iron: std('#202020', 0.5, 0.6),
    glass: new THREE.MeshPhysicalMaterial({ color: '#cfe2e8', roughness: 0.05, metalness: 0, transparent: true, opacity: 0.22, depthWrite: false }),
    doorGlass: new THREE.MeshPhysicalMaterial({ color: '#a9bfd6', roughness: 0.05, metalness: 0, transparent: true, opacity: 0.1, depthWrite: false }),
    pillowA: std('#7a6f74', 1), pillowB: std('#9b9094', 1), pillowCream: std('#e7dcc8', 0.95), fur: std('#d9cdbb', 1),
  };
  const bookCols = ['#7d2e2a', '#2f4a5a', '#b49a6a', '#e9e1d1', '#3e5a3a', '#8a5a2b', '#c9b48a', '#4b3b5a'].map(c => std(c, 0.8));

  const root = new THREE.Group(); root.name = 'den';
  const G = (name, parent = root) => { const g = new THREE.Group(); g.name = name; parent.add(g); return g; };
  const arch = G('architecture'), ceiling = G('vaulted_ceiling'), shelves = G('builtin_bookshelves'), openings = G('doors_and_windows'), exterior = G('exterior_deck'),
    fire = G('fireplace_wall'), furn = G('furniture'), devGroup = G('devices'), anchors = G('device_anchors');

  const mesh = (name, geom, mat, parent, x = 0, y = 0, z = 0, shadow = true) => {
    const m = new THREE.Mesh(geom, mat); m.name = name; m.position.set(x, y, z);
    m.castShadow = shadow; m.receiveShadow = true; parent.add(m); return m;
  };
  const B = (name, parent, mat, x0, x1, y0, y1, z0, z1, r = 0) => {
    const w = x1 - x0, h = y1 - y0, d = z1 - z0;
    const rr = Math.min(r, w / 2 - 1e-4, h / 2 - 1e-4, d / 2 - 1e-4);
    const geo = rr > 0.002 ? new RoundedBoxGeometry(w, h, d, 2, rr) : new THREE.BoxGeometry(w, h, d);
    return mesh(name, geo, mat, parent, (x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2);
  };
  const cyl = (name, parent, mat, rt, rb, h, x, y, z, seg = 20, open = false) =>
    mesh(name, new THREE.CylinderGeometry(rt, rb, h, seg, 1, open), mat, parent, x, y, z);
  const sphere = (name, parent, mat, r, x, y, z) => mesh(name, new THREE.SphereGeometry(r, 16, 12), mat, parent, x, y, z);
  const raisedPanel = (name, parent, axis, face, a0, a1, y0, y1) => {
    const g = G(name, parent);
    if (axis === 'z') { B(name + '_field', g, M.panel, a0, a1, y0, y1, face, face + 0.012); B(name + '_raise', g, M.trim, a0 + 0.05, a1 - 0.05, y0 + 0.05, y1 - 0.05, face + 0.012, face + 0.026, 0.01); }
    else { B(name + '_field', g, M.panel, face - 0.012, face, y0, y1, a0, a1); B(name + '_raise', g, M.trim, face - 0.026, face - 0.012, y0 + 0.05, y1 - 0.05, a0 + 0.05, a1 - 0.05, 0.01); }
    return g;
  };
  // a row of raised panels between a0..a1, skipping gaps [[g0,g1],...]
  const panelRow = (name, parent, axis, face, a0, a1, y0, y1, target = 1.1, gaps = []) => {
    let segs = [[a0, a1]];
    gaps.forEach(([g0, g1]) => { segs = segs.flatMap(([s0, s1]) => (g1 <= s0 || g0 >= s1) ? [[s0, s1]] : [[s0, Math.max(s0, g0)], [Math.min(s1, g1), s1]].filter(([p, q]) => q - p > 0.25)); });
    segs.forEach(([s0, s1], si) => { const n = Math.max(1, Math.round((s1 - s0) / target)), w = (s1 - s0) / n;
      for (let i = 0; i < n; i++) raisedPanel(`${name}_${si + 1}_${i + 1}`, parent, axis, face, s0 + i * w + 0.02, s0 + (i + 1) * w - 0.02, y0, y1); });
  };

  // ---------------- key layout (metres)
  const DZ0 = 0.75, DZ1 = 1.95, DH = 2.66;                 // French doors opening
  const WZ0 = 0.85, WZ1 = 1.85, WY0 = 3.0, WY1 = 4.2;      // arched window above them (arch top at WY1)
  const GZ0 = 5.25, GZ1 = 6.25, GH = 2.66;                 // right glass door
  const FZ0 = 2.35, FZ1 = 4.95, FD = 0.26, FC = (FZ0 + FZ1) / 2, OZ0 = FC - 0.5, OZ1 = FC + 0.5, OY0 = 0.1, OY1 = 0.86;
  const TVZ = FC, TVY = 2.06, TVW = 1.72, TVH = 0.98, FR = 0.075;
  const CHX = -3.1, CHZ = RZ / 2;                          // chandelier position (under vault ridge)

  // ---------------- shell
  B('floor_slab', arch, M.slab, -RX, 0.22, -0.32, -0.02, -0.22, RZ);
  B('floor_carpet', arch, M.carpet, -RX, 0, -0.02, 0, 0, RZ);
  const cutMats = (cuts) => [0, 1, 2, 3, 4, 5].map(i => cuts.includes(i) ? M.cut : M.walnut);
  mesh('wall_bookshelf', new THREE.BoxGeometry(RX + 0.22, HW, 0.22), cutMats([1, 2]), arch, (-RX + 0.22) / 2, HW / 2, -0.11);
  // fireplace wall: extruded outline with door / window holes. caps = walnut, sides (cut edges) = cream
  { const sh = new THREE.Shape(); sh.moveTo(0, 0); sh.lineTo(RZ, 0); sh.lineTo(RZ, HW); sh.lineTo(0, HW); sh.lineTo(0, 0);
    const rect = (z0, z1, y0, y1) => { const p = new THREE.Path(); p.moveTo(z0, y0); p.lineTo(z0, y1); p.lineTo(z1, y1); p.lineTo(z1, y0); p.lineTo(z0, y0); return p; };
    sh.holes.push(rect(DZ0, DZ1, 0.003, DH), rect(GZ0, GZ1, 0.003, GH));
    const w = new THREE.Path(), r = (WZ1 - WZ0) / 2; w.moveTo(WZ0, WY0); w.lineTo(WZ1, WY0); w.lineTo(WZ1, WY1 - r); w.absarc(WZ0 + r, WY1 - r, r, 0, Math.PI, false); w.lineTo(WZ0, WY0); sh.holes.push(w);
    const m = mesh('wall_fireplace', new THREE.ExtrudeGeometry(sh, { depth: 0.22, bevelEnabled: false, curveSegments: 24 }), [M.walnut, M.cut], arch, 0.22, 0, 0);
    m.rotation.y = -Math.PI / 2;
  }
  // tympanum (vault end wall, cream plaster) and half barrel vault cut at the ridge
  { const sh = new THREE.Shape(); sh.moveTo(0, HW); sh.lineTo(RZ, HW); sh.absellipse(RZ / 2, HW, RZ / 2, VR, 0, Math.PI, false);
    const m = mesh('vault_end_wall', new THREE.ExtrudeGeometry(sh, { depth: 0.22, bevelEnabled: false, curveSegments: 48 }), [M.plaster, M.cut], ceiling, 0.22, 0, 0);
    m.rotation.y = -Math.PI / 2;
    const t = 0.16, v = new THREE.Shape(), N = 40;
    for (let i = 0; i <= N; i++) { const a = Math.PI - (Math.PI / 2) * (i / N); const p = [RZ / 2 + (RZ / 2) * Math.cos(a), HW + VR * Math.sin(a)]; i ? v.lineTo(...p) : v.moveTo(...p); }
    for (let i = N; i >= 0; i--) { const a = Math.PI - (Math.PI / 2) * (i / N); v.lineTo(RZ / 2 + (RZ / 2 + t) * Math.cos(a), HW + (VR + t) * Math.sin(a)); }
    const vm = mesh('vault_shell_cut', new THREE.ExtrudeGeometry(v, { depth: RX, bevelEnabled: false }), [M.cut, M.plaster], ceiling, 0, 0, 0);
    vm.rotation.y = -Math.PI / 2;
    // cornice at the springing line
    B('springing_cornice_bookshelf_wall', ceiling, M.trim, -RX, 0, HW - 0.16, HW, 0, 0.14, 0.01);
    B('springing_cornice_fireplace_wall', ceiling, M.trim, -0.14, 0, HW - 0.16, HW, 0, RZ, 0.01);
  }

  // ---------------- built-in bookshelves (z=0 wall)
  const X0 = -4.36, X1 = -0.16, PW = 0.14, NB = 3, BW = (X1 - X0 - (NB + 1) * PW) / NB;
  const R = rng(7);
  B('lower_cabinet_body', shelves, M.walnut, X0, X1, 0.08, 0.84, 0, 0.5);
  B('lower_cabinet_toekick', shelves, M.counter, X0, X1, 0, 0.08, 0, 0.45);
  B('countertop', shelves, M.counter, X0 - 0.02, X1 + 0.02, 0.84, 0.89, 0, 0.55, 0.008);
  B('cornice', shelves, M.walnut, X0 - 0.03, X1 + 0.03, 2.64, 3.0, 0, 0.56);
  B('cornice_crown_trim', shelves, M.trim, X0 - 0.05, X1 + 0.05, 2.84, 2.96, 0, 0.6, 0.01);
  B('cornice_bottom_trim', shelves, M.trim, X0 - 0.03, X1 + 0.03, 2.62, 2.66, 0, 0.58, 0.006);
  const bayCenters = [];
  for (let i = 0; i <= NB; i++) { const px = X0 + i * (BW + PW); B(`shelf_pilaster_${i}`, shelves, M.trim, px, px + PW, 0.89, 2.64, 0, 0.37, 0.008); }
  const archShape = (W, H, s, ry, p) => { const sh = new THREE.Shape();
    sh.moveTo(-W / 2, 0); sh.lineTo(-W / 2, H); sh.lineTo(W / 2, H); sh.lineTo(W / 2, 0); sh.lineTo(W / 2 - p, 0); sh.lineTo(W / 2 - p, s);
    sh.absellipse(0, s, W / 2 - p, ry, 0, Math.PI, false); sh.lineTo(-W / 2 + p, 0); sh.lineTo(-W / 2, 0); return sh; };
  const SHELF_Y = [1.30, 1.70, 2.10];
  for (let b = 0; b < NB; b++) {
    const idx = NB - b, bx0 = X0 + PW + b * (BW + PW), bx1 = bx0 + BW, cx = (bx0 + bx1) / 2;
    bayCenters[idx] = cx;
    const bay = G(`shelf_bay_${idx}`, shelves);
    B(`shelf_bay_${idx}_back`, bay, M.shelfBack, bx0, bx1, 0.89, 2.64, 0, 0.02);
    for (const y of SHELF_Y) B(`shelf_bay_${idx}_shelf_${y.toFixed(2)}`, bay, M.trim, bx0, bx1, y - 0.015, y + 0.015, 0.02, 0.34, 0.004);
    B(`shelf_bay_${idx}_top`, bay, M.trim, bx0, bx1, 2.58, 2.62, 0.02, 0.34);
    mesh(`shelf_bay_${idx}_arch`, new THREE.ExtrudeGeometry(archShape(BW + 0.02, 0.42, 0.1, 0.22, 0.02), { depth: 0.03, bevelEnabled: false }), M.trim, bay, cx, 2.2, 0.34);
    for (let d = 0; d < 2; d++) {
      const dx0 = bx0 + d * BW / 2 + 0.02, dx1 = bx0 + (d + 1) * BW / 2 - 0.02;
      B(`cabinet_door_${idx}_${d + 1}`, bay, M.panel, dx0, dx1, 0.12, 0.8, 0.5, 0.52, 0.006);
      B(`cabinet_door_${idx}_${d + 1}_raise`, bay, M.trim, dx0 + 0.06, dx1 - 0.06, 0.2, 0.72, 0.52, 0.535, 0.01);
      sphere(`cabinet_knob_${idx}_${d + 1}`, bay, M.brass, 0.012, d ? dx0 + 0.04 : dx1 - 0.04, 0.68, 0.545);
    }
    for (const [si, y] of [[1, 1.315], [2, 1.715], [3, 2.115]]) {
      let x = bx0 + 0.06 + R() * 0.12; const end = x + 0.35 + R() * 0.3;
      while (x < end && x < bx1 - 0.08) { const bw = 0.025 + R() * 0.03, bh = 0.17 + R() * 0.1;
        B(`book_${idx}_${si}_${x.toFixed(2)}`, bay, bookCols[Math.floor(R() * bookCols.length)], x, x + bw, y, y + bh, 0.06, 0.27, 0.004); x += bw + 0.003; }
      if (R() > 0.3) { const vx = bx1 - 0.15 - R() * 0.15;
        if (R() > 0.5) cyl(`vase_${idx}_${si}`, bay, M.ceramic, 0.04, 0.05, 0.18, vx, y + 0.09, 0.17, 16);
        else B(`box_${idx}_${si}`, bay, std('#d9c9a8', 0.6), vx - 0.08, vx + 0.08, y, y + 0.08, 0.1, 0.25, 0.01); }
    }
    const pf = G(`picture_frame_${idx}`, bay);
    B(`picture_frame_${idx}_frame`, pf, idx === 3 ? M.gold : M.trim, -0.13, 0.13, 0, 0.22, -0.012, 0.012, 0.005);
    B(`picture_frame_${idx}_art`, pf, std(['#a9b9a0', '#d8c3a5', '#9fb1c4'][b], 0.9), -0.105, 0.105, 0.025, 0.195, 0.011, 0.014);
    pf.position.set(cx - 0.25 + b * 0.12, 0.89, 0.12); pf.rotation.x = -0.12;
  }
  B('counter_book_stack', shelves, bookCols[3], bayCenters[2] + 0.12, bayCenters[2] + 0.36, 0.89, 0.95, 0.08, 0.26, 0.004);
  cyl('counter_bowl', shelves, M.ceramic, 0.09, 0.05, 0.07, bayCenters[1] + 0.25, 0.925, 0.2, 20);
  // bookshelf-wall paneling (left of built-ins, and the upper wall)
  raisedPanel('wall_panel_bookshelf_left_low', arch, 'z', 0, -RX, X0 - 0.05, 0.12, 0.84);
  panelRow('wall_panel_bookshelf_left_high', arch, 'z', 0, -RX, X0 - 0.05, 0.98, 2.95, 1.1);
  B('baseboard_bookshelf_left', arch, M.trim, -RX, X0 - 0.03, 0, 0.12, 0, 0.03);
  panelRow('wall_panel_bookshelf_upper', arch, 'z', 0, -RX, 0, 3.1, HW - 0.22, 1.1);

  // ---------------- glass doors + arched window (shared builder)
  const glassUnit = (name, z0, z1, h, opts = {}) => {
    const g = G(name, openings), lin = 0.03, xa = 0.03, xb = 0.08, top = h - 0.36, rows = opts.rows || 5, leaves = opts.leaves || 2;
    B(`${name}_jamb_left`, g, M.trim, -0.01, 0.22, 0, h, z0, z0 + lin); B(`${name}_jamb_right`, g, M.trim, -0.01, 0.22, 0, h, z1 - lin, z1);
    B(`${name}_jamb_head`, g, M.trim, -0.01, 0.22, h - 0.04, h, z0, z1); B(`${name}_threshold`, g, M.brass, -0.05, 0.22, 0, 0.022, z0, z1);
    B(`${name}_casing_left`, g, M.trim, -0.04, 0, 0, h + 0.1, z0 - 0.1, z0 + 0.01, 0.006); B(`${name}_casing_right`, g, M.trim, -0.04, 0, 0, h + 0.1, z1 - 0.01, z1 + 0.1, 0.006);
    B(`${name}_casing_head`, g, M.trim, -0.06, 0, h, h + 0.16, z0 - 0.14, z1 + 0.14, 0.008);
    const dz0 = z0 + lin, dz1 = z1 - lin, lw = (dz1 - dz0) / leaves;
    for (let d = 0; d < leaves; d++) {
      const lz0 = dz0 + d * lw, lz1 = lz0 + lw, n = (p) => `${name}_leaf_${d + 1}_${p}`, lg = G(`${name}_leaf_${d + 1}`, g);
      B(n('stile_a'), lg, M.trim, xa, xb, 0.022, top, lz0, lz0 + 0.075, 0.005); B(n('stile_b'), lg, M.trim, xa, xb, 0.022, top, lz1 - 0.075, lz1, 0.005);
      B(n('rail_top'), lg, M.trim, xa, xb, top - 0.09, top, lz0, lz1, 0.005); B(n('rail_bottom'), lg, M.trim, xa, xb, 0.022, 0.24, lz0, lz1, 0.005);
      const pz0 = lz0 + 0.075, pz1 = lz1 - 0.075, py0 = 0.24, py1 = top - 0.09;
      B(n('pane'), lg, M.doorGlass, 0.052, 0.058, py0, py1, pz0, pz1).castShadow = false;
      B(n('muntin_v'), lg, M.trim, xa + 0.008, xb - 0.008, py0, py1, (pz0 + pz1) / 2 - 0.011, (pz0 + pz1) / 2 + 0.011);
      for (let k = 1; k < rows; k++) { const y = py0 + (py1 - py0) * k / rows; B(n(`muntin_h_${k}`), lg, M.trim, xa + 0.008, xb - 0.008, y - 0.011, y + 0.011, pz0, pz1); }
      const hz = leaves === 1 ? lz1 - 0.045 : (d === 0 ? lz1 - 0.045 : lz0 + 0.045);
      B(n('handle_rose'), lg, M.brass, xa - 0.012, xa, 0.98, 1.06, hz - 0.02, hz + 0.02, 0.005);
      B(n('handle_lever'), lg, M.brass, xa - 0.04, xa - 0.025, 1.01, 1.03, hz - 0.08, hz + 0.01, 0.006);
    }
    B(`${name}_transom_bar`, g, M.trim, xa, xb, top, top + 0.06, dz0, dz1, 0.005);
    B(`${name}_transom_pane`, g, M.doorGlass, 0.052, 0.058, top + 0.06, h - 0.04, dz0, dz1).castShadow = false;
    for (let k = 1; k < 4; k++) { const z = dz0 + (dz1 - dz0) * k / 4; B(`${name}_transom_muntin_${k}`, g, M.trim, xa + 0.008, xb - 0.008, top + 0.06, h - 0.04, z - 0.011, z + 0.011); }
    portal(`portal_${name}`, new THREE.PlaneGeometry(dz1 - dz0, h - 0.06), 0.12, (h - 0.06) / 2 + 0.02, (dz0 + dz1) / 2);
    return g;
  };
  const portalMat = new THREE.MeshBasicMaterial({ colorWrite: false, depthWrite: false, side: THREE.DoubleSide, stencilWrite: true, stencilRef: 1, stencilFunc: THREE.AlwaysStencilFunc, stencilZPass: THREE.ReplaceStencilOp });
  function portal(name, geo, x, y, z) { const p = mesh(name, geo, portalMat, exterior, x, y, z, false); p.rotation.y = -Math.PI / 2; p.receiveShadow = false; p.renderOrder = 1; p.userData.noExport = true; return p; }

  // French doors in a paneled reveal (as in the photos)
  const RD = 0.3;
  B('french_door_reveal_left', openings, M.walnut, -RD, 0, 0, 2.76, DZ0 - 0.1, DZ0);
  B('french_door_reveal_right', openings, M.walnut, -RD, 0, 0, 2.76, DZ1, DZ1 + 0.1);
  B('french_door_reveal_soffit', openings, M.walnut, -RD, 0, DH, 2.76, DZ0, DZ1);
  B('french_door_reveal_floor', openings, M.trim, -RD, 0, 0, 0.012, DZ0, DZ1);
  B('french_door_casing_left', openings, M.trim, -RD - 0.03, -RD + 0.01, 0, 2.76, DZ0 - 0.12, DZ0 + 0.01, 0.006);
  B('french_door_casing_right', openings, M.trim, -RD - 0.03, -RD + 0.01, 0, 2.76, DZ1 - 0.01, DZ1 + 0.12, 0.006);
  B('french_door_casing_head', openings, M.trim, -RD - 0.03, -RD + 0.01, DH - 0.02, 2.76, DZ0 - 0.12, DZ1 + 0.12, 0.006);
  B('french_door_crown', openings, M.trim, -RD - 0.07, 0, 2.76, 2.84, DZ0 - 0.16, DZ1 + 0.16, 0.01);
  glassUnit('french_doors', DZ0, DZ1, DH, { leaves: 2, rows: 5 });
  // tall arched window above the French doors
  { const g = G('arched_window', openings), r = (WZ1 - WZ0) / 2, sy = WY1 - r, cz = (WZ0 + WZ1) / 2;
    const archPath = (inset) => { const s = new THREE.Shape(); s.moveTo(WZ0 + inset, WY0 + inset); s.lineTo(WZ1 - inset, WY0 + inset); s.lineTo(WZ1 - inset, sy); s.absarc(cz, sy, r - inset, 0, Math.PI, false); s.lineTo(WZ0 + inset, WY0 + inset); return s; };
    const frame = archPath(0); frame.holes.push(archPath(0.07));
    const fm = mesh('arched_window_frame', new THREE.ExtrudeGeometry(frame, { depth: 0.07, bevelEnabled: false, curveSegments: 32 }), M.trim, g, 0.12, 0, 0); fm.rotation.y = -Math.PI / 2;
    const cs = archPath(-0.09); cs.holes.push(archPath(0));
    const cm = mesh('arched_window_casing', new THREE.ExtrudeGeometry(cs, { depth: 0.04, bevelEnabled: false, curveSegments: 32 }), M.trim, g, 0.0, 0, 0); cm.rotation.y = -Math.PI / 2;
    const gl = mesh('arched_window_glass', new THREE.ShapeGeometry(archPath(0.06), 32), M.doorGlass, g, 0.09, 0, 0, false); gl.rotation.y = -Math.PI / 2;
    B('arched_window_sill', g, M.trim, -0.1, 0.12, WY0 - 0.05, WY0, WZ0 - 0.12, WZ1 + 0.12, 0.006);
    const xa = 0.08, xb = 0.12;
    for (const z of [cz - (r - 0.07) / 2, cz, cz + (r - 0.07) / 2]) B(`arched_window_muntin_v_${z.toFixed(2)}`, g, M.trim, xa, xb, WY0 + 0.07, z === cz ? WY1 - 0.07 : sy + Math.sqrt(Math.max(0, (r - 0.07) ** 2 - (z - cz) ** 2)) - 0.01, z - 0.01, z + 0.01);
    for (let k = 1; k <= 4; k++) { const y = WY0 + 0.07 + (sy - WY0 - 0.07) * k / 4; B(`arched_window_muntin_h_${k}`, g, M.trim, xa, xb, y - 0.01, y + 0.01, WZ0 + 0.07, WZ1 - 0.07); }
    for (const a of [Math.PI / 4, (3 * Math.PI) / 4]) { const mm = B(`arched_window_muntin_r_${a.toFixed(2)}`, g, M.trim, xa, xb, -0.01, 0.01, 0, r - 0.07); mm.position.set((xa + xb) / 2, sy + Math.sin(a) * (r - 0.07) / 2, cz + Math.cos(a) * (r - 0.07) / 2); mm.rotation.x = -a; }
    portal('portal_arched_window', new THREE.ShapeGeometry(archPath(0.06), 32), 0.16, 0, 0).rotation.y = -Math.PI / 2;
    exterior.getObjectByName('portal_arched_window').position.set(0.16, 0, 0);
  }
  // right glass door
  glassUnit('glass_door_right', GZ0, GZ1, GH, { leaves: 1, rows: 6 });
  // fireplace-wall paneling
  panelRow('wall_panel_fireplace_upper', fire, 'x', 0, 0.05, RZ - 0.05, 3.1, HW - 0.22, 1.1, [[WZ0 - 0.14, WZ1 + 0.14]]);
  raisedPanel('wall_panel_fireplace_corner_low', fire, 'x', 0, 0.03, DZ0 - 0.13, 0.12, 0.86);
  raisedPanel('wall_panel_fireplace_corner_high', fire, 'x', 0, 0.03, DZ0 - 0.13, 0.98, 2.95);
  raisedPanel('pilaster_panel_low', fire, 'x', 0, DZ1 + 0.13, FZ0 - 0.02, 0.12, 0.86);
  raisedPanel('pilaster_panel_high', fire, 'x', 0, DZ1 + 0.13, FZ0 - 0.02, 0.98, 2.95);
  raisedPanel('wall_panel_right_low', fire, 'x', 0, FZ1 + 0.02, GZ0 - 0.11, 0.12, 0.86);
  raisedPanel('wall_panel_right_high', fire, 'x', 0, FZ1 + 0.02, GZ0 - 0.11, 0.98, 2.95);
  raisedPanel('wall_panel_right_end', fire, 'x', 0, GZ1 + 0.11, RZ - 0.02, 0.12, 2.95);
  raisedPanel('wall_panel_above_glass_door', fire, 'x', 0, GZ0 - 0.1, GZ1 + 0.1, GH + 0.2, 2.95);
  B('crown_fireplace_wall', fire, M.trim, -0.1, 0, 2.95, 3.03, 0, RZ, 0.006);
  B('crown_bookshelf_wall_left', arch, M.trim, -RX, X0 - 0.05, 2.95, 3.03, 0, 0.1, 0.006);

  // ---------------- fireplace / chimney breast
  B('fireplace_breast_left', fire, M.walnut, -FD, 0, 0, 2.84, FZ0, OZ0);
  B('fireplace_breast_right', fire, M.walnut, -FD, 0, 0, 2.84, OZ1, FZ1);
  B('fireplace_breast_top', fire, M.walnut, -FD, 0, OY1, 2.84, OZ0, OZ1);
  B('fireplace_breast_sill', fire, M.walnut, -FD, 0, 0, OY0, OZ0, OZ1);
  B('fireplace_breast_crown', fire, M.trim, -FD - 0.08, 0, 2.84, 2.98, FZ0 - 0.04, FZ1 + 0.04, 0.01);
  B('fireplace_surround_l', fire, M.trim, -FD - 0.03, -FD, OY0, OY1 + 0.08, OZ0 - 0.08, OZ0, 0.006);
  B('fireplace_surround_r', fire, M.trim, -FD - 0.03, -FD, OY0, OY1 + 0.08, OZ1, OZ1 + 0.08, 0.006);
  B('fireplace_surround_top', fire, M.trim, -FD - 0.03, -FD, OY1, OY1 + 0.08, OZ0 - 0.08, OZ1 + 0.08, 0.006);
  raisedPanel('fireplace_panel_left', fire, 'x', -FD, FZ0 + 0.06, OZ0 - 0.14, 0.12, 1.08);
  raisedPanel('fireplace_panel_right', fire, 'x', -FD, OZ1 + 0.14, FZ1 - 0.06, 0.12, 1.08);
  raisedPanel('fireplace_frieze', fire, 'x', -FD, OZ0 - 0.08, OZ1 + 0.08, 0.98, 1.2);
  B('mantel_shelf', fire, M.trim, -FD - 0.2, 0, 1.3, 1.37, FZ0 - 0.08, FZ1 + 0.08, 0.01);
  B('mantel_bed_molding', fire, M.trim, -FD - 0.1, -FD, 1.24, 1.3, FZ0 - 0.04, FZ1 + 0.04, 0.008);
  raisedPanel('fireplace_panel_above_tv', fire, 'x', -FD, FZ0 + 0.1, FZ1 - 0.1, 2.68, 2.82);
  B('hearth', fire, M.hearth, -FD - 0.5, -FD, 0, 0.045, OZ0 - 0.25, OZ1 + 0.25, 0.01);
  const fb = G('firebox', fire);
  B('firebox_back', fb, M.firebrick, -0.04, 0, OY0, OY1, OZ0, OZ1);
  B('firebox_floor', fb, M.soot, -FD, 0, OY0 - 0.01, OY0, OZ0, OZ1);
  B('firebox_side_l', fb, M.firebrick, -FD, 0, OY0, OY1, OZ0, OZ0 + 0.02);
  B('firebox_side_r', fb, M.firebrick, -FD, 0, OY0, OY1, OZ1 - 0.02, OZ1);
  B('firebox_ceiling', fb, M.soot, -FD, 0, OY1 - 0.02, OY1, OZ0, OZ1);
  B('fire_grate', fb, M.iron, -0.2, -0.06, OY0, OY0 + 0.05, OZ0 + 0.2, OZ1 - 0.2);
  const lg1 = cyl('fire_log_1', fb, M.log, 0.05, 0.05, 0.6, -0.13, OY0 + 0.1, FC - 0.05, 10); lg1.rotation.set(Math.PI / 2, 0.15, 0);
  const lg2 = cyl('fire_log_2', fb, M.log, 0.045, 0.045, 0.55, -0.1, OY0 + 0.1, FC + 0.08, 10); lg2.rotation.set(Math.PI / 2, -0.2, 0);
  const lg3 = cyl('fire_log_3', fb, M.log, 0.04, 0.04, 0.5, -0.12, OY0 + 0.18, FC, 10); lg3.rotation.set(Math.PI / 2, 0.1, 0);
  // TV in gold frame
  const tvG = G('tv', devGroup);
  B('tv_frame_top', tvG, M.gold, -FD - 0.07, -FD, TVY + TVH / 2, TVY + TVH / 2 + FR, TVZ - TVW / 2 - FR, TVZ + TVW / 2 + FR, 0.018);
  B('tv_frame_bottom', tvG, M.gold, -FD - 0.07, -FD, TVY - TVH / 2 - FR, TVY - TVH / 2, TVZ - TVW / 2 - FR, TVZ + TVW / 2 + FR, 0.018);
  B('tv_frame_left', tvG, M.gold, -FD - 0.07, -FD, TVY - TVH / 2, TVY + TVH / 2, TVZ - TVW / 2 - FR, TVZ - TVW / 2, 0.018);
  B('tv_frame_right', tvG, M.gold, -FD - 0.07, -FD, TVY - TVH / 2, TVY + TVH / 2, TVZ + TVW / 2, TVZ + TVW / 2 + FR, 0.018);
  B('tv_body', tvG, std('#111', 0.5), -FD - 0.05, -FD, TVY - TVH / 2, TVY + TVH / 2, TVZ - TVW / 2, TVZ + TVW / 2);
  const tvScreenMat = std('#060607', 0.12, 0.3);
  const tvScreen = mesh('tv_screen', new THREE.PlaneGeometry(TVW, TVH), tvScreenMat, tvG, -FD - 0.051, TVY, TVZ, false);
  tvScreen.rotation.y = -Math.PI / 2;
  const sbG = G('soundbar', devGroup);
  B('soundbar_body', sbG, std('#1c1c1e', 0.55), -FD - 0.1, -FD, 1.4, 1.48, TVZ - 0.55, TVZ + 0.55, 0.03);
  B('soundbar_grille', sbG, std('#2a2a2d', 0.9), -FD - 0.102, -FD - 0.09, 1.405, 1.475, TVZ - 0.52, TVZ + 0.52, 0.01);
  const sbLedMat = std('#334', 0.4);
  B('soundbar_led', sbG, sbLedMat, -FD - 0.106, -FD - 0.1, 1.41, 1.418, TVZ - 0.12, TVZ + 0.12);

  // ---------------- exterior night deck (visible only through glass, via stencil)
  { const ext = (color, opts = {}) => new THREE.MeshBasicMaterial({ color, stencilWrite: true, stencilRef: 1, stencilFunc: THREE.EqualStencilFunc, stencilFail: THREE.KeepStencilOp, stencilZFail: THREE.KeepStencilOp, stencilZPass: THREE.KeepStencilOp, ...opts });
    const E = (name, mat, x0, x1, y0, y1, z0, z1) => { const m = B(name, exterior, mat, x0, x1, y0, y1, z0, z1); m.castShadow = false; m.receiveShadow = false; m.renderOrder = 2; return m; };
    const deckA = ext('#3a4157'), deckB = ext('#343a4f'), deckGap = ext('#1a1e2c'), rail = ext('#59617b'), railDark = ext('#474e66');
    const ZA = -1.5, ZB = RZ + 1.5;
    E('deck_substructure', deckGap, 0.22, 3.0, -0.32, -0.06, ZA, ZB);
    for (let i = 0, z = ZA; z < ZB; i++, z += 0.145) E(`deck_board_${i + 1}`, i % 2 ? deckA : deckB, 0.22, 3.0, -0.06, -0.03, z, Math.min(z + 0.133, ZB));
    const RXr = 2.0;
    for (let z = ZA + 0.1, i = 0; z <= ZB; z += 1.3, i++) E(`deck_rail_post_${i + 1}`, rail, RXr - 0.05, RXr + 0.05, -0.03, 1.08, z - 0.05, z + 0.05);
    E('deck_rail_top', rail, RXr - 0.07, RXr + 0.07, 1.02, 1.08, ZA, ZB);
    E('deck_rail_bottom', railDark, RXr - 0.03, RXr + 0.03, 0.08, 0.12, ZA, ZB);
    for (let z = ZA, i = 0; z < ZB; z += 0.13, i++) E(`deck_baluster_${i + 1}`, railDark, RXr - 0.015, RXr + 0.015, 0.12, 1.02, z, z + 0.03);
    const sky = mesh('exterior_night_sky', new THREE.PlaneGeometry(16, 10), ext('#ffffff', { map: T.sky }), exterior, 5.0, 3.0, RZ / 2, false);
    sky.rotation.y = -Math.PI / 2; sky.receiveShadow = false; sky.renderOrder = 2; sky.userData.noExport = true;
  }

  // ---------------- furniture
  // large grey L-sectional: main run along the open left side facing the fireplace, return along the bookshelf side
  const sec = G('sectional', furn);
  { const SX0 = -6.2, SZ0 = 1.3, SZ1 = 5.3, D = 1.0, RXe = -3.9;
    const seatY = 0.44, backH = 0.86;
    B('sectional_main_base', sec, M.sofa, SX0, SX0 + D, 0.08, 0.38, SZ0, SZ1, 0.05);
    B('sectional_main_back', sec, M.sofa, SX0, SX0 + 0.24, 0.38, backH, SZ0, SZ1, 0.08);
    B('sectional_return_base', sec, M.sofa, SX0 + D, RXe, 0.08, 0.38, SZ0, SZ0 + D, 0.05);
    B('sectional_return_back', sec, M.sofa, SX0, RXe, 0.38, backH, SZ0, SZ0 + 0.24, 0.08);
    B('sectional_arm_main_end', sec, M.sofa, SX0, SX0 + D, 0.08, 0.66, SZ1 - 0.22, SZ1, 0.09);
    B('sectional_arm_return_end', sec, M.sofa, RXe - 0.22, RXe, 0.08, 0.66, SZ0, SZ0 + D, 0.09);
    // seat cushions
    const mainSeats = 3, mL = (SZ1 - 0.22 - (SZ0 + D)) / mainSeats;
    B('sectional_corner_seat', sec, M.sofaCushion, SX0 + 0.24, SX0 + D, 0.38, seatY + 0.1, SZ0 + 0.24, SZ0 + D, 0.07);
    for (let i = 0; i < mainSeats; i++) B(`sectional_main_seat_${i + 1}`, sec, M.sofaCushion, SX0 + 0.24, SX0 + D - 0.01, 0.38, seatY + 0.1, SZ0 + D + i * mL + 0.01, SZ0 + D + (i + 1) * mL - 0.01, 0.07);
    const rL = (RXe - 0.22 - (SX0 + D)) / 2;
    for (let i = 0; i < 2; i++) B(`sectional_return_seat_${i + 1}`, sec, M.sofaCushion, SX0 + D + i * rL + 0.01, SX0 + D + (i + 1) * rL - 0.01, 0.38, seatY + 0.1, SZ0 + 0.24, SZ0 + D - 0.01, 0.07);
    for (let i = 0; i < 4; i++) B(`sectional_main_back_cushion_${i + 1}`, sec, M.sofaCushion, SX0 + 0.16, SX0 + 0.38, 0.52, backH - 0.02, SZ0 + 0.3 + i * (SZ1 - SZ0 - 0.55) / 4, SZ0 + 0.3 + (i + 1) * (SZ1 - SZ0 - 0.55) / 4 - 0.02, 0.08);
    for (let i = 0; i < 2; i++) B(`sectional_return_back_cushion_${i + 1}`, sec, M.sofaCushion, SX0 + D + i * rL + 0.01, SX0 + D + (i + 1) * rL - 0.01, 0.52, backH - 0.02, SZ0 + 0.16, SZ0 + 0.38, 0.08);
    for (const [x, z] of [[SX0 + 0.06, SZ0 + 0.06], [SX0 + 0.06, SZ1 - 0.06], [SX0 + D - 0.06, SZ1 - 0.06], [RXe - 0.06, SZ0 + 0.06], [RXe - 0.06, SZ0 + D - 0.06]]) B('sectional_leg', sec, M.legs, x - 0.025, x + 0.025, 0, 0.08, z - 0.025, z + 0.025);
    // throw pillows (muted mauve-grey + a fur throw)
    const pillow = (n, mat, x, z, ry, rz) => { const p = B(n, sec, mat, -0.08, 0.08, -0.2, 0.2, -0.22, 0.22, 0.07); p.position.set(x, 0.72, z); p.rotation.set(0, ry, rz); return p; };
    pillow('sectional_pillow_1', M.pillowA, SX0 + 0.42, SZ0 + 0.5, 0.75, -0.25);
    pillow('sectional_pillow_2', M.pillowB, SX0 + 0.42, SZ0 + 2.4, 0, -0.25);
    pillow('sectional_pillow_3', M.pillowA, SX0 + 0.42, SZ1 - 0.6, 0, -0.25);
    pillow('sectional_pillow_4', M.pillowB, SX0 + 1.5, SZ0 + 0.42, Math.PI / 2, -0.25);
    B('sectional_fur_throw', sec, M.fur, SX0 + 0.35, SX0 + 0.98, 0.54, 0.6, SZ1 - 1.0, SZ1 - 0.3, 0.03);
  }
  // side table + (lamp device below) between the sectional's return and the shelves
  const STX = -3.45, STZ = 1.05;
  const table = G('side_table', furn);
  B('side_table_top', table, M.walnut, -0.27, 0.27, 0.56, 0.6, -0.27, 0.27, 0.01);
  B('side_table_apron', table, M.walnut, -0.25, 0.25, 0.48, 0.56, -0.25, 0.25, 0.005);
  B('side_table_shelf', table, M.walnut, -0.24, 0.24, 0.12, 0.15, -0.24, 0.24, 0.005);
  for (const [a, b] of [[-1, -1], [-1, 1], [1, -1], [1, 1]]) B('side_table_leg', table, M.trim, a * 0.22 - 0.025, a * 0.22 + 0.025, 0, 0.56, b * 0.22 - 0.025, b * 0.22 + 0.025, 0.006);
  table.position.set(STX, 0, STZ);
  // brown leather armchair, front right
  const chair = G('armchair_leather', furn);
  { const D = 0.95, W = 1.0;
    B('armchair_base', chair, M.leather, -D / 2, D / 2, 0.1, 0.42, -W / 2, W / 2, 0.06);
    B('armchair_seat_cushion', chair, M.leather, -D / 2 + 0.2, D / 2 - 0.02, 0.42, 0.55, -W / 2 + 0.17, W / 2 - 0.17, 0.06);
    B('armchair_back', chair, M.leather, -D / 2, -D / 2 + 0.22, 0.42, 1.0, -W / 2, W / 2, 0.09);
    for (const s of [-1, 1]) { B(`armchair_arm_${s < 0 ? 'left' : 'right'}`, chair, M.leather, -D / 2, D / 2, 0.1, 0.68, s * (W / 2 - 0.085) - 0.085, s * (W / 2 - 0.085) + 0.085, 0.08);
      const roll = cyl(`armchair_arm_roll_${s < 0 ? 'left' : 'right'}`, chair, M.leather, 0.1, 0.1, D, 0, 0.68, s * (W / 2 - 0.085), 16); roll.rotation.z = Math.PI / 2; }
    for (const [lx, lz] of [[-1, -1], [-1, 1], [1, -1], [1, 1]]) B('armchair_leg', chair, M.legs, lx * (D / 2 - 0.07) - 0.03, lx * (D / 2 - 0.07) + 0.03, 0, 0.1, lz * (W / 2 - 0.07) - 0.03, lz * (W / 2 - 0.07) + 0.03);
    const p = B('armchair_pillow', chair, M.pillowCream, -0.07, 0.07, -0.19, 0.19, -0.22, 0.22, 0.07); p.position.set(-D / 2 + 0.32, 0.75, 0); p.rotation.z = -0.25;
  }
  chair.position.set(-1.85, 0, 5.85); chair.rotation.y = 2.5;
  // pet_cat: fat black cat curled on the sectional's return end (near side table / bookshelves). Not a device.
  { const cat = G('pet_cat', furn), black = std('#17161a', 0.75), white = std('#f2efe8', 0.85), pink = std('#d9a0a0', 0.8), eye = std('#c9b53a', 0.3, 0, { emissive: new THREE.Color('#5a4d10'), emissiveIntensity: 0.4 });
    const S = (n, mat, r, x, y, z, sx = 1, sy = 1, sz = 1) => { const m = sphere(n, cat, mat, r, x, y, z); m.scale.set(sx, sy, sz); return m; };
    S('pet_cat_body', black, 0.16, 0, 0.11, 0, 1.35, 0.8, 1.0);           // round loaf, long axis = local x
    S('pet_cat_haunch', black, 0.11, -0.1, 0.1, -0.05, 1, 0.85, 1);
    S('pet_cat_belly_white', white, 0.09, 0.02, 0.055, 0.1, 1.3, 0.55, 0.6);
    S('pet_cat_chest_white', white, 0.075, 0.16, 0.1, 0.06, 0.8, 1, 0.9);
    S('pet_cat_head', black, 0.085, 0.2, 0.2, 0.02, 1.05, 0.95, 1.05);
    S('pet_cat_muzzle_white', white, 0.035, 0.275, 0.18, 0.03, 0.8, 0.7, 1.1);
    S('pet_cat_nose', pink, 0.009, 0.3, 0.195, 0.03);
    for (const [k, dz] of [['left', -0.035], ['right', 0.045]]) {
      S(`pet_cat_eye_${k}`, eye, 0.013, 0.27, 0.225, 0.03 + dz, 0.6, 1, 1);
      const ear = mesh(`pet_cat_ear_${k}`, new THREE.ConeGeometry(0.035, 0.07, 4), black, cat, 0.19, 0.29, 0.02 + dz * 1.4); ear.rotation.set(dz > 0 ? 0.35 : -0.35, 0, -0.15);
      const inner = mesh(`pet_cat_ear_inner_${k}`, new THREE.ConeGeometry(0.018, 0.04, 4), pink, cat, 0.205, 0.28, 0.02 + dz * 1.4); inner.rotation.copy(ear.rotation);
    }
    S('pet_cat_paw_front_white', white, 0.035, 0.29, 0.03, 0.09, 1.4, 0.7, 1);   // one white front paw tucked forward
    S('pet_cat_paw_front_black', black, 0.033, 0.26, 0.03, -0.05, 1.4, 0.7, 1);
    const tail = new THREE.CatmullRomCurve3([[-0.2, 0.07, -0.06], [-0.22, 0.05, 0.08], [-0.08, 0.04, 0.17], [0.1, 0.04, 0.18], [0.24, 0.04, 0.14]].map(p => new THREE.Vector3(...p)));
    mesh('pet_cat_tail', new THREE.TubeGeometry(tail, 24, 0.032, 10), black, cat);
    S('pet_cat_tail_tip_white', white, 0.034, 0.24, 0.04, 0.14, 1.3, 0.95, 1);
    cat.position.set(-4.5, 0.54, 1.92); cat.rotation.y = -2.0; cat.scale.setScalar(1.15);
    cat.traverse(o => { if (o.isMesh) o.castShadow = true; });
  }
  B('rug', furn, M.rug, -4.95, -1.35, 0, 0.012, 2.25, 5.15).castShadow = false;

  // ---------------- devices
  const devices = {};
  const addAnchor = (name, type, pos) => { const a = new THREE.Object3D(); a.name = `anchor_${name}`; a.position.copy(pos); a.userData = { device: name, type }; anchors.add(a); return a; };
  const decalMat = (tex, color, opacity) => new THREE.MeshBasicMaterial({ map: tex, color, transparent: true, opacity, blending: THREE.AdditiveBlending, depthWrite: false, toneMapped: false, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: -2 });
  const decal = (name, parent, tex, color, opacity, w, h, x, y, z, orient) => {
    const m = new THREE.Mesh(new THREE.PlaneGeometry(w, h), decalMat(tex, color, opacity)); m.name = name;
    if (orient === 'up') m.rotation.x = -Math.PI / 2; else if (orient === '-x') m.rotation.y = -Math.PI / 2;
    m.position.set(x, y, z); m.renderOrder = 5; m.visible = false; parent.add(m); return m;
  };
  const lightMats = () => ({
    bulb: std('#d8d2c8', 0.4, 0, { emissive: new THREE.Color(STYLE.warmHot), emissiveIntensity: 0 }),
    shade: std(STYLE.shade, 0.9, 0, { side: THREE.DoubleSide, emissive: new THREE.Color(STYLE.warm), emissiveIntensity: 0 }),
  });
  const register = (name, cfg) => {
    const d = { name, label: cfg.label, type: cfg.type, room: 'den', on: false, emissive: cfg.emissive || [], lights: cfg.lights || [], decals: cfg.decals || [], show: cfg.show || [], custom: cfg.custom, anchor: cfg.anchor, group: cfg.group };
    d.anchorObj = addAnchor(name, cfg.type, cfg.anchor);
    if (cfg.group) cfg.group.userData = { device: name, type: cfg.type };
    devices[name] = d; return d;
  };
  const pl = (name, parent, color, x, y, z, dist) => { const l = new THREE.PointLight(color, 0, dist, 2); l.name = name; l.position.set(x, y, z); parent.add(l); return l; };

  // shelf lights: warm LED strips under each shelf (and under the top) in the three arched bays
  { const g = G('shelf_lights', devGroup); const m = lightMats(); const lights = [], decals = [];
    for (let idx = 1; idx <= 3; idx++) {
      const cx = bayCenters[idx]; let k = 0;
      for (const y of [2.58, 2.085, 1.685, 1.285]) {
        k++;
        B(`light_shelf_strip_${idx}_${k}`, g, m.bulb, cx - BW / 2 + 0.05, cx + BW / 2 - 0.05, y - 0.012, y - 0.002, 0.28, 0.3).castShadow = false;
        decals.push(decal(`glow_shelf_${idx}_${k}_back`, g, T.wash, STYLE.warm, 0.8, BW, 0.38, cx, y - 0.2, 0.024, '+z'));
        decals.push(decal(`glow_shelf_${idx}_${k}_shelf`, g, T.glow, STYLE.warm, 0.4, BW * 1.1, 0.3, cx, y - 0.395, 0.18, 'up'));
      }
      lights.push(pl(`light_shelf_bay_${idx}`, g, STYLE.warm, cx, 1.9, 0.32, 1.8));
    }
    register('shelf_lights', { label: 'Bookshelf LED strips', type: 'light', group: g, emissive: [{ mat: m.bulb, on: 6 }], lights: lights.map(l => ({ light: l, on: 1.8 })), decals, anchor: new THREE.Vector3(bayCenters[2], 2.1, 0.36) });
  }
  // soffit downlights in the cornice
  { const g = G('soffit_downlights', devGroup); const m = lightMats(); const decals = [], lights = [];
    for (let idx = 1; idx <= 3; idx++) {
      const cx = bayCenters[idx];
      cyl(`recessed_light_${idx}_trim`, g, M.brass, 0.05, 0.05, 0.008, cx, 2.617, 0.46, 20).castShadow = false;
      cyl(`recessed_light_${idx}`, g, m.bulb, 0.035, 0.035, 0.01, cx, 2.614, 0.46, 20).castShadow = false;
      decals.push(decal(`glow_downlight_${idx}_doors`, g, T.scallop, STYLE.warm, 0.7, 0.9, 0.7, cx, 0.45, 0.556, '+z'));
      decals.push(decal(`glow_downlight_${idx}_counter`, g, T.glow, STYLE.warm, 0.5, 0.8, 0.35, cx, 0.892, 0.4, 'up'));
      decals.push(decal(`glow_downlight_${idx}_floor`, g, T.glow, STYLE.warm, 0.35, 1.0, 0.6, cx, 0.014, 0.75, 'up'));
      const s = new THREE.SpotLight(STYLE.warm, 0, 4, 0.75, 0.6, 2); s.name = `light_downlight_${idx}`; s.position.set(cx, 2.6, 0.46); s.target.position.set(cx, 0, 0.75); g.add(s); g.add(s.target); lights.push(s);
    }
    register('soffit_downlights', { label: 'Shelf downlights', type: 'light', group: g, emissive: [{ mat: m.bulb, on: 6 }], lights: lights.map(l => ({ light: l, on: 6 })), decals, anchor: new THREE.Vector3(bayCenters[2], 2.62, 0.46) });
  }
  // two brass candle sconces flanking the TV (one device each)
  const sconce = (side, SZ) => {
    const name = `sconce_${side}`, g = G(name, devGroup), m = lightMats(), SY = 2.0, fx = -FD;
    B(`${name}_backplate`, g, M.brass, fx - 0.02, fx, SY - 0.16, SY + 0.06, SZ - 0.045, SZ + 0.045, 0.012);
    sphere(`${name}_finial`, g, M.brass, 0.022, fx - 0.03, SY - 0.17, SZ);
    for (const [k, dz] of [[1, -0.11], [2, 0.11]]) {
      const curve = new THREE.CatmullRomCurve3([new THREE.Vector3(fx - 0.02, SY - 0.08, SZ), new THREE.Vector3(fx - 0.09, SY - 0.1, SZ + dz * 0.5), new THREE.Vector3(fx - 0.13, SY - 0.06, SZ + dz)]);
      mesh(`${name}_arm_${k}`, new THREE.TubeGeometry(curve, 16, 0.009, 8), M.brass, g);
      cyl(`${name}_cup_${k}`, g, M.brass, 0.032, 0.018, 0.03, fx - 0.13, SY - 0.04, SZ + dz, 14);
      cyl(`${name}_candle_${k}`, g, M.ceramic, 0.013, 0.013, 0.11, fx - 0.13, SY + 0.03, SZ + dz, 10);
      const b = sphere(`${name}_bulb_${k}`, g, m.bulb, 0.018, fx - 0.13, SY + 0.105, SZ + dz); b.scale.set(0.8, 1.5, 0.8); b.castShadow = false;
    }
    const decals = [decal(`glow_${name}_up`, g, T.scallop, STYLE.warm, 0.85, 0.6, 0.7, fx - 0.003, SY + 0.12 + 0.35, SZ, '-x'),
      decal(`glow_${name}_halo`, g, T.glow, STYLE.warm, 0.5, 0.7, 0.7, fx - 0.003, SY + 0.05, SZ, '-x')];
    decals[0].rotation.z = Math.PI;
    const l = pl(`light_${name}`, g, STYLE.warm, fx - 0.3, SY + 0.1, SZ, 3);
    register(name, { label: `Sconce (${side} of TV)`, type: 'light', group: g, emissive: [{ mat: m.bulb, on: 8 }], lights: [{ light: l, on: 1.5 }], decals, anchor: new THREE.Vector3(fx - 0.13, SY + 0.08, SZ) });
  };
  sconce('left', TVZ - TVW / 2 - FR - 0.22);
  sconce('right', TVZ + TVW / 2 + FR + 0.22);
  // table lamp
  { const g = G('table_lamp', devGroup); const m = lightMats(); const [tx, tz] = [STX, STZ];
    const prof = [[0, 0], [0.07, 0], [0.075, 0.02], [0.06, 0.04], [0.1, 0.13], [0.09, 0.22], [0.035, 0.28], [0.02, 0.33], [0, 0.33]].map(([a, b]) => new THREE.Vector2(a, b));
    mesh('table_lamp_base', new THREE.LatheGeometry(prof, 24), M.ceramic, g, tx, 0.6, tz);
    cyl('table_lamp_stem', g, M.brass, 0.008, 0.008, 0.18, tx, 1.01, tz, 8);
    sphere('table_lamp_bulb', g, m.bulb, 0.035, tx, 1.06, tz);
    cyl('table_lamp_shade', g, m.shade, 0.13, 0.2, 0.24, tx, 1.08, tz, 32, true);
    const decals = [decal('glow_table_lamp_floor', g, T.glow, STYLE.warm, 0.4, 2.6, 2.6, tx, 0.016, tz + 0.25, 'up'),
      decal('glow_table_lamp_table', g, T.glow, STYLE.warm, 0.6, 0.6, 0.6, tx, 0.602, tz, 'up'),
      decal('glow_table_lamp_wall', g, T.glow, STYLE.warm, 0.35, 1.6, 1.0, tx, 0.75, 0.556, '+z')];
    const l = pl('light_table_lamp', g, STYLE.warm, tx, 1.05, tz, 4);
    register('table_lamp', { label: 'Table lamp', type: 'light', group: g, emissive: [{ mat: m.bulb, on: 5 }, { mat: m.shade, on: 0.9 }], lights: [{ light: l, on: 2.0 }], decals, anchor: new THREE.Vector3(tx, 1.22, tz) });
  }
  // large two-tier brass candle chandelier hanging from the vault ridge
  { const g = G('chandelier', devGroup); const m = lightMats(); const top = HW + VR;
    const bodyTop = 4.55, chainLen = top - bodyTop;
    cyl('chandelier_canopy', g, M.brass, 0.08, 0.11, 0.05, CHX, top - 0.025, CHZ, 20);
    for (let i = 0; i < Math.floor(chainLen / 0.08); i++) { const lk = mesh(`chandelier_chain_link_${i + 1}`, new THREE.TorusGeometry(0.022, 0.006, 6, 12), M.brass, g, CHX, top - 0.06 - i * 0.08, CHZ); lk.rotation.y = (i % 2) * Math.PI / 2; }
    const prof = [[0, 0], [0.05, 0.02], [0.11, 0.09], [0.12, 0.16], [0.07, 0.24], [0.035, 0.3], [0.05, 0.36], [0.035, 0.42], [0.025, 0.6], [0.06, 0.66], [0.05, 0.72], [0.025, 0.78], [0.02, 0.98], [0.04, 1.02], [0.02, 1.07], [0, 1.08]].map(([a, b]) => new THREE.Vector2(a, b));
    const body0 = bodyTop - 1.08;
    mesh('chandelier_body', new THREE.LatheGeometry(prof, 24), M.brass, g, CHX, body0, CHZ);
    sphere('chandelier_bottom_finial', g, M.brass, 0.035, CHX, body0 - 0.03, CHZ);
    const tier = (tName, n, radius, hubY, rise, phase) => {
      for (let i = 0; i < n; i++) {
        const a = phase + (i / n) * Math.PI * 2, c = Math.cos(a), s = Math.sin(a), P = (r, y) => new THREE.Vector3(CHX + c * r, y, CHZ + s * r);
        const curve = new THREE.CatmullRomCurve3([P(0.03, hubY), P(radius * 0.35, hubY - 0.09), P(radius * 0.75, hubY - 0.05), P(radius, hubY + rise)]);
        mesh(`chandelier_${tName}_arm_${i + 1}`, new THREE.TubeGeometry(curve, 20, 0.016, 8), M.brass, g);
        const tip = P(radius, hubY + rise);
        cyl(`chandelier_${tName}_cup_${i + 1}`, g, M.brass, 0.04, 0.02, 0.035, tip.x, tip.y + 0.017, tip.z, 14);
        cyl(`chandelier_${tName}_candle_${i + 1}`, g, M.ceramic, 0.015, 0.015, 0.13, tip.x, tip.y + 0.1, tip.z, 10);
        const b = sphere(`chandelier_${tName}_bulb_${i + 1}`, g, m.bulb, 0.02, tip.x, tip.y + 0.185, tip.z); b.scale.set(0.8, 1.5, 0.8); b.castShadow = false;
      }
    };
    tier('lower', 8, 0.6, body0 + 0.2, 0.06, 0);
    tier('upper', 6, 0.36, body0 + 0.62, 0.06, Math.PI / 6);
    const lowerRing = mesh('chandelier_lower_ring', new THREE.TorusGeometry(0.42, 0.008, 6, 48), M.brass, g, CHX, body0 + 0.14, CHZ); lowerRing.rotation.x = Math.PI / 2;
    const decals = [decal('glow_chandelier_floor', g, T.glow, STYLE.warm, 0.4, 4.2, 4.2, CHX, 0.02, CHZ, 'up')];
    const l = pl('light_chandelier', g, STYLE.warm, CHX, body0 + 0.3, CHZ, 9);
    register('chandelier', { label: 'Chandelier', type: 'light', group: g, emissive: [{ mat: m.bulb, on: 7 }], lights: [{ light: l, on: 9 }], decals, anchor: new THREE.Vector3(CHX, body0 + 0.45, CHZ) });
  }
  // fireplace (gas logs)
  { const g = G('fireplace_flames', devGroup);
    const flameA = std('#ff9a3a', 0.8, 0, { emissive: new THREE.Color('#ff7a1a'), emissiveIntensity: 5, transparent: true, opacity: 0.95 });
    const flameB = std('#ffd36b', 0.8, 0, { emissive: new THREE.Color('#ffcc55'), emissiveIntensity: 7 });
    const show = [];
    [[-0.3, 0.26, flameA], [-0.12, 0.34, flameA], [0.08, 0.3, flameA], [0.26, 0.24, flameA], [-0.2, 0.18, flameB], [0.0, 0.22, flameB], [0.16, 0.17, flameB]].forEach(([dz, h, mat], i) => {
      const f = mesh(`fire_flame_${i + 1}`, new THREE.ConeGeometry(mat === flameB ? 0.045 : 0.07, h, 6), mat, g, -0.12 - (mat === flameB ? 0.04 : 0), OY0 + 0.14 + h / 2, FC + dz, false);
      f.visible = false; show.push(f);
    });
    const emberMat = std('#3a1a10', 0.9, 0, { emissive: new THREE.Color('#ff4a10'), emissiveIntensity: 0 });
    B('fire_embers', g, emberMat, -0.2, -0.06, OY0 + 0.05, OY0 + 0.07, OZ0 + 0.22, OZ1 - 0.22).castShadow = false;
    const decals = [decal('glow_fire_hearth', g, T.glow, STYLE.fire, 0.7, 1.6, 1.8, -FD - 0.35, 0.05, FC, 'up'),
      decal('glow_fire_back', g, T.glow, STYLE.fire, 0.8, 1.0, 0.8, -0.042, OY0 + 0.32, FC, '-x')];
    const l = pl('light_fireplace', g, STYLE.fire, -0.5, 0.45, FC, 4);
    register('fireplace', { label: 'Fireplace', type: 'fireplace', group: g, emissive: [{ mat: emberMat, on: 3 }], lights: [{ light: l, on: 3.5 }], decals, show, anchor: new THREE.Vector3(-FD - 0.05, 0.5, FC) });
  }
  // TV + soundbar
  { const tvGlow = decal('glow_tv_wall', tvG, T.glow, '#ffb98a', 0.45, 2.8, 1.8, -FD - 0.003, TVY, TVZ, '-x');
    const tvFloor = decal('glow_tv_floor', tvG, T.glow, '#ffc8a0', 0.18, 2.2, 2.2, -1.2, 0.02, TVZ, 'up');
    register('tv', { label: 'TV', type: 'media', group: tvG, decals: [tvGlow, tvFloor], anchor: new THREE.Vector3(-FD - 0.06, TVY, TVZ),
      custom: (on) => {
        if (on) { tvScreenMat.color.set('#ffffff'); tvScreenMat.map = T.tv; tvScreenMat.emissive.set('#ffffff'); tvScreenMat.emissiveMap = T.tv; tvScreenMat.emissiveIntensity = 1.25; tvScreenMat.roughness = 0.4; tvScreenMat.metalness = 0; }
        else { tvScreenMat.color.set('#060607'); tvScreenMat.map = null; tvScreenMat.emissive.set('#000000'); tvScreenMat.emissiveMap = null; tvScreenMat.emissiveIntensity = 0; tvScreenMat.roughness = 0.12; tvScreenMat.metalness = 0.3; }
        tvScreenMat.needsUpdate = true;
      } });
    sbLedMat.emissive = new THREE.Color(STYLE.led);
    const sbGlow = decal('glow_soundbar', sbG, T.glow, STYLE.led, 0.5, 0.18, 0.7, -FD - 0.108, 1.414, TVZ, '-x');
    register('soundbar', { label: 'Soundbar', type: 'media', group: sbG, emissive: [{ mat: sbLedMat, on: 6 }], decals: [sbGlow], anchor: new THREE.Vector3(-FD - 0.1, 1.44, TVZ) });
  }

  const setDevice = (name, on) => {
    const d = devices[name]; if (!d) return; d.on = !!on;
    d.emissive.forEach(e => { e.mat.emissiveIntensity = on ? e.on : 0; });
    d.lights.forEach(l => { l.light.intensity = on ? l.on : 0; });
    d.decals.forEach(m => { m.visible = !!on; });
    d.show.forEach(m => { m.visible = !!on; });
    if (d.custom) d.custom(!!on);
  };
  Object.keys(devices).forEach(n => setDevice(n, false));
  return { root, devices, setDevice, textures: T };
}

export const PRESETS = {
  all_off: {},
  all_on: { shelf_lights: 1, soffit_downlights: 1, sconce_left: 1, sconce_right: 1, table_lamp: 1, chandelier: 1, fireplace: 1, tv: 1, soundbar: 1 },
  evening: { shelf_lights: 1, sconce_left: 1, sconce_right: 1, table_lamp: 1 },
  movie_night: { tv: 1, soundbar: 1, fireplace: 1, shelf_lights: 1 },
};

export const VIEWS = {
  dollhouse: { pos: [-14.2, 11.6, 14.6], target: [-2.75, 1.9, 2.85], fov: 30 },
  dollhouse_close: { pos: [-10.6, 7.4, 10.9], target: [-2.4, 1.6, 2.4], fov: 30 },
  photo: { pos: [-5.6, 1.65, 6.2], target: [-1.2, 2.0, 2.0], fov: 80 },
  door_detail: { pos: [-3.2, 1.55, 3.0], target: [0, 1.6, 1.35], fov: 52 },
  cat_detail: { pos: [-3.4, 1.5, 3.3], target: [-4.45, 0.6, 1.95], fov: 35 },
  top: { pos: [-6.5, 15, 8.5], target: [-3.0, 0.5, 3.0], fov: 34 },
};

export function createDen(container, { width = 1600, height = 1000, pixelRatio = 1, bloom = true } = {}) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
  renderer.setPixelRatio(pixelRatio); renderer.setSize(width, height);
  renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.toneMappingExposure = 1.0;
  renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  container.appendChild(renderer.domElement);
  const scene = new THREE.Scene();
  const den = buildDen();
  scene.add(den.root);
  scene.background = den.textures.background;
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.environmentIntensity = 0.28;
  const studio = new THREE.Group(); studio.name = 'studio_lighting'; scene.add(studio);
  const hemi = new THREE.HemisphereLight('#fff3e3', '#5b4636', 0.85); hemi.name = 'studio_hemi'; studio.add(hemi);
  const sun = new THREE.DirectionalLight('#fff1dd', 1.25); sun.name = 'studio_key'; sun.position.set(-10, 14, 9); sun.target.position.set(-3, 0, 3);
  sun.castShadow = true; sun.shadow.mapSize.set(4096, 4096); Object.assign(sun.shadow.camera, { left: -9, right: 9, top: 9, bottom: -9, near: 1, far: 40 });
  sun.shadow.bias = -0.0004; sun.shadow.normalBias = 0.02; sun.shadow.radius = 4;
  studio.add(sun); studio.add(sun.target);
  const camera = new THREE.PerspectiveCamera(30, width / height, 0.05, 100);
  const rt = new THREE.WebGLRenderTarget(width * pixelRatio, height * pixelRatio, { type: THREE.HalfFloatType, samples: 4, stencilBuffer: true });
  const composer = new EffectComposer(renderer, rt);
  composer.addPass(new RenderPass(scene, camera));
  const bloomPass = new UnrealBloomPass(new THREE.Vector2(width, height), 0.38, 0.45, 1.0);
  if (bloom) composer.addPass(bloomPass);
  composer.addPass(new OutputPass());
  const setView = (v) => { const V = typeof v === 'string' ? VIEWS[v] : v; camera.position.set(...V.pos); camera.fov = V.fov; camera.updateProjectionMatrix(); camera.lookAt(...V.target); camera.userData.target = new THREE.Vector3(...V.target); };
  const applyPreset = (p) => { const P = typeof p === 'string' ? PRESETS[p] : p; Object.keys(den.devices).forEach(n => den.setDevice(n, !!P[n])); };
  const render = () => composer.render();
  const resize = (w, h) => { camera.aspect = w / h; camera.updateProjectionMatrix(); renderer.setSize(w, h); composer.setSize(w, h); };
  const exportGLB = () => new Promise((res, rej) => {
    const hidden = []; den.root.traverse(o => { if (o.userData.noExport && o.visible) { o.visible = false; hidden.push(o); } });
    new GLTFExporter().parse(den.root, (buf) => { hidden.forEach(o => { o.visible = true; }); res(buf); }, (e) => { hidden.forEach(o => { o.visible = true; }); rej(e); }, { binary: true, onlyVisible: true, maxTextureSize: 1024 });
  });
  setView('dollhouse');
  return { renderer, scene, camera, composer, bloomPass, den, devices: den.devices, setDevice: den.setDevice, setView, applyPreset, render, resize, exportGLB };
}
