// Kitchen + dining + laundry — Room 02 of the dollhouse. Follows ../living-room/style.md.
// Coordinates: metres, Y up. Corner at origin. Kept walls: SINK WALL = plane x=0 (long, runs +z toward
// dining and laundry), COOKTOP WALL = plane z=0. Interior x<0, z>0. Handedness matches the photo
// (standing at the cooktop looking toward the dining, the sink wall is on your left).
import * as THREE from 'three';
import { STYLE, rng, canvasTex, makeBaseTextures, std, makeKit, createApp } from './common.js';

export const KROOM = { W: 4.6, KL: 5.2, DL: 3.0, LW: 2.6, LL: 2.3, H: 2.75 };

function kitchenTextures() {
  const quartz = canvasTex(1024, 512, (g, w, h) => {
    g.fillStyle = '#f2f0ec'; g.fillRect(0, 0, w, h);
    const r = rng(5);
    for (let i = 0; i < 14; i++) {
      g.strokeStyle = `rgba(150,150,155,${0.08 + r() * 0.18})`; g.lineWidth = 0.6 + r() * 2.2;
      g.beginPath(); let x = r() * w, y = r() * h; g.moveTo(x, y);
      for (let k = 0; k < 4; k++) { const nx = x + (r() - 0.3) * 420, ny = y + (r() - 0.5) * 260; g.quadraticCurveTo((x + nx) / 2 + (r() - 0.5) * 120, (y + ny) / 2 + (r() - 0.5) * 120, nx, ny); x = nx; y = ny; }
      g.stroke();
    }
  });
  const planks = canvasTex(512, 1024, (g, w, h) => {
    const r = rng(23), cols = 8, cw = w / cols;
    for (let c = 0; c < cols; c++) {
      let y = -r() * 600;
      while (y < h) { const len = 300 + r() * 500, v = 40 + r() * 22;
        g.fillStyle = `rgb(${v + 18},${v},${v - 12})`; g.fillRect(c * cw, y, cw, len);
        g.strokeStyle = 'rgba(0,0,0,0.08)'; g.lineWidth = 1;
        for (let k = 0; k < 4; k++) { g.beginPath(); const gx = c * cw + 4 + r() * (cw - 8); g.moveTo(gx, y); g.lineTo(gx + (r() - 0.5) * 6, y + len); g.stroke(); }
        g.fillStyle = '#1c130d'; g.fillRect(c * cw, y + len - 2, cw, 2); y += len; }
      g.fillStyle = '#1c130d'; g.fillRect(c * cw, 0, 2, h);
    }
  });
  planks.wrapS = planks.wrapT = THREE.RepeatWrapping;
  const tile = canvasTex(256, 256, (g, w, h) => { g.fillStyle = '#c4bfb5'; g.fillRect(0, 0, w, h); g.strokeStyle = '#a7a196'; g.lineWidth = 3; g.strokeRect(0, 0, w, h); });
  tile.wrapS = tile.wrapT = THREE.RepeatWrapping;
  const runner = canvasTex(256, 512, (g, w, h) => {
    g.fillStyle = '#b48a8c'; g.fillRect(0, 0, w, h);
    g.strokeStyle = '#7e5a5e'; g.lineWidth = 14; g.strokeRect(10, 10, w - 20, h - 20);
    g.strokeStyle = '#e3d3c6'; g.lineWidth = 4; g.strokeRect(24, 24, w - 48, h - 48);
    g.fillStyle = 'rgba(126,90,94,0.55)';
    for (let y = 70; y < h - 60; y += 64) { g.beginPath(); g.ellipse(w / 2, y, 52, 24, 0, 0, 7); g.fill(); }
    g.fillStyle = 'rgba(230,214,200,0.5)'; for (let y = 70; y < h - 60; y += 64) { g.beginPath(); g.ellipse(w / 2, y, 18, 9, 0, 0, 7); g.fill(); }
  });
  const morris = canvasTex(256, 320, (g, w, h) => {
    g.fillStyle = '#e8dfcc'; g.fillRect(0, 0, w, h);
    const r = rng(41);
    for (let i = 0; i < 70; i++) { g.fillStyle = ['#5d7350', '#7b8f5e', '#b4596a', '#8a3d4b', '#d9a85b', '#3f5a6b'][Math.floor(r() * 6)];
      g.beginPath(); g.ellipse(r() * w, 40 + r() * (h - 50), 6 + r() * 14, 4 + r() * 8, r() * 3, 0, 7); g.fill(); }
    g.fillStyle = '#3a3030'; g.font = 'bold 15px serif'; g.fillText('WILLIAM MORRIS', 52, 26);
  });
  const nookRug = canvasTex(512, 384, (g, w, h) => {
    const r = rng(77);
    g.fillStyle = '#5f222b'; g.fillRect(0, 0, w, h);
    for (let i = 0; i < 900; i++) { g.fillStyle = `rgba(${r() < 0.5 ? '40,14,20' : '180,120,110'},${0.05 + r() * 0.12})`; g.fillRect(r() * w, r() * h, 2 + r() * 10, 1 + r() * 3); }
    g.strokeStyle = '#4a1820'; g.lineWidth = 22; g.strokeRect(12, 12, w - 24, h - 24);
    g.strokeStyle = '#c99a86'; g.lineWidth = 3; g.strokeRect(30, 30, w - 60, h - 60); g.strokeRect(46, 46, w - 92, h - 92);
    g.fillStyle = 'rgba(200,150,130,0.35)';
    for (let i = 0; i < 26; i++) { g.beginPath(); g.ellipse(70 + r() * (w - 140), 70 + r() * (h - 140), 6 + r() * 16, 4 + r() * 8, r() * 3, 0, 7); g.fill(); }
    g.strokeStyle = 'rgba(210,170,150,0.55)'; g.lineWidth = 4; g.beginPath(); g.ellipse(w / 2, h / 2, 90, 60, 0, 0, 7); g.stroke();
    g.fillStyle = 'rgba(60,30,50,0.45)'; g.beginPath(); g.ellipse(w / 2, h / 2, 60, 38, 0, 0, 7); g.fill();
  });
  const sage = canvasTex(128, 128, (g, w, h) => {
    g.fillStyle = '#8c9474'; g.fillRect(0, 0, w, h); const r = rng(9);
    for (let i = 0; i < 40; i++) { g.fillStyle = r() < 0.5 ? '#5d6a4c' : '#c9c6a8'; g.beginPath(); g.ellipse(r() * w, r() * h, 3 + r() * 7, 2 + r() * 4, r() * 3, 0, 7); g.fill(); }
    g.strokeStyle = '#3d3a33'; g.lineWidth = 6; g.strokeRect(0, 0, w, h);
  });
  const ribs = canvasTex(256, 16, (g, w, h) => { for (let x = 0; x < w; x++) { const t = 0.5 + 0.5 * Math.cos((x / w) * Math.PI * 2 * 18); const v = Math.round(200 + 55 * t); g.fillStyle = `rgb(${v},${v},${v})`; g.fillRect(x, 0, 1, h); } });
  ribs.wrapS = THREE.RepeatWrapping;
  const fade = canvasTex(16, 256, (g, w, h) => { const gr = g.createLinearGradient(0, 0, 0, h); gr.addColorStop(0, 'rgba(12,9,8,0)'); gr.addColorStop(1, 'rgba(12,9,8,1)'); g.fillStyle = gr; g.fillRect(0, 0, w, h); });
  return { quartz, planks, tile, runner, morris, nookRug, sage, ribs, fade };
}

export function buildKitchen() {
  const T = { ...makeBaseTextures(), ...kitchenTextures() };
  const { W, KL, DL, LW, LL, H } = KROOM;
  const DZ0 = KL, DZ1 = KL + DL, LZ0 = DZ1 + 0.12, LZ1 = DZ1 + 0.12 + LL; // dining and laundry z-ranges
  const root = new THREE.Group(); root.name = 'kitchen';
  const K = makeKit(root, T);
  const { G, mesh, B, cyl, sphere, decal, lightMats, pl, register, portal, extMat, extMesh } = K;
  const M = {
    cut: std(STYLE.cut, 0.95), slab: std(STYLE.slab, 0.95),
    cab: std('#2b1f1a', 0.55), cabPanel: std('#231915', 0.6), cabDark: std('#16100d', 0.7),
    quartz: std('#ffffff', 0.25, 0, { map: T.quartz }),
    steel: std('#b9bdc1', 0.32, 0.85), steelDark: std('#7d8186', 0.35, 0.8), blackGlass: std('#111214', 0.08, 0.2), iron: std('#1b1b1b', 0.6, 0.4),
    wallK: std('#6a4a4f', 0.9), wallD: std('#86807a', 0.9), wallL: std('#dcd8d0', 0.9), whiteTrim: std('#efece6', 0.5),
    floor: std('#ffffff', 0.55, 0, { map: T.planks }), tile: std('#ffffff', 0.6, 0, { map: T.tile }),
    runner: std('#ffffff', 1, 0, { map: T.runner }), tableWood: std('#2e2019', 0.5), chairFabric: std('#d6cdbf', 0.95),
    faucet: std('#151515', 0.4, 0.3), applianceWhite: std('#eeece8', 0.35), panelGrey: std('#d4d2ce', 0.4), chrome: std('#cfd3d6', 0.2, 0.95),
    doorGlass: new THREE.MeshPhysicalMaterial({ color: '#a9bfd6', roughness: 0.05, metalness: 0, transparent: true, opacity: 0.1, depthWrite: false }),
    basket: std('#b08a5a', 0.95), shelfWhite: std('#f1efea', 0.6),
    upholstery: std('#ece4d4', 0.92), lightWood: std('#c99a62', 0.55), brass: std('#c09a52', 0.3, 0.85), burgundy: std('#6e1f2c', 0.8),
    blackPlate: std('#141414', 0.35, 0.2),
  };
  const arch = G('architecture'), cab = G('cabinetry'), app = G('appliances'), dining = G('dining_area'), laundry = G('laundry_room'),
    openings = G('doors_and_windows'), exterior = G('exterior_yard'), devGroup = G('devices');
  // local frames: COOK (z=0 wall): u=x, w=z.  SINK (x=0 wall): u=z, w=-x.
  const frame = (name, parent, rotY) => { const g = G(name, parent); g.rotation.y = rotY; return g; };
  const cookF = frame('cooktop_wall_run', cab, 0), sinkF = frame('sink_wall_run', cab, -Math.PI / 2);
  const sinkOpen = frame('sink_wall_openings', openings, -Math.PI / 2), lauF = frame('laundry_wall_run', laundry, -Math.PI / 2);

  const portalMat2 = new THREE.MeshBasicMaterial({ colorWrite: false, depthWrite: false, side: THREE.DoubleSide, stencilWrite: true, stencilRef: 2, stencilFunc: THREE.AlwaysStencilFunc, stencilZPass: THREE.ReplaceStencilOp });
  // ---------------- floor + walls
  const pk = (len, wid) => { const t = T.planks.clone(); t.needsUpdate = true; t.repeat.set(wid / 1.04, len / 2.08); return t; };
  B('floor_slab', arch, M.slab, -W, 0.22, -0.32, -0.02, -0.22, DZ1 + 0.12);
  mesh('floor_hardwood', new THREE.BoxGeometry(W, 0.02, DZ1 + 0.12), std('#ffffff', 0.55, 0, { map: pk(DZ1, W) }), arch, -W / 2, -0.01, (DZ1 + 0.12) / 2);
  B('laundry_floor_slab', laundry, M.slab, -LW, 0.22, -0.32, -0.02, LZ0 - 0.12, LZ1);
  const tt = T.tile.clone(); tt.needsUpdate = true; tt.repeat.set(LW / 0.3, LL / 0.3);
  mesh('laundry_floor_tile', new THREE.BoxGeometry(LW, 0.02, LL), std('#ffffff', 0.6, 0, { map: tt }), laundry, -LW / 2, -0.01, (LZ0 + LZ1) / 2);
  // cooktop wall z=0 (plain), extruded so its cut faces get the section colour
  const LIV = { x0: -4.3, x1: -3.25, h: 2.3 };   // cased opening into the living room
  { const sh = new THREE.Shape(); sh.moveTo(-W, 0); sh.lineTo(0.22, 0); sh.lineTo(0.22, H); sh.lineTo(-W, H); sh.lineTo(-W, 0);
    const hole = new THREE.Path(); hole.moveTo(LIV.x0, 0.003); hole.lineTo(LIV.x1, 0.003); hole.lineTo(LIV.x1, LIV.h); hole.lineTo(LIV.x0, LIV.h); hole.lineTo(LIV.x0, 0.003); sh.holes.push(hole);
    mesh('wall_cooktop', new THREE.ExtrudeGeometry(sh, { depth: 0.22, bevelEnabled: false }), [M.wallK, M.cut], arch, 0, 0, -0.22); }
  // sink wall x=0: three painted segments, with window + dining door holes
  const WIN = { u0: 1.55, u1: 2.3, v0: 1.06, v1: 2.38 }, DOOR = { u0: 4.84, u1: 5.72, h: 2.45 };
  const archWin = (inset) => { const r = (WIN.u1 - WIN.u0) / 2, cz = (WIN.u0 + WIN.u1) / 2, sy = WIN.v1 - r, s = new THREE.Shape();
    s.moveTo(WIN.u0 + inset, WIN.v0 + inset); s.lineTo(WIN.u1 - inset, WIN.v0 + inset); s.lineTo(WIN.u1 - inset, sy); s.absarc(cz, sy, r - inset, 0, Math.PI, false); s.lineTo(WIN.u0 + inset, WIN.v0 + inset); return s; };
  const wallSeg = (name, z0, z1, mat, holes = []) => {
    const sh = new THREE.Shape(); sh.moveTo(z0, 0); sh.lineTo(z1, 0); sh.lineTo(z1, H); sh.lineTo(z0, H); sh.lineTo(z0, 0); holes.forEach(h => sh.holes.push(h));
    const m = mesh(name, new THREE.ExtrudeGeometry(sh, { depth: 0.22, bevelEnabled: false, curveSegments: 24 }), [mat, M.cut], arch, 0.22, 0, 0); m.rotation.y = -Math.PI / 2; return m;
  };
  const rectPath = (u0, u1, v0, v1) => { const p = new THREE.Path(); p.moveTo(u0, v0); p.lineTo(u0, v1); p.lineTo(u1, v1); p.lineTo(u1, v0); p.lineTo(u0, v0); return p; };
  const DW0 = 4.62; // greige starts at the fridge tower
  wallSeg('wall_sink_kitchen', 0, DW0, M.wallK, [archWin(0)]);
  const BAY = { z0: 6.0, z1: 8.0, d: 0.5, h: 2.3, knee: 0.3 };  // three-panel bay window behind the banquette
  wallSeg('wall_sink_dining', DW0, DZ1, M.wallD, [rectPath(DOOR.u0, DOOR.u1, 0.003, DOOR.h), rectPath(BAY.z0, BAY.z1, 0.003, BAY.h)]);
  wallSeg('wall_sink_laundry', DZ1, LZ1, M.wallL);
  // low partition between dining and laundry (interior partitions facing the camera are cut at 1.1 m)
  const PH = 1.1;
  const LDX = -1.98; // laundry doorway x -2.6..LDX (to the right of the booth)
  B('laundry_partition', laundry, M.wallL, LDX, 0, 0, PH, DZ1, DZ1 + 0.12).material = [M.wallL, M.cut, M.cut, M.wallL, M.wallL, M.wallD];
  // full-height nook back wall: only shown in interior camera views (dollhouse views keep the 1.1 m cut)
  { const up = B('nook_back_wall_upper', dining, M.wallD, -LW, 0, PH, H, DZ1, DZ1 + 0.12); up.userData.interiorOnly = true;
    const cr = B('crown_nook_back', dining, M.whiteTrim, -LW, 0, 2.63, 2.75, DZ1 - 0.09, DZ1, 0.01); cr.userData.interiorOnly = true; }
  B('baseboard_nook_back', dining, M.whiteTrim, LDX, 0, 0, 0.12, DZ1 - 0.02, DZ1);
  // white trim in dining + laundry
  B('baseboard_dining', dining, M.whiteTrim, -0.02, 0, 0, 0.12, DW0, DOOR.u0 - 0.1);
  B('baseboard_dining_2', dining, M.whiteTrim, -0.02, 0, 0, 0.12, DOOR.u1 + 0.1, BAY.z0);
  B('baseboard_dining_3', dining, M.whiteTrim, -0.02, 0, 0, 0.12, BAY.z1, DZ1);
  B('crown_dining', dining, M.whiteTrim, -0.09, 0, 2.63, 2.75, DW0, DZ1, 0.01);
  B('baseboard_laundry', laundry, M.whiteTrim, -0.02, 0, 0, 0.1, LZ0, LZ1);
  // tray ceiling: low ring cut back to a band along the sink wall, riser up to the tray, and one cut beam for the pendant
  B('tray_ceiling_band', dining, M.cut, -0.55, 0, 2.75, 2.87, DZ0, DZ1);
  B('tray_ceiling_riser', dining, M.cut, -0.6, -0.55, 2.75, 3.08, DZ0, DZ1);
  B('tray_ceiling_band_soffit', dining, std('#f3f0ea', 0.9), -0.55, 0, 2.735, 2.75, DZ0, DZ1);
  const TBL = { x: -1.3, z: 6.95 };
  B('tray_ceiling_beam', dining, M.cut, -2.45, -0.6, 2.96, 3.08, TBL.z - 0.07, TBL.z + 0.07);

  // ---------------- cabinet kit (local frame: u along wall, v up, w out from wall)
  const D = 0.6;
  const pull = (name, parent, horiz, uc, vc, w, len = 0.22) => horiz ? B(name, parent, M.steel, uc - len / 2, uc + len / 2, vc - 0.007, vc + 0.007, w, w + 0.028, 0.006) : B(name, parent, M.steel, uc - 0.007, uc + 0.007, vc - len / 2, vc + len / 2, w, w + 0.028, 0.006);
  const shaker = (name, parent, u0, u1, v0, v1, w, pullCfg) => {
    const s = 0.055, t = 0.02;
    if (v1 - v0 < 0.2) B(`${name}`, parent, M.cab, u0, u1, v0, v1, w, w + t, 0.004);
    else { B(`${name}_stile_l`, parent, M.cab, u0, u0 + s, v0, v1, w, w + t, 0.003); B(`${name}_stile_r`, parent, M.cab, u1 - s, u1, v0, v1, w, w + t, 0.003);
      B(`${name}_rail_t`, parent, M.cab, u0 + s, u1 - s, v1 - s, v1, w, w + t, 0.003); B(`${name}_rail_b`, parent, M.cab, u0 + s, u1 - s, v0, v0 + s, w, w + t, 0.003);
      B(`${name}_panel`, parent, M.cabPanel, u0 + s, u1 - s, v0 + s, v1 - s, w, w + t * 0.45); }
    if (pullCfg) { const [kind, side] = pullCfg; if (kind === 'h') pull(`${name}_pull`, parent, true, (u0 + u1) / 2, v1 - Math.min(0.06, (v1 - v0) / 2), w + t, Math.min(0.3, (u1 - u0) * 0.5));
      else pull(`${name}_pull`, parent, false, side > 0 ? u1 - 0.045 : u0 + 0.045, v1 - 0.16, w + t, 0.2); }
  };
  const base = (name, parent, u0, u1, kind) => {
    const g = G(name, parent), f = D - 0.02;
    B(`${name}_box`, g, M.cab, u0, u1, 0.1, 0.88, 0, f); B(`${name}_toekick`, g, M.cabDark, u0, u1, 0, 0.1, 0, D - 0.08);
    const n = (p) => `${name}_${p}`, wdt = u1 - u0;
    if (kind === 'drawers') [[0.12, 0.38], [0.40, 0.62], [0.64, 0.86]].forEach(([a, b], i) => shaker(n(`drawer_${i + 1}`), g, u0 + 0.004, u1 - 0.004, a, b, f, ['h']));
    else if (kind === 'cooktop') [[0.12, 0.48], [0.50, 0.86]].forEach(([a, b], i) => shaker(n(`drawer_${i + 1}`), g, u0 + 0.004, u1 - 0.004, a, b, f, ['h']));
    else if (kind === 'dw') { B(n('dishwasher_panel'), g, M.steel, u0 + 0.004, u1 - 0.004, 0.1, 0.86, f, f + 0.02, 0.006); pull(n('dishwasher_handle'), g, true, (u0 + u1) / 2, 0.8, f + 0.02, wdt * 0.7); }
    else { const two = wdt > 0.55; const dv1 = kind === 'sink' ? 0.7 : 0.7;
      if (two) { shaker(n('door_l'), g, u0 + 0.004, (u0 + u1) / 2 - 0.002, 0.12, dv1, f, ['v', 1]); shaker(n('door_r'), g, (u0 + u1) / 2 + 0.002, u1 - 0.004, 0.12, dv1, f, ['v', -1]); }
      else shaker(n('door'), g, u0 + 0.004, u1 - 0.004, 0.12, dv1, f, ['v', 1]);
      shaker(n(kind === 'sink' ? 'false_front' : 'top_drawer'), g, u0 + 0.004, u1 - 0.004, 0.72, 0.86, f, kind === 'sink' ? null : ['h']); }
    return g;
  };
  const counter = (name, parent, u0, u1, w1 = 0.64) => B(name, parent, M.quartz, u0, u1, 0.88, 0.92, 0, w1, 0.006);
  const upper = (name, parent, u0, u1, v0 = 1.42, v1 = 2.48) => {
    const g = G(name, parent);
    B(`${name}_box`, g, M.cab, u0, u1, v0, v1, 0, 0.33);
    const nd = Math.max(1, Math.round((u1 - u0) / 0.46)), dw = (u1 - u0) / nd;
    for (let i = 0; i < nd; i++) shaker(`${name}_door_${i + 1}`, g, u0 + i * dw + 0.003, u0 + (i + 1) * dw - 0.003, v0 + 0.01, v1 - 0.01, 0.33, ['v', i % 2 ? -1 : 1]);
    B(`${name}_crown`, g, M.cab, u0 - 0.01, u1 + 0.01, v1, v1 + 0.1, 0, 0.39, 0.008);
    B(`${name}_light_rail`, g, M.cab, u0, u1, v0 - 0.035, v0, 0.31, 0.35);
    return g;
  };
  const tall = (name, parent, u0, u1, layout) => {
    const g = G(name, parent), f = 0.6;
    B(`${name}_box`, g, M.cab, u0, u1, 0.1, 2.48, 0, f); B(`${name}_toekick`, g, M.cabDark, u0, u1, 0, 0.1, 0, f - 0.06);
    B(`${name}_crown`, g, M.cab, u0 - 0.01, u1 + 0.01, 2.48, 2.58, 0, f + 0.06, 0.008);
    layout(g, f); return g;
  };

  // ---------------- cooktop wall (z=0)  u = x
  base('cooktop_run_corner', cookF, -0.62, 0, 'doors');
  base('cooktop_run_drawers', cookF, -1.27, -0.62, 'drawers');
  base('cooktop_run_range_base', cookF, -2.07, -1.27, 'cooktop');
  base('cooktop_run_end', cookF, -2.62, -2.07, 'doors');
  counter('cooktop_run_counter', cookF, -2.64, 0);
  const CK = -1.67;
  { const g = G('gas_cooktop', app);
    B('gas_cooktop_glass', g, M.iron, CK - 0.38, CK + 0.38, 0.92, 0.935, 0.07, 0.6, 0.008);
    [[-0.25, 0.2], [0.25, 0.2], [-0.25, 0.47], [0.25, 0.47], [0, 0.335]].forEach(([du, w], i) => {
      cyl(`gas_cooktop_burner_${i + 1}`, g, M.steelDark, 0.045, 0.05, 0.02, CK + du, 0.945, w, 16);
      const grate = mesh(`gas_cooktop_grate_${i + 1}`, new THREE.TorusGeometry(0.1, 0.008, 4, 4), M.iron, g, CK + du, 0.958, w); grate.rotation.x = Math.PI / 2; grate.rotation.z = Math.PI / 4; });
    for (let i = 0; i < 5; i++) cyl(`gas_cooktop_knob_${i + 1}`, g, M.steel, 0.02, 0.02, 0.025, CK - 0.3 + i * 0.15, 0.81, 0.6, 12).rotation.x = Math.PI / 2;
  }
  upper('cooktop_upper_left', cookF, -1.25, 0);
  upper('cooktop_upper_right', cookF, -2.62, -2.09);
  // range hood: stainless chimney hood
  { const g = G('range_hood', app);
    const can = mesh('range_hood_canopy', new THREE.CylinderGeometry(0.3, 0.52, 0.28, 4, 1), M.steel, g, CK, 1.72, 0.29); can.rotation.y = Math.PI / 4; can.scale.set(1.05, 1, 0.72);
    B('range_hood_rim', g, M.steel, CK - 0.4, CK + 0.4, 1.54, 1.59, 0.02, 0.54, 0.008);
    B('range_hood_chimney', g, M.steel, CK - 0.15, CK + 0.15, 1.86, H, 0.02, 0.3, 0.006);
  }
  // backsplash (white quartz)
  B('backsplash_cooktop_wall', cookF, M.quartz, -2.64, 0, 0.92, 1.42, 0, 0.012);
  B('backsplash_behind_hood', cookF, M.quartz, -2.09, -1.25, 1.42, 2.6, 0, 0.012);
  // cased opening into the living room (white casing, dark room beyond seen only through the opening)
  { const g = G('living_room_opening', openings), { x0, x1, h } = LIV;
    B('living_opening_jamb_l', g, M.whiteTrim, x0, x0 + 0.02, 0, h, -0.22, 0); B('living_opening_jamb_r', g, M.whiteTrim, x1 - 0.02, x1, 0, h, -0.22, 0);
    B('living_opening_jamb_head', g, M.whiteTrim, x0, x1, h - 0.02, h, -0.22, 0);
    B('living_opening_casing_l', g, M.whiteTrim, x0 - 0.09, x0 + 0.005, 0, h + 0.09, 0, 0.025); B('living_opening_casing_r', g, M.whiteTrim, x1 - 0.005, x1 + 0.09, 0, h + 0.09, 0, 0.025);
    B('living_opening_casing_head', g, M.whiteTrim, x0 - 0.11, x1 + 0.11, h, h + 0.12, 0, 0.03, 0.005);
    B('living_opening_plinth_l', g, M.whiteTrim, x0 - 0.1, x0 + 0.005, 0, 0.14, 0, 0.035); B('living_opening_plinth_r', g, M.whiteTrim, x1 - 0.005, x1 + 0.1, 0, 0.14, 0, 0.035);
    B('baseboard_cooktop_wall_end', g, M.whiteTrim, -W, x0 - 0.1, 0, 0.12, 0, 0.02); B('baseboard_cooktop_wall_mid', g, M.whiteTrim, x1 + 0.1, -2.64, 0, 0.12, 0, 0.02);
    const p = portal('portal_living_opening', g, new THREE.PlaneGeometry(x1 - x0 - 0.04, h - 0.03)); p.position.set((x0 + x1) / 2, (h - 0.03) / 2, -0.12);
    const lv = G('living_room_glimpse', exterior), cx = (x0 + x1) / 2;
    const pf = T.planks.clone(); pf.needsUpdate = true; pf.repeat.set(3 / 1.04, 4 / 2.08);
    const fl = extMesh(mesh('living_floor', new THREE.PlaneGeometry(3, 4), extMat('#e8d6c4', { map: pf }), lv, cx, 0.0, -2.2, false)); fl.rotation.x = -Math.PI / 2;
    const fd = extMesh(mesh('living_floor_fade', new THREE.PlaneGeometry(3, 4), extMat('#ffffff', { map: T.fade, transparent: true, depthWrite: false }), lv, cx, 0.004, -2.2, false)); fd.rotation.x = -Math.PI / 2; fd.renderOrder = 3;
    extMesh(B('living_wall_l', lv, extMat('#2b2226'), x0 - 0.6, x0 - 0.55, 0, 3, -4.2, -0.22)); extMesh(B('living_wall_r', lv, extMat('#2b2226'), x1 + 0.55, x1 + 0.6, 0, 3, -4.2, -0.22));
    extMesh(B('living_baseboard_l', lv, extMat('#5d5650'), x0 - 0.55, x0 - 0.53, 0, 0.12, -2.2, -0.22)); extMesh(B('living_baseboard_r', lv, extMat('#5d5650'), x1 + 0.53, x1 + 0.55, 0, 0.12, -2.2, -0.22));
    extMesh(B('living_backdrop', lv, extMat('#0c0908'), x0 - 0.6, x1 + 0.6, 0, 3, -4.25, -4.2));
  }

  // ---------------- sink wall (x=0)  u = z, w = -x
  base('sink_run_drawers_a', sinkF, 0.62, 1.45, 'drawers');
  base('sink_run_sink_base', sinkF, 1.45, 2.4, 'sink');
  base('sink_run_dishwasher', sinkF, 2.4, 3.0, 'dw');
  base('sink_run_drawers_b', sinkF, 3.0, 3.62, 'drawers');
  counter('sink_run_counter', sinkF, 0.64, 3.64);
  const SC = (WIN.u0 + WIN.u1) / 2;
  { const g = G('sink', sinkF);
    B('sink_basin', g, std('#2a2a2c', 0.45, 0.3), SC - 0.38, SC + 0.38, 0.921, 0.925, 0.12, 0.52, 0.01);
    B('sink_basin_inner', g, std('#1c1c1e', 0.5, 0.3), SC - 0.35, SC + 0.35, 0.922, 0.926, 0.14, 0.5, 0.01);
    const fc = new THREE.CatmullRomCurve3([[0, 0.92, 0.07], [0, 1.2, 0.07], [0, 1.3, 0.16], [0, 1.22, 0.3]].map(p => new THREE.Vector3(SC + p[0], p[1], p[2])));
    mesh('sink_faucet', new THREE.TubeGeometry(fc, 24, 0.014, 10), M.faucet, g);
    cyl('sink_faucet_base', g, M.faucet, 0.03, 0.035, 0.03, SC, 0.935, 0.07, 14);
  }
  upper('sink_upper_left', sinkF, 0.35, 1.45);
  upper('sink_upper_right', sinkF, 2.4, 3.62);
  B('backsplash_sink_wall_a', sinkF, M.quartz, 0.62, WIN.u0, 0.92, 1.42, 0, 0.012);
  B('backsplash_sink_wall_b', sinkF, M.quartz, WIN.u1, 3.64, 0.92, 1.42, 0, 0.012);
  B('backsplash_under_window', sinkF, M.quartz, WIN.u0, WIN.u1, 0.92, WIN.v0, 0, 0.012);
  B('window_sill_quartz', sinkF, M.quartz, WIN.u0 - 0.05, WIN.u1 + 0.05, WIN.v0 - 0.02, WIN.v0 + 0.01, -0.08, 0.06, 0.004);
  // fridge set into tall cabinetry
  { const g = G('fridge_tower', sinkF), fz0 = 3.66, fz1 = 4.56;
    B('fridge_tower_panel_l', g, M.cab, 3.62, 3.67, 0, 2.48, 0, 0.7, 0.004); B('fridge_tower_panel_r', g, M.cab, 4.56, 4.62, 0, 2.48, 0, 0.7, 0.004);
    B('fridge_tower_upper_box', g, M.cab, 3.67, 4.56, 1.86, 2.48, 0, 0.62);
    shaker('fridge_tower_upper_door_l', g, 3.672, 4.112, 1.87, 2.46, 0.62, ['v', 1]); shaker('fridge_tower_upper_door_r', g, 4.118, 4.556, 1.87, 2.46, 0.62, ['v', -1]);
    B('fridge_tower_crown', g, M.cab, 3.61, 4.63, 2.48, 2.58, 0, 0.76, 0.008);
    const fg = G('fridge', app); fg.rotation.y = -Math.PI / 2;
    B('fridge_body', fg, M.steelDark, fz0, fz1, 0.02, 1.82, 0.02, 0.66, 0.01);
    B('fridge_door_left', fg, M.steel, fz0 + 0.005, (fz0 + fz1) / 2 - 0.004, 0.86, 1.82, 0.66, 0.72, 0.012);
    B('fridge_door_right', fg, M.steel, (fz0 + fz1) / 2 + 0.004, fz1 - 0.005, 0.86, 1.82, 0.66, 0.72, 0.012);
    B('fridge_drawer_upper', fg, M.steel, fz0 + 0.005, fz1 - 0.005, 0.46, 0.84, 0.66, 0.72, 0.012);
    B('fridge_drawer_lower', fg, M.steel, fz0 + 0.005, fz1 - 0.005, 0.03, 0.44, 0.66, 0.72, 0.012);
    for (const [n, u, v0, v1] of [['left', (fz0 + fz1) / 2 - 0.05, 1.0, 1.7], ['right', (fz0 + fz1) / 2 + 0.05, 1.0, 1.7]]) B(`fridge_handle_${n}`, fg, M.chrome, u - 0.01, u + 0.01, v0, v1, 0.73, 0.76, 0.008);
    B('fridge_handle_drawer_upper', fg, M.chrome, fz0 + 0.2, fz1 - 0.2, 0.78, 0.8, 0.73, 0.76, 0.008);
    B('fridge_handle_drawer_lower', fg, M.chrome, fz0 + 0.2, fz1 - 0.2, 0.38, 0.4, 0.73, 0.76, 0.008);
    B('fridge_display', fg, std('#1d2a33', 0.2), (fz0 + fz1) / 2 + 0.12, (fz0 + fz1) / 2 + 0.3, 1.45, 1.6, 0.72, 0.725);
  }
  // arched window over the sink (white frame, muntins)
  { const g = G('arched_window_sink', sinkOpen), r = (WIN.u1 - WIN.u0) / 2, cz = SC, sy = WIN.v1 - r;
    const frame = archWin(0); frame.holes.push(archWin(0.06));
    mesh('arched_window_sink_frame', new THREE.ExtrudeGeometry(frame, { depth: 0.07, bevelEnabled: false, curveSegments: 32 }), M.whiteTrim, g, 0, 0, -0.12);
    const cs = archWin(-0.08); cs.holes.push(archWin(0));
    mesh('arched_window_sink_casing', new THREE.ExtrudeGeometry(cs, { depth: 0.025, bevelEnabled: false, curveSegments: 32 }), M.whiteTrim, g, 0, 0, 0);
    mesh('arched_window_sink_glass', new THREE.ShapeGeometry(archWin(0.05), 32), M.doorGlass, g, 0, 0, -0.085, false);
    const wa = -0.1, wb = -0.06;
    B('arched_window_sink_muntin_v', g, M.whiteTrim, cz - 0.01, cz + 0.01, WIN.v0 + 0.06, WIN.v1 - 0.06, wa, wb);
    for (let k = 1; k <= 3; k++) { const v = WIN.v0 + 0.06 + (sy - WIN.v0 - 0.06) * k / 3; B(`arched_window_sink_muntin_h_${k}`, g, M.whiteTrim, WIN.u0 + 0.06, WIN.u1 - 0.06, v - 0.01, v + 0.01, wa, wb); }
    for (const a of [Math.PI / 4, (3 * Math.PI) / 4]) { const m = B(`arched_window_sink_muntin_r_${a.toFixed(2)}`, g, M.whiteTrim, 0, r - 0.06, -0.01, 0.01, wa, wb); m.position.set(cz + Math.cos(a) * (r - 0.06) / 2, sy + Math.sin(a) * (r - 0.06) / 2, (wa + wb) / 2); m.rotation.z = a; }
    const p = portal('portal_sink_window', g, new THREE.ShapeGeometry(archWin(0.05), 32)); p.position.z = -0.16;
  }
  // dining glass door (white frame, 3x5 lites + transom)
  { const g = G('dining_glass_door', sinkOpen), { u0, u1, h } = DOOR, top = 2.08, wa = -0.11, wb = -0.06;
    B('dining_glass_door_jamb_l', g, M.whiteTrim, u0, u0 + 0.03, 0, h, -0.22, 0.01); B('dining_glass_door_jamb_r', g, M.whiteTrim, u1 - 0.03, u1, 0, h, -0.22, 0.01);
    B('dining_glass_door_jamb_head', g, M.whiteTrim, u0, u1, h - 0.04, h, -0.22, 0.01);
    B('dining_glass_door_casing_l', g, M.whiteTrim, u0 - 0.09, u0 + 0.005, 0, h + 0.09, 0, 0.025); B('dining_glass_door_casing_r', g, M.whiteTrim, u1 - 0.005, u1 + 0.09, 0, h + 0.09, 0, 0.025);
    B('dining_glass_door_casing_head', g, M.whiteTrim, u0 - 0.11, u1 + 0.11, h, h + 0.12, 0, 0.03, 0.005);
    const a0 = u0 + 0.03, a1 = u1 - 0.03;
    B('dining_glass_door_stile_a', g, M.whiteTrim, a0, a0 + 0.08, 0.02, top, wa, wb); B('dining_glass_door_stile_b', g, M.whiteTrim, a1 - 0.08, a1, 0.02, top, wa, wb);
    B('dining_glass_door_rail_bottom', g, M.whiteTrim, a0, a1, 0.02, 0.24, wa, wb); B('dining_glass_door_rail_top', g, M.whiteTrim, a0, a1, top - 0.08, top, wa, wb);
    B('dining_glass_door_pane', g, M.doorGlass, a0 + 0.08, a1 - 0.08, 0.24, top - 0.08, -0.088, -0.082).castShadow = false;
    for (const k of [1, 2]) { const u = a0 + 0.08 + (a1 - a0 - 0.16) * k / 3; B(`dining_glass_door_muntin_v_${k}`, g, M.whiteTrim, u - 0.01, u + 0.01, 0.24, top - 0.08, wa + 0.01, wb - 0.01); }
    for (let k = 1; k < 5; k++) { const v = 0.24 + (top - 0.32) * k / 5; B(`dining_glass_door_muntin_h_${k}`, g, M.whiteTrim, a0 + 0.08, a1 - 0.08, v - 0.01, v + 0.01, wa + 0.01, wb - 0.01); }
    B('dining_glass_door_transom_bar', g, M.whiteTrim, a0, a1, top, top + 0.05, wa, wb);
    B('dining_glass_door_transom_pane', g, M.doorGlass, a0, a1, top + 0.05, h - 0.04, -0.088, -0.082).castShadow = false;
    B('dining_glass_door_lever', g, M.chrome, a1 - 0.07, a1 - 0.05, 1.0, 1.02, 0.0, 0.05, 0.005);
    const p = portal('portal_dining_door', g, new THREE.PlaneGeometry(a1 - a0, h - 0.06)); p.position.set((a0 + a1) / 2, (h - 0.06) / 2 + 0.02, -0.15); p.material = portalMat2;
  }
  // ---------------- bay window (three angled panels on a 0.3 m knee wall, projecting outward from the dining wall)
  const sunMat = (color, opts = {}) => extMat(color, { stencilRef: 2, ...opts });
  { const g = G('bay_window', openings), X0 = 0.22, { z0, z1, d, h, knee } = BAY, T2 = 0.12;
    const wallTop = [M.wallD, M.wallD, M.cut, M.wallD, M.wallD, M.wallD];
    const pts = [[X0, z0], [X0 + d, z0 + d], [X0 + d, z1 - d], [X0, z1]];
    for (let i = 0; i < 3; i++) {
      const [ax, az] = pts[i], [bx, bz] = pts[i + 1], len = Math.hypot(bx - ax, bz - az), n = ['left', 'centre', 'right'][i];
      const pg = G(`bay_panel_${n}`, g); pg.position.set(ax, 0, az); pg.rotation.y = Math.atan2(-(bz - az), bx - ax);   // local u along the panel, +w = room side
      B(`bay_knee_wall_${n}`, pg, M.wallD, 0, len, 0, knee, -T2, 0).material = wallTop;
      B(`bay_sill_${n}`, pg, M.whiteTrim, -0.01, len + 0.01, knee, knee + 0.035, -T2, 0.05, 0.004);
      B(`bay_header_${n}`, pg, M.wallD, 0, len, h, H, -T2, 0).material = wallTop;
      const fw = 0.055, wa = -0.08, wb = -0.03, v0 = knee + 0.035;
      B(`bay_frame_${n}_l`, pg, M.whiteTrim, 0, fw, v0, h, wa, wb); B(`bay_frame_${n}_r`, pg, M.whiteTrim, len - fw, len, v0, h, wa, wb);
      B(`bay_frame_${n}_head`, pg, M.whiteTrim, 0, len, h - fw, h, wa, wb); B(`bay_frame_${n}_bottom`, pg, M.whiteTrim, 0, len, v0, v0 + fw, wa, wb);
      B(`bay_transom_bar_${n}`, pg, M.whiteTrim, fw, len - fw, 1.86, 1.9, wa, wb);
      if (len > 0.9) B(`bay_mullion_${n}`, pg, M.whiteTrim, len / 2 - 0.025, len / 2 + 0.025, v0, h, wa, wb);
      B(`bay_casing_${n}_head`, pg, M.whiteTrim, -0.02, len + 0.02, h, h + 0.08, -0.03, 0.0);
      const gl = mesh(`bay_glass_${n}`, new THREE.PlaneGeometry(len - 2 * fw, h - v0 - 2 * fw), M.doorGlass, pg, len / 2, (v0 + h) / 2, -0.06, false); gl.castShadow = false;
      const pp = portal(`portal_bay_${n}`, pg, new THREE.PlaneGeometry(len - 2 * fw, h - v0 - 2 * fw)); pp.position.set(len / 2, (v0 + h) / 2, -0.09);
    }
    for (const [k, [x, z]] of [[1, pts[1]], [2, pts[2]]]) { const post = cyl(`bay_corner_post_${k}`, g, M.wallD, 0.07, 0.07, H, x - 0.02, H / 2, z, 12); post.material = [M.wallD, M.cut, M.cut];
      B(`bay_corner_trim_${k}`, g, M.whiteTrim, x - 0.075, x + 0.015, knee, h, z - 0.045, z + 0.045, 0.01); }
    // reveals through the wall and the bay floor
    B('bay_reveal_a', g, M.wallD, 0, X0, 0, h, z0, z0 + 0.01); B('bay_reveal_b', g, M.wallD, 0, X0, 0, h, z1 - 0.01, z1);
    B('bay_soffit', g, M.whiteTrim, 0, X0, h - 0.01, h, z0, z1);
    const fs = new THREE.Shape([[0, z0], [X0, z0], [X0 + d, z0 + d], [X0 + d, z1 - d], [X0, z1], [0, z1]].map(([x, z]) => new THREE.Vector2(x, z)));
    const bt = T.planks.clone(); bt.needsUpdate = true; bt.repeat.set(1 / 1.04, 1 / 2.08);
    const bf = mesh('bay_floor', new THREE.ExtrudeGeometry(fs, { depth: 0.32, bevelEnabled: false }), [std('#ffffff', 0.55, 0, { map: bt }), M.cut], arch, 0, 0, 0); bf.rotation.x = Math.PI / 2;
  }
  // sunroom glimpse beyond the glass door (stencil ref 2: only visible through the door glass)
  { const sr = G('sunroom_glimpse', exterior), Z0 = 3.0, Z1 = 6.0, X1 = 4.2;
    const SM = (n, mat, ...a) => extMesh(B(n, sr, mat, ...a));
    const st = T.tile.clone(); st.needsUpdate = true; st.repeat.set((X1 - 0.22) / 0.45, (Z1 - Z0) / 0.45);
    const fl = extMesh(mesh('sunroom_floor', new THREE.PlaneGeometry(X1 - 0.22, Z1 - Z0), sunMat('#d9d2c6', { map: st }), sr, (X1 + 0.22) / 2, 0.0, (Z0 + Z1) / 2, false)); fl.rotation.x = -Math.PI / 2;
    const moon = extMesh(mesh('sunroom_moonlight', new THREE.PlaneGeometry(2.6, 2.0), sunMat('#9fb6e0', { map: T.glow, transparent: true, opacity: 0.55, blending: THREE.AdditiveBlending, depthWrite: false }), sr, 2.0, 0.006, 4.6, false));
    moon.rotation.x = -Math.PI / 2; moon.renderOrder = 3;
    const sky = extMesh(mesh('sunroom_night_sky', new THREE.PlaneGeometry(12, 6), sunMat('#ffffff', { map: T.sky }), sr, X1 + 0.6, 2.4, 4.5, false)); sky.rotation.y = -Math.PI / 2; sky.userData.noExport = true;
    SM('sunroom_knee_wall_far', sunMat('#cfc7ba'), X1 - 0.1, X1, 0, 0.5, Z0, Z1);
    for (let k = 0; k <= 5; k++) { const z = Z0 + (Z1 - Z0) * k / 5; SM(`sunroom_mullion_${k}`, sunMat('#eeebe4'), X1 - 0.08, X1 - 0.02, 0.5, 2.6, z - 0.03, z + 0.03); }
    SM('sunroom_window_head', sunMat('#eeebe4'), X1 - 0.08, X1 - 0.02, 2.55, 2.65, Z0, Z1); SM('sunroom_window_sill', sunMat('#eeebe4'), X1 - 0.12, X1, 0.5, 0.54, Z0, Z1);
    SM('sunroom_side_wall', sunMat('#c9c0b2'), 0.22, X1, 0, 2.8, Z0 - 0.1, Z0);
    SM('sunroom_wicker_chair_seat', sunMat('#8a6f52'), 2.3, 2.9, 0.35, 0.45, 3.6, 4.2); SM('sunroom_wicker_chair_back', sunMat('#7a6248'), 2.8, 2.9, 0.45, 0.95, 3.6, 4.2);
    SM('sunroom_cushion', sunMat('#e6e0d4'), 2.33, 2.8, 0.45, 0.5, 3.63, 4.17);
    SM('sunroom_planter', sunMat('#6d5a4a'), 3.6, 3.85, 0, 0.35, 5.4, 5.65); extMesh(sphere('sunroom_plant', sr, sunMat('#4f6b4a'), 0.28, 3.72, 0.6, 5.52));
  }

  // ---------------- island (long axis along z)
  const ISX = -2.5, IZ0 = 1.72, IZ1 = 4.32, IHW = 0.48;
  const isl = G('island', cab); isl.position.set(ISX, 0, 0); isl.rotation.y = -Math.PI / 2;   // local u=z, w=-(x-ISX)
  B('island_box', isl, M.cab, IZ0, IZ1, 0.1, 0.88, -IHW, IHW); B('island_toekick', isl, M.cabDark, IZ0 + 0.06, IZ1 - 0.06, 0, 0.1, -IHW + 0.06, IHW - 0.06);
  B('island_top', isl, M.quartz, IZ0 - 0.04, IZ1 + 0.04, 0.88, 0.92, -IHW - 0.05, IHW + 0.05, 0.006);
  { const n = 4, dw = (IZ1 - IZ0) / n;
    for (let i = 0; i < n; i++) { const u0 = IZ0 + i * dw + 0.004, u1 = IZ0 + (i + 1) * dw - 0.004;
      shaker(`island_room_side_panel_${i + 1}`, isl, u0, u1, 0.12, 0.86, IHW, null);
      if (i === n - 1) continue;
      shaker(`island_sink_side_panel_${i + 1}`, isl, u0, u1, 0.12, 0.86, -IHW - 0.02, null); }
    // microwave drawer (sink side, dining end) with a cabinet door under it, as in the nook photo
    { const u0 = IZ1 - dw + 0.004, u1 = IZ1 - 0.004, w = -IHW - 0.02;
      B('island_microwave_drawer_frame', isl, M.steel, u0, u1, 0.5, 0.86, w - 0.025, w, 0.006);
      B('island_microwave_drawer_glass', isl, M.blackGlass, u0 + 0.06, u1 - 0.06, 0.53, 0.74, w - 0.03, w - 0.024);
      B('island_microwave_drawer_controls', isl, M.blackGlass, u0 + 0.06, u1 - 0.06, 0.77, 0.83, w - 0.03, w - 0.024);
      B('island_microwave_drawer_handle', isl, M.chrome, u0 + 0.08, u1 - 0.08, 0.755, 0.765, w - 0.05, w - 0.025, 0.004);
      shaker('island_microwave_base_door', isl, u0, u1, 0.12, 0.48, -IHW - 0.02, ['v', 1]);  }
    for (const [nm, u0, u1] of [['island_end_a', IZ0 - 0.02, IZ0], ['island_end_b', IZ1, IZ1 + 0.02]]) B(nm, isl, M.cab, u0, u1, 0, 0.88, -IHW, IHW, 0.004);
  }
  const OVT = { u0: -IZ1, u1: -(IZ1 - 0.78) };   // ghost-wall frame: u = -z, w = +x from x = -W
  const ghostF = G('ghost_wall_run', cab); ghostF.position.x = -W; ghostF.rotation.y = Math.PI / 2;
  // Dollhouse views cut the tower at OCUT (cream cap), like the other camera-side walls; interior views show it full height.
  { const g = G('oven_tower', ghostF), a = OVT.u0, b = OVT.u1, c = (a + b) / 2, f = 0.6, OCUT = 1.24;
    const hi = []; const H_ = (m) => { m.userData.interiorOnly = true; hi.push(m); return m; };
    B('oven_tower_box_low', g, M.cab, a, b, 0.1, OCUT, 0, f); B('oven_tower_toekick', g, M.cabDark, a, b, 0, 0.1, 0, f - 0.06);
    H_(B('oven_tower_box_high', g, M.cab, a, b, OCUT, 2.48, 0, f)); H_(B('oven_tower_crown', g, M.cab, a - 0.01, b + 0.01, 2.48, 2.58, 0, f + 0.06, 0.008));
    const cap = B('oven_tower_cut_cap', g, M.cut, a - 0.005, b + 0.005, OCUT, OCUT + 0.012, -0.005, f + 0.032); cap.userData.dollhouseOnly = true;
    const before = new Set(g.children);
    shaker('oven_tower_top_door_l', g, a + 0.004, c - 0.002, 1.98, 2.46, f, ['v', 1]); shaker('oven_tower_top_door_r', g, c + 0.002, b - 0.004, 1.98, 2.46, f, ['v', -1]);
    for (const [k, v0, v1] of [[1, 1.26, 1.94], [2, 0.56, 1.22]]) {
      B(`wall_oven_${k}_frame`, g, M.steel, a + 0.02, b - 0.02, v0, v1, f, f + 0.025, 0.008);
      B(`wall_oven_${k}_window`, g, M.blackGlass, a + 0.08, b - 0.08, v0 + 0.08, v1 - 0.15, f + 0.025, f + 0.03);
      B(`wall_oven_${k}_control_strip`, g, M.blackGlass, a + 0.05, b - 0.05, v1 - 0.06, v1 - 0.02, f + 0.025, f + 0.03);
      pull(`wall_oven_${k}_handle`, g, true, c, v1 - 0.1, f + 0.03, 0.56);
    }
    g.children.filter(m => !before.has(m) && (m.position.y > OCUT)).forEach(H_);
    shaker('oven_tower_drawer', g, a + 0.004, b - 0.004, 0.12, 0.52, f, ['h']);
  }
  B('runner_rug', arch, M.runner, -1.5, -0.72, 0, 0.01, 1.3, 2.55).castShadow = false;

  // ---------------- corner booth (from the nook photo): L banquette, trapezoid table, pillows, side table, rug
  const BZ0 = 6.0, BX1 = -1.85; // long run along the sink wall z BZ0..DZ1, return along the back wall x BX1..0
  { const g = G('booth', dining), U = M.upholstery, SD = 0.58;
    B('booth_long_plinth', g, U, -SD, 0, 0, 0.4, BZ0, DZ1, 0.03);
    B('booth_long_seat', g, U, -SD - 0.02, -0.14, 0.4, 0.5, BZ0, DZ1 - SD, 0.05);
    B('booth_long_back', g, U, -0.16, 0, 0.4, 0.86, BZ0, DZ1, 0.06);
    B('booth_return_plinth', g, U, BX1, -SD, 0, 0.4, DZ1 - SD, DZ1, 0.03);
    B('booth_return_seat', g, U, BX1, -0.14, 0.4, 0.5, DZ1 - SD - 0.02, DZ1 - 0.14, 0.05);
    B('booth_return_back', g, U, BX1, -0.16, 0.4, 0.86, DZ1 - 0.16, DZ1, 0.06);
    B('booth_long_seat_seam', g, std('#d9cfbd', 0.95), -SD + 0.02, -0.15, 0.398, 0.402, BZ0 + 0.02, DZ1 - SD);
    // trapezoid table, light wood top on a thick cream pedestal (long edge to the room, short edge to the corner)
    const trap = (aL, aS, b0, b1) => { const t = new THREE.Shape(); t.moveTo(-aL, b0); t.lineTo(aL, b0); t.lineTo(aS, b1); t.lineTo(-aS, b1); t.lineTo(-aL, b0); return t; };
    const tg = G('booth_table', g); tg.position.set(TBL.x, 0, TBL.z); tg.rotation.y = -3 * Math.PI / 4;
    const top = mesh('booth_table_top', new THREE.ExtrudeGeometry(trap(0.66, 0.4, -0.32, 0.32), { depth: 0.04, bevelEnabled: true, bevelSize: 0.008, bevelThickness: 0.008, bevelSegments: 2 }), M.lightWood, tg, 0, 0.73, 0); top.rotation.x = -Math.PI / 2;
    const ped = mesh('booth_table_pedestal', new THREE.ExtrudeGeometry(trap(0.44, 0.32, -0.15, 0.13), { depth: 0.72, bevelEnabled: false }), U, tg, 0, 0.005, 0); ped.rotation.x = -Math.PI / 2;
    // pillows in the corner (sage pattern + burgundy ruffle)
    const pil = (n, mat, w, h, x, y, z, ry, rx = -0.25) => { const m = B(n, g, mat, -w / 2, w / 2, -h / 2, h / 2, -0.07, 0.07, 0.06); m.position.set(x, y, z); m.rotation.set(rx, ry, 0, 'YXZ'); return m; };
    const sage = std('#ffffff', 0.95, 0, { map: T.sage });
    pil('booth_pillow_burgundy_1', M.burgundy, 0.46, 0.44, -0.3, 0.74, DZ1 - 0.3, -Math.PI * 0.75);
    pil('booth_pillow_sage_1', sage, 0.44, 0.42, -0.38, 0.7, DZ1 - 0.6, -Math.PI / 2 - 0.25);
    pil('booth_pillow_burgundy_2', M.burgundy, 0.4, 0.38, -0.25, 0.72, DZ1 - 0.95, -Math.PI / 2);
    pil('booth_pillow_sage_2', sage, 0.4, 0.38, -0.62, 0.7, DZ1 - 0.3, Math.PI - 0.2);
    // William Morris print leaning on the return back, against the back wall
    { const pg = G('framed_print', g); pg.position.set(-0.98, 0.86, DZ1 - 0.11); pg.rotation.set(-0.1, Math.PI, 0, 'YXZ');
      B('framed_print_frame', pg, std('#f2efe8', 0.5), -0.34, 0.34, 0, 0.86, -0.03, 0, 0.006);
      mesh('framed_print_art', new THREE.PlaneGeometry(0.56, 0.74), std('#ffffff', 0.9, 0, { map: T.morris }), pg, 0, 0.43, 0.002, false); }
    // brass pedestal side table with a tissue box, at the end of the return
    { const sx = BX1 - 0.2, sz = DZ1 - 0.25;
      cyl('side_table_base', g, M.brass, 0.12, 0.14, 0.03, sx, 0.015, sz, 24); cyl('side_table_column', g, M.brass, 0.022, 0.03, 0.56, sx, 0.31, sz, 12);
      cyl('side_table_knop', g, M.brass, 0.045, 0.045, 0.05, sx, 0.4, sz, 16); cyl('side_table_top', g, M.brass, 0.17, 0.16, 0.025, sx, 0.6, sz, 28);
      B('side_table_tissue_box', g, M.burgundy, sx - 0.065, sx + 0.065, 0.613, 0.75, sz - 0.065, sz + 0.065, 0.008);
      B('side_table_tissue', g, std('#f4f2ee', 0.9), sx - 0.03, sx + 0.03, 0.75, 0.8, sz - 0.02, sz + 0.02, 0.01); }
    // rug under the booth
    B('booth_rug', g, std('#ffffff', 1, 0, { map: T.nookRug }), -2.5, -0.3, 0, 0.012, 5.85, DZ1 - 0.05).castShadow = false;
    // two small round black plates on the sink-wall side of the nook
  }

  // ---------------- laundry (washer + dryer on the sink wall line)  frame u=z, w=-x
  const WASH = { u0: LZ0 + 0.18, u1: LZ0 + 0.86 }, DRY = { u0: LZ0 + 0.9, u1: LZ0 + 1.58 };
  B('laundry_counter', lauF, std('#8a6a4c', 0.5), LZ0 + 0.14, LZ0 + 1.62, 0.9, 0.94, 0, 0.74, 0.008);
  B('laundry_shelf', lauF, M.shelfWhite, LZ0 + 0.1, LZ1 - 0.1, 1.62, 1.65, 0, 0.32, 0.006);
  for (const [k, u] of [[1, LZ0 + 0.35], [2, LZ0 + 0.9], [3, LZ0 + 1.45]]) B(`laundry_basket_${k}`, lauF, M.basket, u - 0.18, u + 0.18, 1.65, 1.88, 0.04, 0.3, 0.03);
  [['#3c7fb8', 0.12], ['#e2e2e2', 0.1], ['#7cb87a', 0.09]].forEach(([c, r], k) => cyl(`laundry_bottle_${k + 1}`, lauF, std(c, 0.4), r * 0.5, r * 0.55, 0.24, LZ0 + 1.8 + k * 0.16, 1.77, 0.16, 12));
  { const g = G('laundry_utility_cabinet', lauF), u0 = LZ1 - 0.6, u1 = LZ1 - 0.05;
    B('laundry_utility_cabinet_box', g, M.shelfWhite, u0, u1, 0.1, 0.9, 0, 0.58); B('laundry_utility_cabinet_top', g, std('#8a6a4c', 0.5), u0 - 0.01, u1 + 0.01, 0.9, 0.94, 0, 0.62, 0.006);
    B('laundry_utility_cabinet_door', g, M.whiteTrim, u0 + 0.01, u1 - 0.01, 0.12, 0.86, 0.58, 0.6, 0.006); }
  const washerParts = (name, R, accent) => {
    const g = G(name, lauF), uc = (R.u0 + R.u1) / 2;
    B(`${name}_body`, g, M.applianceWhite, R.u0, R.u1, 0.02, 0.86, 0.05, 0.72, 0.025);
    B(`${name}_control_panel`, g, M.panelGrey, R.u0 + 0.02, R.u1 - 0.02, 0.74, 0.84, 0.72, 0.73, 0.004);
    const dispMat = std('#1c2328', 0.3, 0, { emissive: new THREE.Color(accent), emissiveIntensity: 0 });
    B(`${name}_display`, g, dispMat, uc + 0.04, uc + 0.2, 0.765, 0.815, 0.73, 0.734, 0.003);
    cyl(`${name}_dial`, g, M.chrome, 0.03, 0.03, 0.02, uc - 0.16, 0.79, 0.735, 16).rotation.x = Math.PI / 2;
    mesh(`${name}_door_ring`, new THREE.TorusGeometry(0.19, 0.028, 10, 40), M.chrome, g, uc, 0.41, 0.735);
    const glassMat = std('#1a2026', 0.08, 0.2, { emissive: new THREE.Color(accent), emissiveIntensity: 0 });
    mesh(`${name}_door_glass`, new THREE.CircleGeometry(0.17, 40), glassMat, g, uc, 0.41, 0.74, false);
    const ringMat = std('#8f949a', 0.4, 0, { emissive: new THREE.Color(accent), emissiveIntensity: 0 });
    const ring = mesh(`${name}_status_arc`, new THREE.TorusGeometry(0.235, 0.007, 6, 48, Math.PI * 1.35), ringMat, g, uc, 0.41, 0.738, false); ring.rotation.z = -Math.PI * 0.175 + Math.PI / 2;
    return { g, dispMat, glassMat, ringMat, uc };
  };

  // ---------------- exterior night yard (only through glass)
  { const deckA = extMat('#3a4157'), deckB = extMat('#343a4f'), gap = extMat('#1a1e2c'), rail = extMat('#59617b'), railDark = extMat('#474e66');
    const E = (n, mat, ...a) => extMesh(B(n, exterior, mat, ...a));
    const ZA = -1.5, ZB = LZ1 + 1.5;
    E('deck_substructure', gap, 0.22, 3.2, -0.32, -0.06, ZA, ZB);
    for (let i = 0, z = ZA; z < ZB; i++, z += 0.145) E(`deck_board_${i + 1}`, i % 2 ? deckA : deckB, 0.22, 3.2, -0.06, -0.03, z, Math.min(z + 0.133, ZB));
    const RX = 2.2;
    for (let z = ZA + 0.1, i = 0; z <= ZB; z += 1.3, i++) E(`deck_rail_post_${i + 1}`, rail, RX - 0.05, RX + 0.05, -0.03, 1.08, z - 0.05, z + 0.05);
    E('deck_rail_top', rail, RX - 0.07, RX + 0.07, 1.02, 1.08, ZA, ZB); E('deck_rail_bottom', railDark, RX - 0.03, RX + 0.03, 0.08, 0.12, ZA, ZB);
    for (let z = ZA, i = 0; z < ZB; z += 0.13, i++) E(`deck_baluster_${i + 1}`, railDark, RX - 0.015, RX + 0.015, 0.12, 1.02, z, z + 0.03);
    const sky = extMesh(mesh('exterior_night_sky', new THREE.PlaneGeometry(26, 10), extMat('#ffffff', { map: T.sky }), exterior, 6.0, 3.0, LZ1 / 2, false));
    sky.rotation.y = -Math.PI / 2; sky.userData.noExport = true;
  }

  // ---------------- devices
  const room = 'kitchen';
  // under-cabinet LED strips
  { const g = G('under_cabinet_lights', devGroup), m = lightMats(), decals = [], lights = [];
    const fr = { cook: G('under_cabinet_lights_cook', g), sink: G('under_cabinet_lights_sink', g) }; fr.sink.rotation.y = -Math.PI / 2;
    [['cook', -1.25, -0.02], ['cook', -2.62, -2.09], ['sink', 0.35, 1.45], ['sink', 2.4, 3.62]].forEach(([f, u0, u1], i) => {
      const p = fr[f], uc = (u0 + u1) / 2, len = u1 - u0;
      B(`light_under_cabinet_strip_${i + 1}`, p, m.bulb, u0 + 0.03, u1 - 0.03, 1.375, 1.385, 0.26, 0.28).castShadow = false;
      decals.push(decal(`glow_under_cabinet_${i + 1}_backsplash`, p, T.wash, STYLE.warm, 0.85, len, 0.5, uc, 1.17, 0.014, '+z'));
      decals.push(decal(`glow_under_cabinet_${i + 1}_counter`, p, T.glow, STYLE.warm, 0.55, len * 1.1, 0.6, uc, 0.923, 0.3, 'up'));
      lights.push(pl(`light_under_cabinet_${i + 1}`, p, STYLE.warm, uc, 1.3, 0.3, 1.4));
    });
    register('under_cabinet_lights', { room, label: 'Under-cabinet lights', type: 'light', group: g, emissive: [{ mat: m.bulb, on: 6 }], lights: lights.map(l => ({ light: l, on: 1.2 })), decals, anchor: new THREE.Vector3(-0.3, 1.38, 1.0) });
  }
  // toe-kick LED strips (base runs, towers, island)
  { const g = G('toe_kick_lights', devGroup), m = lightMats(), decals = [];
    const fr = { cook: G('toe_kick_cook', g), sink: G('toe_kick_sink', g), isl: G('toe_kick_island', g) }; fr.sink.rotation.y = -Math.PI / 2;
    fr.isl.position.set(ISX, 0, 0); fr.isl.rotation.y = -Math.PI / 2;
    const strip = (p, n, u0, u1, w, flip) => {
      B(`light_toe_kick_${n}`, p, m.bulb, u0 + 0.02, u1 - 0.02, 0.012, 0.022, w - 0.012, w).castShadow = false;
      const d = decal(`glow_toe_kick_${n}_floor`, p, T.wash, STYLE.warm, 0.75, u1 - u0, 0.42, (u0 + u1) / 2, 0.006, flip ? w - 0.21 : w + 0.21, 'up');
      if (flip) d.rotation.z = Math.PI; decals.push(d);
    };
    strip(fr.cook, 'cooktop_run', -2.62, 0, D - 0.08);
    const gh = G('toe_kick_ghost', g); gh.position.x = -W; gh.rotation.y = Math.PI / 2; strip(gh, 'oven_tower', OVT.u0, OVT.u1, 0.54);
    strip(fr.sink, 'sink_run', 0.62, 3.62, D - 0.08);
    strip(fr.isl, 'island_front', IZ0 + 0.06, IZ1 - 0.06, IHW - 0.06); strip(fr.isl, 'island_back', IZ0 + 0.06, IZ1 - 0.06, -IHW + 0.06, true);
    register('toe_kick_lights', { room, label: 'Toe-kick lights', type: 'light', group: g, emissive: [{ mat: m.bulb, on: 6 }], decals, anchor: new THREE.Vector3(-0.6, 0.05, 2.1) });
  }
  // recessed ceiling lights (ceiling removed -> floating can housings; ON adds a soft light cone + pools)
  { const g = G('recessed_lights', devGroup), m = lightMats(), decals = [], lights = [], show = [];
    const coneTex = canvasTex(8, 256, (c, w, h) => { const gr = c.createLinearGradient(0, 0, 0, h); gr.addColorStop(0, 'rgba(255,255,255,0.9)'); gr.addColorStop(1, 'rgba(255,255,255,0)'); c.fillStyle = gr; c.fillRect(0, 0, w, h); });
    const coneMat = new THREE.MeshBasicMaterial({ map: coneTex, color: STYLE.warm, transparent: true, opacity: 0.16, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide, toneMapped: false });
    const housing = std('#cfcac2', 0.6);
    const pts = [[-1.3, 1.3], [-1.3, 2.85], [-1.3, 4.4], [-2.5, 1.3], [-2.5, 2.85], [-2.5, 4.4], [-3.75, 2.2], [-3.75, 3.9]];
    pts.forEach(([x, z], i) => {
      cyl(`recessed_light_${i + 1}_trim`, g, housing, 0.075, 0.075, 0.012, x, H - 0.006, z, 20).castShadow = false;
      cyl(`recessed_light_${i + 1}`, g, m.bulb, 0.05, 0.05, 0.004, x, H - 0.013, z, 20).castShadow = false;
      const onIsland = Math.abs(x - ISX) < 0.3, fy = onIsland ? 0.925 : 0.012;
      const cone = mesh(`light_cone_recessed_${i + 1}`, new THREE.ConeGeometry(0.5, H - 0.08 - fy, 24, 1, true), coneMat, g, x, (H - 0.08 + fy) / 2, z, false);
      cone.receiveShadow = false; cone.visible = false; cone.renderOrder = 6; show.push(cone);
      decals.push(decal(`glow_recessed_${i + 1}_pool`, g, T.glow, STYLE.warm, 0.5, 1.3, 1.3, x, fy + 0.003, z, 'up'));
    });
    for (const z of [1.3, 2.85, 4.4]) { const s = new THREE.SpotLight(STYLE.warm, 0, 5, 0.9, 0.7, 2); s.name = `light_recessed_row_${z}`; s.position.set(-1.9, H - 0.1, z); s.target.position.set(-1.9, 0, z); g.add(s); g.add(s.target); lights.push(s); }
    register('recessed_lights', { room, label: 'Recessed lights', type: 'light', group: g, emissive: [{ mat: m.bulb, on: 6 }], lights: lights.map(l => ({ light: l, on: 7 })), decals, show, anchor: new THREE.Vector3(-1.9, H - 0.08, 2.85) });
  }
  // dining pendant: ribbed clear-glass prismatic dome on a brass fitting, hanging from the tray beam
  { const g = G('dining_pendant', devGroup), m = lightMats(), y0 = 1.62;
    cyl('dining_pendant_canopy', g, M.brass, 0.07, 0.07, 0.03, TBL.x, 2.945, TBL.z, 20);
    cyl('dining_pendant_rod', g, M.brass, 0.008, 0.008, 2.945 - (y0 + 0.3), TBL.x, (2.945 + y0 + 0.3) / 2, TBL.z, 8);
    cyl('dining_pendant_fitter_neck', g, M.brass, 0.03, 0.045, 0.06, TBL.x, y0 + 0.29, TBL.z, 20);
    cyl('dining_pendant_fitter', g, M.brass, 0.075, 0.075, 0.05, TBL.x, y0 + 0.245, TBL.z, 24);
    const prof = [[0.07, 0.22], [0.12, 0.205], [0.17, 0.17], [0.205, 0.11], [0.225, 0.04], [0.23, 0.0], [0.225, -0.008]].map(([a, b]) => new THREE.Vector2(a, b));
    const glassMat = new THREE.MeshPhysicalMaterial({ color: '#f3f0e8', map: T.ribs, roughness: 0.18, metalness: 0, transparent: true, opacity: 0.5, side: THREE.DoubleSide, depthWrite: false,
      emissive: new THREE.Color(STYLE.warm), emissiveMap: T.ribs, emissiveIntensity: 0 });
    const dome = mesh('dining_pendant_glass_dome', new THREE.LatheGeometry(prof, 48), glassMat, g, TBL.x, y0, TBL.z, false); dome.renderOrder = 4;
    mesh('dining_pendant_rim', new THREE.TorusGeometry(0.228, 0.006, 6, 48), glassMat, g, TBL.x, y0 - 0.006, TBL.z, false).rotation.x = Math.PI / 2;
    sphere('dining_pendant_bulb', g, m.bulb, 0.05, TBL.x, y0 + 0.12, TBL.z);
    const decals = [decal('glow_dining_pendant_table', g, T.glow, STYLE.warm, 0.65, 1.5, 1.5, TBL.x, 0.786, TBL.z, 'up'),
      decal('glow_dining_pendant_floor', g, T.glow, STYLE.warm, 0.35, 3.2, 3.2, TBL.x, 0.016, TBL.z, 'up'),
      decal('glow_dining_pendant_wall', g, T.glow, STYLE.warm, 0.25, 2.4, 2.0, -0.004, 1.6, TBL.z, '-x')];
    const l = pl('light_dining_pendant', g, STYLE.warm, TBL.x, y0 - 0.12, TBL.z, 5);
    register('dining_pendant', { room, label: 'Dining pendant', type: 'light', group: g, emissive: [{ mat: m.bulb, on: 3 }, { mat: glassMat, on: 0.22 }], lights: [{ light: l, on: 3 }], decals, anchor: new THREE.Vector3(TBL.x, y0 + 0.15, TBL.z) });
  }
  // booth table lamp: small burgundy cordless lamp on the table
  { const g = G('table_lamp_booth', devGroup), m = lightMats(), lx = TBL.x + 0.16, lz = TBL.z + 0.16, ty = 0.786;
    cyl('table_lamp_booth_base', g, M.brass, 0.05, 0.055, 0.015, lx, ty + 0.0075, lz, 20);
    cyl('table_lamp_booth_stem', g, M.brass, 0.008, 0.008, 0.25, lx, ty + 0.14, lz, 8);
    const shadeMat = std('#6e1f2c', 0.85, 0, { side: THREE.DoubleSide, emissive: new THREE.Color('#ff7a4a'), emissiveIntensity: 0 });
    cyl('table_lamp_booth_shade', g, shadeMat, 0.045, 0.075, 0.1, lx, ty + 0.3, lz, 24, true).castShadow = false;
    sphere('table_lamp_booth_bulb', g, m.bulb, 0.018, lx, ty + 0.28, lz);
    const decals = [decal('glow_table_lamp_booth_table', g, T.glow, STYLE.warm, 0.55, 0.8, 0.8, lx, ty + 0.002, lz, 'up'),
      decal('glow_table_lamp_booth_wall', g, T.glow, STYLE.warm, 0.25, 1.2, 1.0, -0.004, 1.15, lz + 0.3, '-x')];
    const l = pl('light_table_lamp_booth', g, STYLE.warm, lx, ty + 0.25, lz, 2.2);
    register('table_lamp_booth', { room, label: 'Booth table lamp', type: 'light', group: g, emissive: [{ mat: m.bulb, on: 5 }, { mat: shadeMat, on: 0.9 }], lights: [{ light: l, on: 1.2 }], decals, anchor: new THREE.Vector3(lx, ty + 0.36, lz) });
  }
  // range hood light
  { const g = G('range_hood_light', devGroup), m = lightMats();
    for (const [k, du] of [[1, -0.2], [2, 0.2]]) { const d = cyl(`range_hood_lamp_${k}`, g, m.bulb, 0.035, 0.035, 0.006, CK + du, 1.535, 0.3, 16); d.castShadow = false; }
    const decals = [decal('glow_range_hood_cooktop', g, T.glow, STYLE.warm, 0.7, 1.1, 0.8, CK, 0.94, 0.33, 'up'),
      decal('glow_range_hood_backsplash', g, T.wash, STYLE.warm, 0.7, 0.85, 0.6, CK, 1.24, 0.014, '+z')];
    const s = new THREE.SpotLight(STYLE.warm, 0, 2.5, 0.9, 0.6, 2); s.name = 'light_range_hood'; s.position.set(CK, 1.53, 0.3); s.target.position.set(CK, 0.9, 0.32); g.add(s); g.add(s.target);
    register('range_hood_light', { room, label: 'Range hood light', type: 'light', group: g, emissive: [{ mat: m.bulb, on: 6 }], lights: [{ light: s, on: 2.5 }], decals, anchor: new THREE.Vector3(CK, 1.6, 0.4) });
  }
  // laundry ceiling light (flush mount, floating at ceiling height)
  { const g = G('laundry_light', devGroup), m = lightMats(), lx = -1.2, lz = (LZ0 + LZ1) / 2;
    cyl('laundry_light_canopy', g, std('#e9e6e0', 0.6), 0.17, 0.17, 0.03, lx, H - 0.015, lz, 28).castShadow = false;
    const dome = mesh('laundry_light_dome', new THREE.SphereGeometry(0.16, 28, 10, 0, Math.PI * 2, Math.PI / 2, Math.PI / 2), m.shade, g, lx, H - 0.03, lz, false); dome.scale.y = 0.45;
    const decals = [decal('glow_laundry_floor', g, T.glow, STYLE.warm, 0.45, 2.6, 2.6, lx, 0.012, lz, 'up'),
      decal('glow_laundry_wall', g, T.glow, STYLE.warm, 0.3, 2.2, 1.6, -0.003, 1.9, lz, '-x')];
    const l = pl('light_laundry', g, STYLE.warm, lx, H - 0.25, lz, 4.5);
    register('laundry_light', { room, label: 'Laundry light', type: 'light', group: g, emissive: [{ mat: m.shade, on: 2.2 }], lights: [{ light: l, on: 3.5 }], decals, anchor: new THREE.Vector3(lx, H - 0.1, lz) });
  }
  // washer + dryer (running = display + drum glow + tumble arc)
  { const wsh = washerParts('washer', WASH, '#58c8ff'), dry = washerParts('dryer', DRY, '#ffae5c');
    const wDecal = decal('glow_washer_drum', wsh.g, T.glow, '#58c8ff', 0.35, 0.7, 0.7, wsh.uc, 0.41, 0.745, '+z');
    const dDecal = decal('glow_dryer_drum', dry.g, T.glow, '#ffae5c', 0.35, 0.7, 0.7, dry.uc, 0.41, 0.745, '+z');
    register('washer', { room, label: 'Washer', type: 'appliance', group: wsh.g, emissive: [{ mat: wsh.dispMat, on: 4 }, { mat: wsh.glassMat, on: 0.7 }, { mat: wsh.ringMat, on: 3 }], decals: [wDecal], anchor: new THREE.Vector3(-0.75, 0.6, (WASH.u0 + WASH.u1) / 2) });
    register('dryer', { room, label: 'Dryer', type: 'appliance', group: dry.g, emissive: [{ mat: dry.dispMat, on: 4 }, { mat: dry.glassMat, on: 0.7 }, { mat: dry.ringMat, on: 3 }], decals: [dDecal], anchor: new THREE.Vector3(-0.75, 0.6, (DRY.u0 + DRY.u1) / 2) });
  }

  Object.keys(K.devices).forEach(n => K.setDevice(n, false));
  return { root, devices: K.devices, setDevice: K.setDevice, textures: T };
}

export const PRESETS = {
  all_off: {},
  all_on: { under_cabinet_lights: 1, toe_kick_lights: 1, recessed_lights: 1, dining_pendant: 1, table_lamp_booth: 1, range_hood_light: 1, laundry_light: 1, washer: 1, dryer: 1 },
  evening: { under_cabinet_lights: 1, toe_kick_lights: 1, dining_pendant: 1, table_lamp_booth: 1 },
  laundry_running: { laundry_light: 1, washer: 1, dryer: 1 },
};

export const VIEWS = {
  dollhouse: { pos: [-15.6, 10.9, 12.6], target: [-2.3, 0.5, 5.0], fov: 30 },
  hero: { pos: [-9.9, 7.4, 12.0], target: [-1.9, 0.8, 4.0], fov: 31 },
  photo: { pos: [-3.75, 1.65, 0.85], target: [-0.6, 1.0, 5.4], fov: 80, interior: true },
  oven: { pos: [-1.35, 1.6, 1.4], target: [-4.3, 1.15, 3.95], fov: 60, interior: true },
  bay_and_door: { pos: [-2.6, 1.55, 5.1], target: [0.2, 1.15, 6.4], fov: 62, interior: true },
  nook: { pos: [-1.45, 1.62, 3.55], target: [-1.15, 0.85, 8.2], fov: 74, interior: true },
  island_sink_side: { pos: [-0.75, 1.55, 5.7], target: [-2.5, 0.55, 3.3], fov: 55, interior: true },
  laundry_detail: { pos: [-4.2, 2.4, 12.4], target: [-0.6, 0.8, 9.5], fov: 42 },
  top: { pos: [-6.0, 21, 12.5], target: [-2.3, 0, 5.3], fov: 34 },
};

export const createKitchen = (container, opts = {}) => createApp(container, { build: buildKitchen, views: VIEWS, presets: PRESETS, shadow: { center: [-2.3, 0, 5.3], half: 9 }, ...opts });
