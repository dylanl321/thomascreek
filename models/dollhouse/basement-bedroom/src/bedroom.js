// Basement bedroom — Room 04 of the dollhouse. Follows /workspace/room/style.md and reuses kitchen/src/common.js.
// Coordinates: metres, Y up. Kept (standing) walls: BED WALL = plane z=0 (headboard) and PRINTS WALL = plane x=W (two framed prints). Dresser + wall TV on the front wall z=L, facing the bed.
// Interior 0<x<W, z>0. Camera-side walls (left wall x=0 with the doorway, front wall z=L) are cut at 1.1 m; upper parts interior-only.
// Photo note: the two nightstand table lamps in photo.png are NOT real; brushed-brass cone sconces (sconce_ref.jpg) replace them.
import * as THREE from 'three';
import { STYLE, rng, canvasTex, makeBaseTextures, std, makeKit, createApp } from './common.js';
import { makeLifeKit } from './life.js';

export const BROOM = { W: 4.0, L: 4.5, H: 2.45 };

function bedroomTextures() {
  const carpet = canvasTex(256, 256, (g, w, h) => { g.fillStyle = '#d4cdc1'; g.fillRect(0, 0, w, h); const r = rng(5);
    for (let i = 0; i < 6000; i++) { g.fillStyle = r() < 0.5 ? 'rgba(110,100,85,0.10)' : 'rgba(255,252,245,0.14)'; g.fillRect(r() * w, r() * h, 1.5, 1.5); } });
  carpet.wrapS = carpet.wrapT = THREE.RepeatWrapping;
  const walnut = canvasTex(256, 256, (g, w, h) => { g.fillStyle = '#6b4127'; g.fillRect(0, 0, w, h); const r = rng(8);
    for (let i = 0; i < 70; i++) { g.strokeStyle = `rgba(35,18,8,${0.1 + r() * 0.22})`; g.lineWidth = 1 + r() * 2; g.beginPath(); const y = r() * h; g.moveTo(0, y); g.bezierCurveTo(w * 0.3, y + (r() - 0.5) * 20, w * 0.6, y + (r() - 0.5) * 20, w, y + (r() - 0.5) * 10); g.stroke(); } });
  walnut.wrapS = walnut.wrapT = THREE.RepeatWrapping;
  // walnut, grain running vertically (headboard panels, drawer fronts)
  const walnutV = canvasTex(256, 256, (g, w, h) => { g.fillStyle = '#6e4228'; g.fillRect(0, 0, w, h); const r = rng(9);
    for (let i = 0; i < 70; i++) { g.strokeStyle = `rgba(35,18,8,${0.1 + r() * 0.22})`; g.lineWidth = 1 + r() * 2; g.beginPath(); const x = r() * w; g.moveTo(x, 0); g.bezierCurveTo(x + (r() - 0.5) * 18, h * 0.3, x + (r() - 0.5) * 18, h * 0.6, x + (r() - 0.5) * 10, h); g.stroke(); } });
  // moss + wood chevron art (nested chevrons meeting in the middle)
  const chevron = canvasTex(512, 256, (g, w, h) => { const r = rng(21);
    g.fillStyle = '#3f5a22'; g.fillRect(0, 0, w, h);
    for (let i = 0; i < 4000; i++) { const v = r(); g.fillStyle = v < 0.33 ? '#5c7d2c' : v < 0.66 ? '#2d4418' : '#7a9a3a'; g.beginPath(); g.arc(r() * w, r() * h, 1.5 + r() * 3, 0, 7); g.fill(); }
    const chev = (col, lw, dx) => { g.strokeStyle = col; g.lineWidth = lw; g.lineJoin = 'miter';
      for (let k = 0; k < 9; k++) { const ax = w / 2 - k * 62 + dx, bx = w / 2 + k * 62 - dx;
        g.beginPath(); g.moveTo(ax - h / 2, -4); g.lineTo(ax, h / 2); g.lineTo(ax - h / 2, h + 4); g.stroke();
        g.beginPath(); g.moveTo(bx + h / 2, -4); g.lineTo(bx, h / 2); g.lineTo(bx + h / 2, h + 4); g.stroke(); } };
    chev('#c58c52', 20, 0); chev('rgba(90,50,22,0.55)', 2, -8); chev('rgba(240,200,150,0.35)', 2, 7);
  });
  const print = (seed, tint) => canvasTex(160, 210, (g, w, h) => { const r = rng(seed);
    g.fillStyle = '#efe7d6'; g.fillRect(0, 0, w, h); g.fillStyle = tint; g.fillRect(18, 18, w - 36, h - 36);
    g.strokeStyle = 'rgba(90,70,50,0.45)'; g.lineWidth = 1;
    for (let i = 0; i < 26; i++) { g.beginPath(); let x = 18 + r() * (w - 36), y = 18 + r() * (h - 36); g.moveTo(x, y); for (let j = 0; j < 6; j++) { x += (r() - 0.5) * 30; y += (r() - 0.5) * 30; g.lineTo(Math.max(18, Math.min(w - 18, x)), Math.max(18, Math.min(h - 18, y))); } g.stroke(); }
    g.fillStyle = 'rgba(120,90,60,0.35)'; for (let i = 0; i < 40; i++) g.fillRect(18 + r() * (w - 40), 18 + r() * (h - 40), 3 + r() * 10, 2);
    g.strokeStyle = 'rgba(70,50,35,0.5)'; g.strokeRect(24, 24, w - 48, h - 48); });
  const knurl = canvasTex(64, 8, (g, w, h) => { for (let x = 0; x < w; x++) { const v = x % 4 < 2 ? 70 : 150; g.fillStyle = `rgb(${v},${v - 10},${v - 30})`; g.fillRect(x, 0, 1, h); } });
  knurl.wrapS = THREE.RepeatWrapping; knurl.repeat.set(3, 1);
  const brushed = canvasTex(256, 64, (g, w, h) => { g.fillStyle = '#b29463'; g.fillRect(0, 0, w, h); const r = rng(13);
    for (let i = 0; i < 400; i++) { g.fillStyle = `rgba(${r() < 0.5 ? '255,240,200' : '80,60,30'},${0.06 + r() * 0.08})`; g.fillRect(r() * w, r() * h, 1, h); } });
  brushed.wrapS = THREE.RepeatWrapping;
  // cone-of-light wall pool under a down-pointing shade (bright at the top, fanning out and fading downward)
  const sconcePool = canvasTex(256, 384, (g, w, h) => { g.filter = 'blur(14px)';
    const gr = g.createLinearGradient(0, 0, 0, h); gr.addColorStop(0, 'rgba(255,255,255,1)'); gr.addColorStop(0.45, 'rgba(255,255,255,0.45)'); gr.addColorStop(1, 'rgba(255,255,255,0)');
    g.fillStyle = gr; g.beginPath(); g.moveTo(w * 0.42, 20); g.lineTo(w * 0.58, 20); g.lineTo(w * 0.92, h - 20); g.lineTo(w * 0.08, h - 20); g.closePath(); g.fill(); });
  // ceiling-fan spin cue: soft motion-blur disc with faint streak rings
  const spin = canvasTex(256, 256, (g, w, h) => { const c = w / 2;
    const gr = g.createRadialGradient(c, c, 14, c, c, c); gr.addColorStop(0, 'rgba(0,0,0,0)'); gr.addColorStop(0.15, 'rgba(255,255,255,0.55)'); gr.addColorStop(0.9, 'rgba(255,255,255,0.35)'); gr.addColorStop(1, 'rgba(255,255,255,0)');
    g.fillStyle = gr; g.fillRect(0, 0, w, h);
    g.strokeStyle = 'rgba(255,255,255,0.8)'; g.lineWidth = 3;
    for (const [rr, a0] of [[0.62, 0.2], [0.78, 2.3], [0.92, 4.4]]) { g.beginPath(); g.arc(c, c, c * rr, a0, a0 + 1.4); g.stroke(); } });
  return { carpet, walnut, walnutV, chevron, printA: print(41, '#e2d3b4'), printB: print(57, '#ddcfb0'), knurl, brushed, sconcePool, spin };
}

export function buildBedroom() {
  const T = { ...makeBaseTextures(), ...bedroomTextures() };
  const { W, L, H } = BROOM, WT = 0.2, CUT = 1.1;
  const root = new THREE.Group(); root.name = 'basement_bedroom';
  const K = makeKit(root, T);
  const { G, mesh, B, cyl, sphere, decal, lightMats, pl, register } = K;
  const room = 'basement_bedroom';
  const M = {
    cut: std(STYLE.cut, 0.95), slab: std(STYLE.slab, 0.95),
    wall: std('#33363a', 0.9), ceiling: std('#e9e8e4', 0.9), trim: std('#f4f2ee', 0.5),
    carpet: (rx, rz) => { const t = T.carpet.clone(); t.needsUpdate = true; t.repeat.set(rx, rz); return std('#ffffff', 1, 0, { map: t }); },
    walnut: std('#ffffff', 0.55, 0, { map: T.walnut }), walnutV: std('#ffffff', 0.55, 0, { map: T.walnutV }), walnutDark: std('#3f2616', 0.6),
    sheet: std('#f3f2ef', 0.95), duvet: std('#b4b6b8', 0.95), duvetFold: std('#e9e9e7', 0.95), pillow: std('#f6f5f2', 0.95), pillowGray: std('#9a9a9f', 0.95),
    brass: std('#ffffff', 0.38, 0.9, { map: T.brushed }), brassDark: std('#ffffff', 0.45, 0.85, { map: T.knurl }),
    black: std('#151515', 0.55), blackMatte: std('#1b1b1b', 0.8), white: std('#f1efea', 0.5), ceramic: std('#ecebe6', 0.5),
    potDark: std('#2a2a2a', 0.7), leaf: std('#4f7a3a', 0.75), leaf2: std('#6b9447', 0.75), frameWood: std('#c9a571', 0.55),
    book: [std('#6a7f8f', 0.8), std('#c8b79a', 0.8), std('#8d4a3a', 0.8)],
  };
  const arch = G('architecture'), openings = G('doors_and_windows'), furn = G('furniture'), devGroup = G('devices');
  const io = (m) => { m.userData.interiorOnly = true; return m; };

  // ---------------- floor, walls, ceiling band, trim
  B('floor_slab', arch, M.slab, -WT, W + WT, -0.3, -0.02, -WT, L + WT);
  B('floor_carpet', arch, M.carpet(W / 1.2, L / 1.2), 0, W, -0.02, 0, 0, L);
  // BoxGeometry face order: +x, -x, +y, -y, +z, -z
  B('wall_bed', arch, M.wall, -WT, W + WT, 0, H, -WT, 0).material = [M.cut, M.cut, M.cut, M.wall, M.wall, M.wall];
  B('wall_prints', arch, M.wall, W, W + WT, 0, H, 0, L + WT).material = [M.wall, M.wall, M.cut, M.wall, M.cut, M.wall];
  const DZ0 = 2.85, DZ1 = 3.7, DHt = 2.05;                       // doorway on the left wall
  const leftWall = (n, z0, z1, y0, y1) => B(n, arch, M.wall, -WT, 0, y0, y1, z0, z1);
  leftWall('wall_left_a', 0, DZ0, 0, CUT).material = [M.wall, M.wall, M.cut, M.wall, M.cut, M.cut];
  leftWall('wall_left_b', DZ1, L + WT, 0, CUT).material = [M.wall, M.wall, M.cut, M.wall, M.cut, M.cut];
  io(leftWall('wall_left_upper_a', 0, DZ0, CUT, H)); io(leftWall('wall_left_upper_b', DZ1, L + WT, CUT, H)); io(leftWall('wall_left_over_door', DZ0, DZ1, DHt, H));
  B('wall_front', arch, M.wall, 0, W, 0, CUT, L, L + WT).material = [M.cut, M.cut, M.cut, M.wall, M.wall, M.wall];
  io(B('wall_front_upper', arch, M.wall, 0, W, CUT, H, L, L + WT));
  // ceiling omitted (style guide); light gray-white band along the two standing walls
  B('ceiling_band_bed_wall', arch, M.ceiling, -WT, W, H, H + 0.08, 0, 0.45).material = [M.cut, M.cut, M.cut, M.ceiling, M.cut, M.ceiling];
  B('ceiling_band_prints_wall', arch, M.ceiling, W - 0.45, W + WT, H, H + 0.08, -WT, L + WT).material = [M.cut, M.ceiling, M.cut, M.ceiling, M.cut, M.cut];
  B('ceiling_band_corner', arch, M.cut, -WT, W + WT, H, H + 0.08, -WT, 0);
  B('crown_bed_wall', arch, M.trim, 0, W, H - 0.11, H, 0, 0.09, 0.01); B('crown_prints_wall', arch, M.trim, W - 0.09, W, H - 0.11, H, 0, L, 0.01);
  io(B('crown_left_wall', arch, M.trim, 0, 0.09, H - 0.11, H, 0, L, 0.01)); io(B('crown_front_wall', arch, M.trim, 0, W, H - 0.11, H, L - 0.09, L, 0.01));
  B('baseboard_bed_wall', arch, M.trim, 0, W, 0, 0.12, 0, 0.018); B('baseboard_prints_wall', arch, M.trim, W - 0.018, W, 0, 0.12, 0, L);
  B('baseboard_left_a', arch, M.trim, 0, 0.018, 0, 0.12, 0, DZ0 - 0.07); B('baseboard_left_b', arch, M.trim, 0, 0.018, 0, 0.12, DZ1 + 0.07, L);
  B('baseboard_front', arch, M.trim, 0, W, 0, 0.12, L - 0.018, L);

  // ---------------- doorway (left wall) to the hall
  { const g = G('doorway_left', openings);
    B('doorway_casing_a', g, M.trim, 0, 0.022, 0, CUT, DZ0 - 0.08, DZ0).material = [M.trim, M.trim, M.cut, M.trim, M.trim, M.trim];
    B('doorway_casing_b', g, M.trim, 0, 0.022, 0, CUT, DZ1, DZ1 + 0.08).material = [M.trim, M.trim, M.cut, M.trim, M.trim, M.trim];
    io(B('doorway_casing_a_upper', g, M.trim, 0, 0.022, CUT, DHt + 0.1, DZ0 - 0.08, DZ0)); io(B('doorway_casing_b_upper', g, M.trim, 0, 0.022, CUT, DHt + 0.1, DZ1, DZ1 + 0.08));
    io(B('doorway_casing_head', g, M.trim, 0, 0.022, DHt, DHt + 0.1, DZ0 - 0.08, DZ1 + 0.08));
    B('doorway_jamb_a', g, M.trim, -WT, 0, 0, CUT, DZ0, DZ0 + 0.02); B('doorway_jamb_b', g, M.trim, -WT, 0, 0, CUT, DZ1 - 0.02, DZ1);
  }

  // ---------------- walnut mid-century bed (panel headboard), bedding
  const BX0 = 0.84, BX1 = 2.46, BC = (BX0 + BX1) / 2, BZ1 = 2.2;
  { const g = G('bed', furn);
    B('bed_headboard', g, M.walnutV, BX0, BX1, 0.22, 1.18, 0.03, 0.09, 0.012);
    for (let i = 1; i < 4; i++) { const x = BX0 + (BX1 - BX0) * i / 4; B(`bed_headboard_groove_${i}`, g, M.walnutDark, x - 0.006, x + 0.006, 0.3, 1.1, 0.088, 0.094); }
    B('bed_headboard_rail', g, M.walnut, BX0, BX1, 1.16, 1.2, 0.025, 0.1, 0.01);
    B('bed_rail_left', g, M.walnut, BX0, BX0 + 0.05, 0.16, 0.36, 0.09, BZ1, 0.008); B('bed_rail_right', g, M.walnut, BX1 - 0.05, BX1, 0.16, 0.36, 0.09, BZ1, 0.008);
    B('bed_footboard', g, M.walnut, BX0, BX1, 0.16, 0.42, BZ1 - 0.05, BZ1, 0.01);
    for (const [k, x, z] of [[1, BX0 + 0.04, 0.07], [2, BX1 - 0.04, 0.07], [3, BX0 + 0.04, BZ1 - 0.03], [4, BX1 - 0.04, BZ1 - 0.03]]) cyl(`bed_leg_${k}`, g, M.walnut, 0.028, 0.018, 0.18, x, 0.09, z, 12);
    B('bed_mattress', g, M.sheet, BX0 + 0.05, BX1 - 0.05, 0.36, 0.6, 0.1, BZ1 - 0.05, 0.05);
    B('bed_duvet', g, M.duvet, BX0 - 0.03, BX1 + 0.03, 0.34, 0.66, 0.66, BZ1 + 0.03, 0.06);
    B('bed_duvet_fold', g, M.duvetFold, BX0 - 0.02, BX1 + 0.02, 0.5, 0.685, 0.6, 0.78, 0.05);
    for (const [k, x0, x1] of [[1, BX0 + 0.1, BC - 0.03], [2, BC + 0.03, BX1 - 0.1]]) { const p = B(`bed_pillow_${k}`, g, M.pillow, x0, x1, 0.6, 0.84, 0.12, 0.34, 0.08); p.rotation.x = -0.28; }
    for (const [k, x0, x1] of [[3, BX0 + 0.14, BC - 0.06], [4, BC + 0.06, BX1 - 0.14]]) { const p = B(`bed_pillow_${k}`, g, M.pillow, x0, x1, 0.6, 0.78, 0.3, 0.48, 0.07); p.rotation.x = -0.2; }
    const lp = B('bed_pillow_lumbar_gray', g, M.pillowGray, BC - 0.27, BC + 0.27, 0.62, 0.8, 0.47, 0.58, 0.05); lp.rotation.x = -0.15; }

  // ---------------- nightstands (drawer + open shelf, splayed legs) with small plants
  const nightstand = (name, x0) => { const g = G(name, furn), x1 = x0 + 0.5, z0 = 0.04, z1 = 0.44;
    B(`${name}_top`, g, M.walnut, x0, x1, 0.57, 0.6, z0, z1, 0.008);
    B(`${name}_side_l`, g, M.walnut, x0, x0 + 0.02, 0.3, 0.57, z0, z1); B(`${name}_side_r`, g, M.walnut, x1 - 0.02, x1, 0.3, 0.57, z0, z1);
    B(`${name}_back`, g, M.walnut, x0, x1, 0.3, 0.57, z0, z0 + 0.015);
    B(`${name}_shelf`, g, M.walnut, x0, x1, 0.3, 0.325, z0, z1);
    B(`${name}_divider`, g, M.walnut, x0, x1, 0.43, 0.45, z0, z1);
    B(`${name}_drawer`, g, M.walnutV, x0 + 0.025, x1 - 0.025, 0.455, 0.565, z1 - 0.02, z1 + 0.005, 0.006);
    B(`${name}_drawer_pull`, g, M.brass, (x0 + x1) / 2 - 0.05, (x0 + x1) / 2 + 0.05, 0.505, 0.515, z1 + 0.005, z1 + 0.02, 0.004);
    for (const [k, xs, zs] of [[1, -1, -1], [2, 1, -1], [3, -1, 1], [4, 1, 1]]) { const x = xs < 0 ? x0 + 0.05 : x1 - 0.05, z = zs < 0 ? z0 + 0.06 : z1 - 0.06;
      const l = cyl(`${name}_leg_${k}`, g, M.walnut, 0.016, 0.01, 0.33, x + xs * 0.025, 0.15, z + zs * 0.02, 10); l.rotation.z = -xs * 0.16; l.rotation.x = zs * 0.12; }
    B(`${name}_book_1`, g, M.book[0], x0 + 0.06, x0 + 0.3, 0.325, 0.355, z0 + 0.08, z1 - 0.08); B(`${name}_book_2`, g, M.book[1], x0 + 0.08, x0 + 0.28, 0.355, 0.38, z0 + 0.1, z1 - 0.1);
    // small plant
    const px = x0 + 0.13, pz = 0.3; cyl(`${name}_plant_pot`, g, M.potDark, 0.05, 0.04, 0.09, px, 0.645, pz, 16);
    for (let i = 0; i < 7; i++) { const a = i / 7 * Math.PI * 2; const lf = mesh(`${name}_plant_leaf_${i + 1}`, new THREE.SphereGeometry(0.04, 8, 6), i % 2 ? M.leaf : M.leaf2, g, px + Math.cos(a) * 0.04, 0.73 + (i % 3) * 0.02, pz + Math.sin(a) * 0.04); lf.scale.set(0.6, 1.4, 0.6); lf.rotation.z = Math.cos(a) * 0.5; lf.rotation.x = -Math.sin(a) * 0.5; }
    return g; };
  nightstand('nightstand_left', 0.24); nightstand('nightstand_right', 2.56);

  // ---------------- moss & wood chevron art above the bed
  { const g = G('chevron_art', furn), x0 = BC - 0.66, x1 = BC + 0.66, y0 = 1.42, y1 = 2.05;
    B('chevron_art_frame', g, M.walnut, x0, x1, y0, y1, 0.0, 0.045, 0.006);
    mesh('chevron_art_panel', new THREE.PlaneGeometry(x1 - x0 - 0.06, y1 - y0 - 0.06), std('#ffffff', 0.95, 0, { map: T.chevron }), g, BC, (y0 + y1) / 2, 0.047, false); }
  // ---------------- two framed prints on the right (prints) wall
  for (const [k, z0, z1, y0, y1, tex] of [[1, 0.7, 1.18, 1.22, 1.88, T.printA], [2, 1.75, 2.55, 1.12, 1.98, T.printB]]) {
    const g = G(`framed_print_${k}`, furn);
    B(`framed_print_${k}_frame`, g, M.frameWood, W - 0.035, W, y0, y1, z0, z1, 0.005);
    const p = mesh(`framed_print_${k}_paper`, new THREE.PlaneGeometry(z1 - z0 - 0.05, y1 - y0 - 0.05), std('#ffffff', 0.9, 0, { map: tex }), g, W - 0.037, (y0 + y1) / 2, (z0 + z1) / 2, false); p.rotation.y = -Math.PI / 2; }

  // ---------------- walnut dresser on the FRONT wall (z = L), opposite the bed and facing it (plant, bowl, small brass lamp); wall-mounted TV above
  // (Dylan's correction: dresser on the wall opposite the bed, TV on the wall above it). Drawer fronts face -Z (toward the bed).
  const DRX0 = BC - 0.675, DRX1 = BC + 0.675, DRZF = L - 0.5, DRY = 0.82;
  { const g = G('dresser', furn);
    B('dresser_body', g, M.walnut, DRX0, DRX1, 0.1, DRY, DRZF, L - 0.01, 0.01);
    for (let r = 0; r < 3; r++) for (let c = 0; c < 2; c++) { const y0 = 0.14 + r * 0.225, cw = (DRX1 - DRX0 - 0.06) / 2, x0 = DRX0 + 0.03 + c * cw;
      B(`dresser_drawer_${r + 1}_${c + 1}`, g, M.walnutV, x0 + 0.01, x0 + cw - 0.01, y0, y0 + 0.2, DRZF - 0.002, DRZF + 0.012, 0.004);
      B(`dresser_pull_${r + 1}_${c + 1}`, g, M.brass, x0 + cw / 2 - 0.1, x0 + cw / 2 + 0.1, y0 + 0.17, y0 + 0.18, DRZF - 0.015, DRZF - 0.002, 0.003); }
    for (const [k, x] of [[1, DRX0 + 0.06], [2, DRX1 - 0.06]]) for (const [j, z] of [[1, DRZF + 0.06], [2, L - 0.06]]) cyl(`dresser_leg_${k}_${j}`, g, M.walnut, 0.02, 0.014, 0.1, x, 0.05, z, 10);
    // plant in white ceramic pot (right end) + wooden bowl (centre-right); the lamp sits on the left end
    const px = DRX1 - 0.2, pz = L - 0.24;
    cyl('dresser_plant_pot', g, M.ceramic, 0.08, 0.07, 0.16, px, DRY + 0.08, pz, 20);
    for (let i = 0; i < 9; i++) { const a = i / 9 * Math.PI * 2; const lf = mesh(`dresser_plant_leaf_${i + 1}`, new THREE.SphereGeometry(0.05, 8, 6), i % 2 ? M.leaf : M.leaf2, g, px + Math.cos(a) * 0.05, DRY + 0.24 + (i % 3) * 0.03, pz + Math.sin(a) * 0.05); lf.scale.set(0.55, 1.6, 0.55); lf.rotation.z = Math.cos(a) * 0.45; lf.rotation.x = -Math.sin(a) * 0.45; }
    const bowl = mesh('dresser_bowl', new THREE.SphereGeometry(0.1, 20, 10, 0, Math.PI * 2, Math.PI / 2, Math.PI / 2), M.walnutDark, g, DRX1 - 0.5, DRY + 0.06, L - 0.27); bowl.material = std('#5a3a22', 0.5, 0, { side: THREE.DoubleSide }); }

  // ================= DEVICES
  // ---------------- life: props (unmade throw, bedside book, kicked-off shoes, laundry basket)
  { const L = makeLifeKit();
    const knit = L.lstd('#ffffff', 1, 0, { map: L.knit('#6f8a6a'), side: THREE.DoubleSide });
    const g = L.LG('prop_throw_blanket', root, BC + 0.12, 0.662, 1.78, 0.32);                // crumpled on the duvet near the foot of the bed
    L.cloth('pm_throw_blanket_cloth', g, knit, 1.0, 0.62, (u, v) => {
      const r = 0.035 * Math.max(0, Math.sin(u * 9.0 + 0.6) * Math.sin(v * 6.5 + 1.1)) + 0.022 * Math.max(0, Math.sin(u * 17 + v * 5)) + 0.05 * Math.exp(-((u - 0.62) ** 2 + (v - 0.4) ** 2) / 0.02);
      return [0.02 * Math.sin(v * 9), 0.006 + r, 0.015 * Math.sin(u * 7)]; }, 28);
    L.P.book('prop_open_book', root, 2.88, 0.6, 0.22, -0.35, '#35506b', true);              // left open, face-up, on the right nightstand
    L.P.lamp('prop_lamp', root, 0.6, 0.6, 0.2, 0.3);                                       // little moon night-lamp on the left nightstand (night sparkle)
    L.P.shoes('prop_shoes', root, 2.85, 0.0, 2.6, 0.5, '#3f6fb0');                          // kicked off beside the bed
    L.P.laundryBasket('prop_laundry_basket', root, 0.45, 0.0, 4.1, 0.2);                    // corner past the doorway
  }

  // ---- brushed brass cone sconces (one each side of the bed), per sconce_ref.jpg
  const sconce = (name, label, x) => {
    const g = G(name, devGroup), y = 1.3, zA = 0.13;
    const plate = cyl(`${name}_backplate`, g, std('#b39260', 0.34, 0.92), 0.066, 0.066, 0.016, x, y, 0.008, 32); plate.rotation.x = Math.PI / 2;
    const arm = cyl(`${name}_arm`, g, M.brass, 0.011, 0.011, zA - 0.01, x, y, (zA + 0.01) / 2, 12); arm.rotation.x = Math.PI / 2;
    cyl(`${name}_arm_cup`, g, M.brass, 0.02, 0.02, 0.02, x, y, 0.025, 16).rotation.x = Math.PI / 2;
    sphere(`${name}_elbow`, g, M.brass, 0.0115, x, y, zA);
    cyl(`${name}_arm_drop`, g, M.brass, 0.011, 0.011, 0.07, x, y - 0.035, zA, 12);
    cyl(`${name}_knurled_collar`, g, M.brassDark, 0.024, 0.024, 0.04, x, y - 0.085, zA, 32);
    const sy1 = y - 0.105, sy0 = sy1 - 0.21;
    cyl(`${name}_shade`, g, M.brass, 0.024, 0.088, sy1 - sy0, x, (sy0 + sy1) / 2, zA, 40, true);
    const inner = std('#f3ead8', 0.6, 0, { side: THREE.BackSide, emissive: new THREE.Color(STYLE.warmHot), emissiveIntensity: 0 });
    cyl(`${name}_shade_lining`, g, inner, 0.022, 0.085, sy1 - sy0 - 0.004, x, (sy0 + sy1) / 2, zA, 40, true).castShadow = false;
    const bulb = std('#fff6e6', 0.4, 0, { emissive: new THREE.Color(STYLE.warmHot), emissiveIntensity: 0 });
    const disc = mesh(`${name}_bulb_disc`, new THREE.CircleGeometry(0.07, 32), bulb, g, x, sy0 + 0.03, zA, false); disc.rotation.x = Math.PI / 2;
    const decals = [
      decal(`glow_${name}_wall_pool`, g, T.sconcePool, STYLE.warm, 1.0, 0.7, 1.0, x, sy0 - 0.43, 0.004, '+z'),
      decal(`glow_${name}_wall_pool_core`, g, T.sconcePool, STYLE.warmHot, 0.8, 0.42, 0.75, x, sy0 - 0.33, 0.005, '+z'),
      decal(`glow_${name}_wall_halo`, g, T.glow, STYLE.warm, 0.45, 0.6, 0.6, x, y - 0.05, 0.005, '+z'),
      decal(`glow_${name}_nightstand_pool`, g, T.glow, STYLE.warm, 0.9, 0.55, 0.45, x, 0.605, 0.24, 'up'),
    ];
    const s = new THREE.SpotLight(STYLE.warm, 0, 3.2, 0.62, 0.6, 2); s.name = `light_${name}`; s.position.set(x, sy0 + 0.02, zA); s.target.position.set(x, 0.3, zA + 0.15); g.add(s); g.add(s.target);
    const fill = pl(`light_${name}_fill`, g, STYLE.warm, x, sy0 - 0.05, 0.3, 1.6);
    register(name, { room, label, type: 'light', group: g, emissive: [{ mat: inner, on: 1.4 }, { mat: bulb, on: 6 }], lights: [{ light: s, on: 9 }, { light: fill, on: 1.0 }], decals, anchor: new THREE.Vector3(x, y - 0.2, zA) });
  };
  sconce('sconce_left', 'Sconce (left)', 0.49); sconce('sconce_right', 'Sconce (right)', 2.81);

  // ---- recessed lights (floating discs: the ceiling is omitted)
  { const g = G('recessed_lights', devGroup), m = lightMats(), decals = [], show = [];
    const coneTex = canvasTex(8, 256, (c2, w, h) => { const gr = c2.createLinearGradient(0, 0, 0, h); gr.addColorStop(0, 'rgba(255,255,255,0.9)'); gr.addColorStop(1, 'rgba(255,255,255,0)'); c2.fillStyle = gr; c2.fillRect(0, 0, w, h); });
    const coneMat = new THREE.MeshBasicMaterial({ map: coneTex, color: STYLE.warm, transparent: true, opacity: 0.07, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide, toneMapped: false });
    const trim = std('#e4e2dc', 0.6);
    [[0.75, 0.85], [0.75, 3.65], [3.15, 3.65], [3.3, 0.5]].forEach(([x, z], i) => {
      cyl(`recessed_light_${i + 1}_trim`, g, trim, 0.075, 0.075, 0.012, x, H - 0.006, z, 20).castShadow = false;
      cyl(`recessed_light_${i + 1}`, g, m.bulb, 0.05, 0.05, 0.004, x, H - 0.013, z, 20).castShadow = false;
      const cone = mesh(`light_cone_recessed_${i + 1}`, new THREE.ConeGeometry(0.45, H - 0.08, 24, 1, true), coneMat, g, x, (H - 0.08) / 2 + 0.02, z, false); cone.visible = false; cone.renderOrder = 6; show.push(cone);
      decals.push(decal(`glow_recessed_${i + 1}_pool`, g, T.glow, STYLE.warm, 0.3, 1.0, 1.0, x, 0.004, z, 'up')); });
    const lights = [[1.2, 1.2], [2.6, 3.4]].map(([x, z]) => { const s = new THREE.SpotLight(STYLE.warm, 0, 6, 1.15, 0.7, 2); s.name = `light_recessed_${x}_${z}`.replace(/\./g, '_'); /* no '.' in node names (USDZ) */ s.position.set(x, H - 0.1, z); s.target.position.set(x, 0, z); g.add(s); g.add(s.target); return s; });
    register('recessed_lights', { room, label: 'Recessed lights', type: 'light', group: g, emissive: [{ mat: m.bulb, on: 6 }], lights: lights.map(l => ({ light: l, on: 6 })), decals, show, anchor: new THREE.Vector3(0.75, H - 0.08, 3.65) });
  }

  // ---- black 5-blade ceiling fan with light kit; spin cue (blur disc + streak arcs) when running
  const FX = 1.85, FZ = 2.35;
  { const g = G('ceiling_fan', devGroup), black = M.black;
    cyl('ceiling_fan_canopy', g, black, 0.06, 0.075, 0.06, FX, H - 0.03, FZ, 20);
    cyl('ceiling_fan_downrod', g, black, 0.012, 0.012, 0.2, FX, H - 0.16, FZ, 10);
    cyl('ceiling_fan_motor', g, black, 0.115, 0.125, 0.11, FX, H - 0.31, FZ, 28);
    const blades = G('ceiling_fan_blades', g); blades.position.set(FX, H - 0.31, FZ);
    // app spins ceiling_fan_blades on Y (ios/Rooms/README.md): origin = the fan hub on the motor axis
    blades.userData = { pivotAxis: 'y', pivotNote: 'origin at the fan hub on the motor axis; spin rotation.y' };
    for (let i = 0; i < 5; i++) { const a = i / 5 * Math.PI * 2, arm = new THREE.Group(); arm.name = `ceiling_fan_blade_arm_${i + 1}`; arm.rotation.y = a; blades.add(arm);
      B(`ceiling_fan_iron_${i + 1}`, arm, black, 0.08, 0.2, -0.012, 0.0, -0.02, 0.02);
      const bl = B(`ceiling_fan_blade_${i + 1}`, arm, M.blackMatte, 0.18, 0.74, -0.018, -0.008, -0.075, 0.075, 0.004); bl.rotation.x = 0.12; }
    const kitMat = std('#f2efe8', 0.35, 0, { emissive: new THREE.Color(STYLE.warmHot), emissiveIntensity: 0 });
    cyl('ceiling_fan_light_housing', g, black, 0.085, 0.07, 0.04, FX, H - 0.385, FZ, 24);
    mesh('ceiling_fan_light_bowl', new THREE.SphereGeometry(0.075, 24, 12, 0, Math.PI * 2, Math.PI / 2, Math.PI / 2), kitMat, g, FX, H - 0.403, FZ);
    cyl('ceiling_fan_pull_chain', g, std('#9a9a9a', 0.4, 0.8), 0.003, 0.003, 0.18, FX + 0.03, H - 0.55, FZ, 6);
    const spinMat = new THREE.MeshBasicMaterial({ map: T.spin, color: '#d8d2c8', transparent: true, opacity: 0.55, depthWrite: false, side: THREE.DoubleSide, toneMapped: false });
    const blur = mesh('ceiling_fan_spin_blur', new THREE.CircleGeometry(0.8, 48), spinMat, g, FX, H - 0.322, FZ, false); blur.rotation.x = -Math.PI / 2; blur.renderOrder = 7; blur.visible = false;
    const decals = [decal('glow_ceiling_fan_floor', g, T.glow, STYLE.warm, 0.4, 2.2, 2.2, FX, 0.004, FZ, 'up'), decal('glow_ceiling_fan_bed', g, T.glow, STYLE.warm, 0.3, 1.4, 1.2, BC, 0.69, 1.6, 'up')];
    const l = pl('light_ceiling_fan', g, STYLE.warm, FX, H - 0.5, FZ, 5.5);
    register('ceiling_fan', { room, label: 'Ceiling fan + light', type: 'fan', group: g, emissive: [{ mat: kitMat, on: 3 }], lights: [{ light: l, on: 2.0 }], decals, show: [blur],
      custom: (on) => { blades.rotation.y = on ? 0.35 : 0; blades.userData.spinning = on; }, anchor: new THREE.Vector3(FX, H - 0.42, FZ) });
  }

  // ---- skylight (light-tube style): floating white well with a blue diffuser and blue glow
  { const g = G('skylight', devGroup), x0 = 2.85, x1 = 3.35, z0 = 0.95, z1 = 2.05, d = 0.06;
    B('skylight_well_a', g, M.white, x0 - 0.04, x0, H - 0.02, H + d, z0 - 0.04, z1 + 0.04); B('skylight_well_b', g, M.white, x1, x1 + 0.04, H - 0.02, H + d, z0 - 0.04, z1 + 0.04);
    B('skylight_well_c', g, M.white, x0, x1, H - 0.02, H + d, z0 - 0.04, z0); B('skylight_well_d', g, M.white, x0, x1, H - 0.02, H + d, z1, z1 + 0.04);
    const dif = std('#cfdbe6', 0.3, 0, { emissive: new THREE.Color('#7fb2ff'), emissiveIntensity: 0 });
    B('skylight_tube_cap', g, dif, x0, x1, H + d - 0.01, H + d, z0, z1);
    const p = mesh('skylight_diffuser', new THREE.PlaneGeometry(x1 - x0, z1 - z0), dif, g, (x0 + x1) / 2, H - 0.005, (z0 + z1) / 2, false); p.rotation.x = Math.PI / 2;
    const coneTex = canvasTex(8, 256, (c2, w, h) => { const gr = c2.createLinearGradient(0, 0, 0, h); gr.addColorStop(0, 'rgba(255,255,255,0.9)'); gr.addColorStop(1, 'rgba(255,255,255,0)'); c2.fillStyle = gr; c2.fillRect(0, 0, w, h); });
    const shaftMat = new THREE.MeshBasicMaterial({ map: coneTex, color: '#7fb2ff', transparent: true, opacity: 0.16, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide, toneMapped: false });
    const shaft = mesh('light_shaft_skylight', new THREE.CylinderGeometry(0.42, 0.85, H - 0.1, 4, 1, true), shaftMat, g, (x0 + x1) / 2, (H - 0.1) / 2 + 0.02, (z0 + z1) / 2, false);
    shaft.scale.set(0.75, 1, 1.6); shaft.rotation.y = Math.PI / 4; shaft.renderOrder = 6; shaft.visible = false;
    const decals = [decal('glow_skylight_floor', g, T.glow, '#7fb2ff', 0.45, 1.5, 2.2, (x0 + x1) / 2, 0.004, (z0 + z1) / 2, 'up'),
      decal('glow_skylight_wall', g, T.glow, '#7fb2ff', 0.3, 2.0, 1.4, W - 0.004, H - 0.6, (z0 + z1) / 2, '-x')];
    decals[1].rotation.y = -Math.PI / 2;
    const l = pl('light_skylight', g, '#8fbaff', (x0 + x1) / 2, H - 0.25, (z0 + z1) / 2, 4.5);
    register('skylight_glow', { room, label: 'Skylight glow', type: 'light', group: g, emissive: [{ mat: dif, on: 3.2 }], lights: [{ light: l, on: 2.2 }], decals, show: [shaft], anchor: new THREE.Vector3((x0 + x1) / 2, H - 0.05, (z0 + z1) / 2) });
  }

  // ---- small brass lamp on the dresser (dome shade), left end; the arm reaches toward the bed (-Z).
  // Built in the lamp's own frame (arm along local -X) inside `dresser_lamp_body`, turned -90° so local -X = world -Z.
  { const g = G('dresser_lamp', devGroup), y0 = DRY, LX = DRX0 + 0.26, LZ = L - 0.24;
    const body = G('dresser_lamp_body', g); body.position.set(LX, 0, LZ); body.rotation.y = -Math.PI / 2;
    const x = 0, z = 0;
    cyl('dresser_lamp_base', body, M.brass, 0.07, 0.075, 0.02, x, y0 + 0.01, z, 28);
    cyl('dresser_lamp_stem', body, M.brass, 0.008, 0.008, 0.3, x, y0 + 0.17, z, 10);
    const arm = cyl('dresser_lamp_arm', body, M.brass, 0.007, 0.007, 0.12, x - 0.05, y0 + 0.33, z, 10); arm.rotation.z = Math.PI / 2 - 0.4;
    const shadeY = y0 + 0.33, sx = x - 0.12;
    const shade = cyl('dresser_lamp_shade', body, M.brass, 0.03, 0.075, 0.1, sx, shadeY, z, 32, true); shade.rotation.z = 0.35;
    const inner = std('#f3ead8', 0.6, 0, { side: THREE.BackSide, emissive: new THREE.Color(STYLE.warmHot), emissiveIntensity: 0 });
    const lin = cyl('dresser_lamp_shade_lining', body, inner, 0.028, 0.072, 0.096, sx, shadeY, z, 32, true); lin.rotation.z = 0.35; lin.castShadow = false;
    const decals = [decal('glow_dresser_lamp_top', body, T.glow, STYLE.warm, 0.75, 0.7, 0.7, sx - 0.05, y0 + 0.004, z - 0.05, 'up'),
      decal('glow_dresser_lamp_wall', body, T.glow, STYLE.warm, 0.4, 0.9, 0.8, 0.236, y0 + 0.35, z, '-x')];
    const l = pl('light_dresser_lamp', body, STYLE.warm, sx - 0.03, shadeY - 0.06, z, 2.6);
    // world anchor = body frame point (sx, shadeY + 0.12, 0): local -X -> world -Z
    register('dresser_lamp', { room, label: 'Dresser lamp', type: 'light', group: g, emissive: [{ mat: inner, on: 2.5 }], lights: [{ light: l, on: 1.4 }], decals, anchor: new THREE.Vector3(LX, shadeY + 0.12, LZ + sx) });
  }
  // ---- wall-mounted TV above the dresser (front wall, facing the bed). The front wall is cut at 1.1 m in dollhouse views, so the TV
  // floats on its bracket there (like the recessed cans); interior views show it on the full wall. tv_screen glows when on.
  { const g = G('tv', devGroup), TW = 1.23, TH = 0.70, TY = 1.62, TZ = L - 0.07, cx = BC;
    const tvTex = canvasTex(1024, 576, (c, w, h) => { const sky = c.createLinearGradient(0, 0, 0, h); sky.addColorStop(0, '#1d3557'); sky.addColorStop(0.55, '#457b9d'); sky.addColorStop(1, '#a8dadc'); c.fillStyle = sky; c.fillRect(0, 0, w, h);
      c.fillStyle = '#f1faee'; c.beginPath(); c.arc(w * 0.72, h * 0.3, 46, 0, 7); c.fill();
      c.fillStyle = '#2a4a3a'; c.beginPath(); c.moveTo(0, h * 0.75); for (let x = 0; x <= w; x += 32) c.lineTo(x, h * (0.68 + 0.06 * Math.sin(x / 90))); c.lineTo(w, h); c.lineTo(0, h); c.fill();
      c.fillStyle = '#1b3328'; c.beginPath(); c.moveTo(0, h * 0.86); for (let x = 0; x <= w; x += 32) c.lineTo(x, h * (0.82 + 0.04 * Math.sin(x / 60 + 1))); c.lineTo(w, h); c.lineTo(0, h); c.fill(); });
    B('tv_wall_bracket', g, M.black, cx - 0.2, cx + 0.2, TY - 0.15, TY + 0.15, TZ + 0.025, L, 0.004);
    B('tv_body', g, std('#111113', 0.5), cx - TW / 2, cx + TW / 2, TY - TH / 2, TY + TH / 2, TZ - 0.025, TZ + 0.025, 0.006);
    const scrMat = std('#060607', 0.12, 0.3);
    const scr = mesh('tv_screen', new THREE.PlaneGeometry(TW - 0.03, TH - 0.03), scrMat, g, cx, TY, TZ - 0.026, false); scr.rotation.y = Math.PI;
    B('tv_logo', g, std('#8a8d92', 0.3, 0.8), cx + TW / 2 - 0.06, cx + TW / 2 - 0.05, TY - TH / 2 + 0.005, TY - TH / 2 + 0.012, TZ - 0.028, TZ - 0.025);
    const glow = decal('glow_tv_room', g, T.glow, '#9fc3ff', 0.35, 2.4, 1.6, cx, 0.006, TZ - 1.0, 'up');
    const l = pl('light_tv', g, '#9fc3ff', cx, TY, TZ - 0.6, 3.0);
    register('tv', { room, label: 'TV', type: 'media', group: g, lights: [{ light: l, on: 0.8 }], decals: [glow], anchor: new THREE.Vector3(cx, TY, TZ - 0.06),
      custom: (on) => {
        if (on) { scrMat.color.set('#ffffff'); scrMat.map = tvTex; scrMat.emissive.set('#ffffff'); scrMat.emissiveMap = tvTex; scrMat.emissiveIntensity = 1.2; scrMat.roughness = 0.4; scrMat.metalness = 0; }
        else { scrMat.color.set('#060607'); scrMat.map = null; scrMat.emissive.set('#000000'); scrMat.emissiveMap = null; scrMat.emissiveIntensity = 0; scrMat.roughness = 0.12; scrMat.metalness = 0.3; }
        scrMat.needsUpdate = true; } });
  }

  Object.keys(K.devices).forEach(n => K.setDevice(n, false));
  return { root, devices: K.devices, setDevice: K.setDevice, textures: T };
}

// Preset values: 1 = on, fractions = dimmed (common.js setDevice accepts a 0..1 level)
export const PRESETS = {
  all_off: {},
  all_on: { sconce_left: 1, sconce_right: 1, recessed_lights: 1, ceiling_fan: 1, skylight_glow: 1, dresser_lamp: 1, tv: 1 },
  bedtime: { sconce_left: 0.3, sconce_right: 0.3 },              // TV off at bedtime
  reading: { sconce_left: 1, sconce_right: 1 },
};

export const VIEWS = {
  dollhouse: { pos: [-5.0, 9.4, 9.6], target: [2.0, 0.45, 2.1], fov: 31 },
  hero: { pos: [-3.6, 5.0, 8.0], target: [2.0, 0.8, 1.7], fov: 36 },
  photo: { pos: [0.3, 1.55, 4.35], target: [2.75, 1.05, 0.35], fov: 64, interior: true },
  sconces: { pos: [1.65, 1.35, 2.6], target: [1.65, 1.05, 0.0], fov: 58, interior: true },
  sconce_close: { pos: [2.35, 1.2, 0.95], target: [2.81, 1.05, 0.05], fov: 40, interior: true },
  ceiling: { pos: [0.6, 1.0, 4.2], target: [2.5, 2.45, 1.4], fov: 62, interior: true },
  dresser: { pos: [1.65, 1.5, 1.1], target: [1.65, 1.15, 4.5], fov: 62, interior: true },        // from the bed toward the dresser + TV
  top: { pos: [2.0, 15.5, 7.0], target: [2.0, 0, 2.25], fov: 32 },
  life_props: { pos: [3.15, 1.8, 4.42], target: [1.35, 0.35, 2.3], fov: 72, interior: true },
  life_bedside: { pos: [1.65, 1.45, 1.55], target: [1.65, 0.6, 0.2], fov: 62, interior: true },
};

export const createBedroom = (container, opts = {}) => createApp(container, { build: buildBedroom, views: VIEWS, presets: PRESETS, shadow: { center: [2.0, 0, 2.25], half: 6 }, ...opts });
