// Basement movie room — Room 03 of the dollhouse. Follows /workspace/room/style.md and reuses kitchen/src/common.js.
// Coordinates: metres, Y up. Kept (standing) walls: SCREEN WALL = plane z=0 and FIREPLACE WALL = plane x=0.
// Interior x>0, z>0. Facing the screen (-z) the fireplace wall is on your left. Rear seating + riser at large z (foreground).
// Right wall (x=W) is cut at 1.1 m (upper part interior-only). The rear edge (z=L) has NO wall: it is an open boundary
// closed by a row of reeded-glass panels in brass frames with a black curtain behind them (another part of the basement beyond).
// Seating: two rows facing the screen (-z), pushed right so their right ends touch the right wall; back row on a riser behind the front row.
import * as THREE from 'three';
import { STYLE, rng, canvasTex, makeBaseTextures, std, makeKit, createApp } from '../../kitchen/src/common.js';

export const MROOM = { W: 5.4, L: 8.6, H: 2.6 };

function movieTextures() {
  const carpet = canvasTex(256, 256, (g, w, h) => { g.fillStyle = '#cbb999'; g.fillRect(0, 0, w, h); const r = rng(3);
    for (let i = 0; i < 5000; i++) { const v = r(); g.fillStyle = v < 0.5 ? 'rgba(120,100,70,0.10)' : 'rgba(255,245,225,0.12)'; g.fillRect(r() * w, r() * h, 1.5, 1.5); } });
  carpet.wrapS = carpet.wrapT = THREE.RepeatWrapping;
  const stone = canvasTex(512, 512, (g, w, h) => {
    g.fillStyle = '#7c746b'; g.fillRect(0, 0, w, h); const r = rng(19);
    const cols = ['#9a9184', '#b3a796', '#857b70', '#a69a8a', '#c2b6a3', '#8d8579', '#77706a', '#b9a58c'];
    for (let i = 0; i < 150; i++) { const x = r() * w, y = r() * h, rx = 22 + r() * 30, ry = 16 + r() * 18, a = (r() - 0.5) * 0.6;
      for (const [ox, oy] of [[0, 0], [w, 0], [-w, 0], [0, h], [0, -h]]) { const gr = g.createRadialGradient(x + ox - rx * 0.3, y + oy - ry * 0.4, 1, x + ox, y + oy, rx);
        const c = cols[Math.floor(r() * cols.length)]; gr.addColorStop(0, '#efe7da'); gr.addColorStop(0.3, c); gr.addColorStop(0.85, c); gr.addColorStop(1, '#5a534c');
        g.fillStyle = gr; g.beginPath(); g.ellipse(x + ox, y + oy, rx, ry, a, 0, 7); g.fill(); } }
  });
  stone.wrapS = stone.wrapT = THREE.RepeatWrapping;
  const movie = canvasTex(1024, 576, (g, w, h) => {
    const gr = g.createLinearGradient(0, 0, 0, h); gr.addColorStop(0, '#1d2a5c'); gr.addColorStop(0.45, '#c8573a'); gr.addColorStop(0.62, '#f3b45a'); gr.addColorStop(1, '#3a1b16');
    g.fillStyle = gr; g.fillRect(0, 0, w, h);
    g.fillStyle = '#ffe2a0'; g.beginPath(); g.arc(w * 0.62, h * 0.6, 58, 0, 7); g.fill();
    g.fillStyle = '#2b1410'; g.beginPath(); g.moveTo(0, h); for (let x = 0; x <= w; x += 8) g.lineTo(x, h * 0.66 - 40 * Math.sin(x * 0.006) - 18 * Math.sin(x * 0.021)); g.lineTo(w, h); g.fill();
    g.fillStyle = '#140806'; g.beginPath(); g.moveTo(0, h); for (let x = 0; x <= w; x += 8) g.lineTo(x, h * 0.8 - 25 * Math.sin(x * 0.009 + 2)); g.lineTo(w, h); g.fill();
    // rider silhouette
    g.fillStyle = '#0d0504'; g.save(); g.translate(w * 0.36, h * 0.74);
    g.fillRect(-40, -34, 80, 26); g.fillRect(30, -58, 14, 30); g.beginPath(); g.ellipse(44, -60, 16, 9, -0.4, 0, 7); g.fill();
    for (const lx of [-34, -20, 20, 32]) g.fillRect(lx, -10, 6, 34); g.fillRect(-8, -70, 14, 38); g.beginPath(); g.arc(-1, -76, 9, 0, 7); g.fill(); g.restore();
    g.fillStyle = '#000'; g.fillRect(0, 0, w, 52); g.fillRect(0, h - 52, w, 52);
  });
  const ribs = canvasTex(256, 16, (g, w, h) => { for (let x = 0; x < w; x++) { const t = 0.5 + 0.5 * Math.cos((x / w) * Math.PI * 2 * 14); const v = Math.round(170 + 85 * t); g.fillStyle = `rgb(${v},${v},${v})`; g.fillRect(x, 0, 1, h); } });
  const rug = canvasTex(512, 384, (g, w, h) => { const r = rng(31);
    g.fillStyle = '#8d6a52'; g.fillRect(0, 0, w, h);
    g.strokeStyle = '#3e2a22'; g.lineWidth = 26; g.strokeRect(13, 13, w - 26, h - 26);
    g.strokeStyle = '#d9c4a2'; g.lineWidth = 4; g.strokeRect(34, 34, w - 68, h - 68);
    for (let i = 0; i < 1200; i++) { g.fillStyle = `rgba(${r() < 0.5 ? '60,40,30' : '220,200,170'},${0.06 + r() * 0.1})`; g.fillRect(r() * w, r() * h, 2 + r() * 8, 1 + r() * 2); }
    g.strokeStyle = 'rgba(220,196,160,0.6)'; g.lineWidth = 5; g.beginPath(); g.ellipse(w / 2, h / 2, 130, 80, 0, 0, 7); g.stroke();
    g.fillStyle = 'rgba(120,40,40,0.5)'; g.beginPath(); g.ellipse(w / 2, h / 2, 80, 46, 0, 0, 7); g.fill(); });
  const popSign = canvasTex(256, 64, (g, w, h) => { g.fillStyle = '#b3121b'; g.fillRect(0, 0, w, h); g.fillStyle = '#ffe9a8'; g.font = 'bold 40px sans-serif'; g.textAlign = 'center'; g.fillText('POPCORN', w / 2, 46); });
  const walnut = canvasTex(256, 256, (g, w, h) => { g.fillStyle = '#5a3a22'; g.fillRect(0, 0, w, h); const r = rng(8);
    for (let i = 0; i < 60; i++) { g.strokeStyle = `rgba(30,16,8,${0.1 + r() * 0.2})`; g.lineWidth = 1 + r() * 2; g.beginPath(); const y = r() * h; g.moveTo(0, y); g.bezierCurveTo(w * 0.3, y + (r() - 0.5) * 20, w * 0.6, y + (r() - 0.5) * 20, w, y + (r() - 0.5) * 10); g.stroke(); } });
  return { carpet, stone, movie, ribs, rug, popSign, walnut };
}

export function buildMovieRoom() {
  const T = { ...makeBaseTextures(), ...movieTextures() };
  const { W, L, H } = MROOM, WT = 0.22, CUT = 1.1;
  const root = new THREE.Group(); root.name = 'movie_room';
  const K = makeKit(root, T);
  const { G, mesh, B, cyl, sphere, decal, lightMats, pl, register, portal, extMat, extMesh } = K;
  const M = {
    cut: std(STYLE.cut, 0.95), slab: std(STYLE.slab, 0.95),
    wall: std('#7a1622', 0.88), ceiling: std('#c4552c', 0.9), trim: std('#f1ece2', 0.5),
    carpet: (rx, rz) => { const t = T.carpet.clone(); t.needsUpdate = true; t.repeat.set(rx, rz); return std('#ffffff', 1, 0, { map: t }); },
    velvet: std('#8c0f1d', 0.8, 0, { side: THREE.DoubleSide }), blackCurtain: std('#121212', 0.9, 0, { side: THREE.DoubleSide }),
    black: std('#141414', 0.5), blackMatte: std('#0c0c0c', 0.85), leather: std('#5b3922', 0.5), leatherDark: std('#3f2716', 0.55),
    walnut: std('#ffffff', 0.55, 0, { map: T.walnut }), darkWood: std('#2e1d13', 0.6), brass: std('#c19a50', 0.3, 0.85),
    stone: (rx, ry) => { const t = T.stone.clone(); t.needsUpdate = true; t.repeat.set(rx, ry); return std('#ffffff', 0.9, 0, { map: t }); },
    soot: std('#151110', 0.95), white: std('#efedea', 0.45), red: std('#b3121b', 0.45, 0.1), steel: std('#c6c9cc', 0.3, 0.85),
    glass: new THREE.MeshPhysicalMaterial({ color: '#a9bfd6', roughness: 0.05, transparent: true, opacity: 0.1, depthWrite: false }),
    cabGlass: new THREE.MeshPhysicalMaterial({ color: '#ffffff', roughness: 0.05, transparent: true, opacity: 0.14, depthWrite: false, side: THREE.DoubleSide }),
  };
  const arch = G('architecture'), openings = G('doors_and_windows'), furn = G('furniture'), fire = G('fireplace'), exterior = G('exterior_patio'), devGroup = G('devices');
  const io = (m) => { m.userData.interiorOnly = true; return m; };

  // ---------------- floor, walls, ceiling band
  B('floor_slab', arch, M.slab, -WT, W + WT, -0.3, -0.02, -WT, L + WT);
  B('floor_carpet', arch, M.carpet(W / 1.2, L / 1.2), 0, W, -0.02, 0, 0, L + 0.2);   // runs under the rear curtain (open boundary)
  // fireplace wall x=0 with two French-door holes (extruded so the cut faces take the section colour)
  const FD = [{ z0: 1.0, z1: 2.2 }, { z0: 4.85, z1: 6.05 }], DH = 2.15;
  { const sh = new THREE.Shape(); sh.moveTo(-WT, 0); sh.lineTo(L + WT, 0); sh.lineTo(L + WT, H); sh.lineTo(-WT, H); sh.lineTo(-WT, 0);
    FD.forEach(({ z0, z1 }) => { const p = new THREE.Path(); p.moveTo(z0, 0.003); p.lineTo(z1, 0.003); p.lineTo(z1, DH); p.lineTo(z0, DH); p.lineTo(z0, 0.003); sh.holes.push(p); });
    const m = mesh('wall_fireplace', new THREE.ExtrudeGeometry(sh, { depth: WT, bevelEnabled: false }), [M.wall, M.cut], arch, 0, 0, 0); m.rotation.y = -Math.PI / 2; }
  B('wall_screen', arch, M.wall, 0, W + WT, 0, H, -WT, 0).material = [M.cut, M.wall, M.cut, M.wall, M.wall, M.wall];
  // camera-side walls cut at 1.1 m (upper parts interior-only)
  const RD = { z0: 0.45, z1: 1.35, h: 2.05 };   // white door on the right wall near the screen
  const rightWall = (n, z0, z1, y0, y1) => B(n, arch, M.wall, W, W + WT, y0, y1, z0, z1);
  rightWall('wall_right_a', 0, RD.z0, 0, CUT).material = [M.wall, M.wall, M.cut, M.wall, M.cut, M.cut];
  rightWall('wall_right_b', RD.z1, L + WT, 0, CUT).material = [M.wall, M.wall, M.cut, M.wall, M.cut, M.cut];
  io(rightWall('wall_right_upper', 0, L + WT, CUT, H)); io(rightWall('wall_right_over_door', RD.z0, RD.z1, CUT, H));
  // ceiling: shown as a red-orange band along the two standing walls (ceiling itself omitted), crown molding below it
  B('ceiling_band_fireplace_wall', arch, M.ceiling, 0, 0.5, H, H + 0.1, 0, L).material = [M.cut, M.ceiling, M.cut, M.ceiling, M.cut, M.cut];
  B('ceiling_band_screen_wall', arch, M.ceiling, 0.5, W, H, H + 0.1, 0, 0.5).material = [M.cut, M.cut, M.cut, M.ceiling, M.cut, M.ceiling];
  B('crown_fireplace_wall', arch, M.trim, 0, 0.08, H - 0.1, H, 0, L, 0.01); B('crown_screen_wall', arch, M.trim, 0, W, H - 0.1, H, 0, 0.08, 0.01);
  io(B('crown_right_wall', arch, M.trim, W - 0.08, W, H - 0.1, H, 0, L, 0.01));
  B('baseboard_screen_wall', arch, M.trim, 0, W, 0, 0.1, 0, 0.018);

  // ---------------- French doors to the lower porch (den style: 2 leaves, 5 rows, white trim, transom)
  const portalMesh = [];
  FD.forEach(({ z0, z1 }, di) => {
    const g = G(`french_door_${di + 1}`, openings), n = (s) => `french_door_${di + 1}_${s}`, top = 1.86;
    B(n('jamb_l'), g, M.trim, -WT, 0, 0, DH, z0, z0 + 0.03); B(n('jamb_r'), g, M.trim, -WT, 0, 0, DH, z1 - 0.03, z1); B(n('jamb_head'), g, M.trim, -WT, 0, DH - 0.03, DH, z0, z1);
    B(n('casing_l'), g, M.trim, 0, 0.025, 0, DH + 0.09, z0 - 0.09, z0 + 0.005); B(n('casing_r'), g, M.trim, 0, 0.025, 0, DH + 0.09, z1 - 0.005, z1 + 0.09);
    B(n('casing_head'), g, M.trim, 0, 0.03, DH, DH + 0.12, z0 - 0.11, z1 + 0.11, 0.005);
    const xa = -0.13, xb = -0.08, a0 = z0 + 0.03, a1 = z1 - 0.03, mid = (a0 + a1) / 2;
    for (const [k, la, lb] of [[1, a0, mid - 0.004], [2, mid + 0.004, a1]]) {
      B(n(`leaf${k}_stile_a`), g, M.trim, xa, xb, 0.02, top, la, la + 0.07); B(n(`leaf${k}_stile_b`), g, M.trim, xa, xb, 0.02, top, lb - 0.07, lb);
      B(n(`leaf${k}_rail_bottom`), g, M.trim, xa, xb, 0.02, 0.22, la, lb); B(n(`leaf${k}_rail_top`), g, M.trim, xa, xb, top - 0.07, top, la, lb);
      B(n(`leaf${k}_glass`), g, M.glass, -0.108, -0.102, 0.22, top - 0.07, la + 0.07, lb - 0.07).castShadow = false;
      const c = (la + lb) / 2; B(n(`leaf${k}_muntin_v`), g, M.trim, xa + 0.008, xb - 0.008, 0.22, top - 0.07, c - 0.011, c + 0.011);
      for (let r = 1; r < 5; r++) { const y = 0.22 + (top - 0.29) * r / 5; B(n(`leaf${k}_muntin_h_${r}`), g, M.trim, xa + 0.008, xb - 0.008, y - 0.011, y + 0.011, la + 0.07, lb - 0.07); }
      B(n(`leaf${k}_lever`), g, M.brass, -0.08, -0.04, 1.0, 1.02, k === 1 ? lb - 0.09 : la + 0.07, k === 1 ? lb - 0.07 : la + 0.09, 0.005);
    }
    B(n('transom_bar'), g, M.trim, xa, xb, top, top + 0.05, a0, a1);
    B(n('transom_glass'), g, M.glass, -0.108, -0.102, top + 0.05, DH - 0.03, a0, a1).castShadow = false;
    for (let k = 1; k < 4; k++) { const z = a0 + (a1 - a0) * k / 4; B(n(`transom_muntin_${k}`), g, M.trim, xa + 0.008, xb - 0.008, top + 0.05, DH - 0.03, z - 0.011, z + 0.011); }
    const p = portal(n('portal'), g, new THREE.PlaneGeometry(a1 - a0, DH - 0.05)); p.rotation.y = Math.PI / 2; p.position.set(-0.16, (DH - 0.05) / 2 + 0.02, (a0 + a1) / 2); portalMesh.push(p);
  });
  // lower porch at night (only through the glass)
  { const E = (nm, mat, ...a) => extMesh(B(nm, exterior, mat, ...a));
    const paverA = extMat('#4c5164'), paverB = extMat('#444a5c');
    E('porch_slab', extMat('#2a2e3b'), -4.5, -WT, -0.3, -0.04, -1, L + 1);
    for (let i = 0, z = -1; z < L + 1; i++, z += 0.6) for (let j = 0, x = -4.5; x < -WT; j++, x += 0.6) E(`porch_paver_${i}_${j}`, (i + j) % 2 ? paverA : paverB, x + 0.01, Math.min(x + 0.59, -WT), -0.04, -0.02, z + 0.01, z + 0.59);
    for (const z of [0.4, 3.5, 6.6]) E(`porch_post_${z}`, extMat('#5c6380'), -2.9, -2.7, -0.02, 2.6, z, z + 0.2);
    E('porch_beam', extMat('#545b77'), -2.95, -2.65, 2.4, 2.65, -1, L + 1);
    E('porch_deck_over', extMat('#1d2130'), -3.0, -WT, 2.62, 2.75, -1, L + 1);
    E('porch_lawn', extMat('#1b2a26'), -14, -4.5, -0.06, -0.04, -6, L + 6);
    for (const [k, x, z, r] of [[1, -5.2, 1.0, 0.6], [2, -5.5, 4.0, 0.8], [3, -5.0, 6.8, 0.55]]) extMesh(sphere(`porch_shrub_${k}`, exterior, extMat('#22362c'), r, x, r * 0.7, z));
    const sky = extMesh(mesh('porch_night_sky', new THREE.PlaneGeometry(30, 12), extMat('#ffffff', { map: T.sky }), exterior, -8, 3.5, L / 2, false)); sky.rotation.y = Math.PI / 2; sky.userData.noExport = true;
  }

  // ---------------- curtains (folded planes)
  const curtain = (name, parent, mat, w, h, x, y, z, alongZ, folds = 7, amp = 0.035) => {
    const geo = new THREE.PlaneGeometry(w, h, folds * 6, 1); const p = geo.attributes.position;
    for (let i = 0; i < p.count; i++) p.setZ(i, amp * Math.sin((p.getX(i) / w) * Math.PI * 2 * folds));
    geo.computeVertexNormals(); const m = mesh(name, geo, mat, parent, x, y, z); if (alongZ) m.rotation.y = Math.PI / 2; return m;
  };
  const rod = (name, parent, x0, x1, y, z0, z1) => B(name, parent, M.brass, x0, x1, y - 0.012, y + 0.012, z0, z1, 0.01);
  // flanking the fireplace (on the fireplace wall)
  for (const [k, zc] of [[1, 2.42], [2, 4.6]]) { curtain(`fireplace_curtain_${k}`, furn, M.velvet, 0.38, H - 0.22, 0.09, (H - 0.22) / 2 + 0.02, zc, true); rod(`fireplace_curtain_rod_${k}`, furn, 0.06, 0.12, H - 0.16, zc - 0.25, zc + 0.25); }
  // screen-wall curtains (both sides of the screen)
  const SC = 2.7, SW = 2.5, SH = 1.4, SY0 = 0.78;
  for (const [k, xc] of [[1, SC - SW / 2 - 0.42], [2, SC + SW / 2 + 0.42]]) curtain(`screen_curtain_${k}`, furn, M.velvet, 0.62, H - 0.18, xc, (H - 0.18) / 2 + 0.01, 0.12, false, 8, 0.045);
  rod('screen_curtain_rod', furn, SC - SW / 2 - 0.85, SC + SW / 2 + 0.85, H - 0.13, 0.1, 0.14);
  // black curtain wall behind the back row (full height in interior views, cut at the wall height in the dollhouse)
  // (the open rear edge; reeded-glass panels hang flat against it; full height inside, cut at 1.1 m in the dollhouse)
  { const cw = W + 0.1; const full = curtain('rear_black_curtain', furn, M.blackCurtain, cw, H - 0.04, W / 2, (H - 0.04) / 2, L + 0.12, false, 18, 0.04); io(full);
    const low = curtain('rear_black_curtain_cut', furn, M.blackCurtain, cw, CUT, W / 2, CUT / 2, L + 0.12, false, 18, 0.03); low.userData.dollhouseOnly = true;
    rod('rear_curtain_track', furn, -0.05, W + 0.05, H - 0.03, L + 0.1, L + 0.14); }

  // ---------------- screen wall: screen, console, art; right wall door
  { const g = G('projection_screen', furn);
    B('screen_frame', g, M.blackMatte, SC - SW / 2 - 0.07, SC + SW / 2 + 0.07, SY0 - 0.07, SY0 + SH + 0.07, 0.03, 0.1, 0.01);
  }
  const screenMat = std('#e8e8e4', 0.85, 0, { emissive: new THREE.Color('#ffffff'), emissiveIntensity: 0 });
  mesh('screen_surface', new THREE.PlaneGeometry(SW, SH), screenMat, furn, SC, SY0 + SH / 2, 0.102, false);
  // (nothing under the screen: media console removed per Dylan)
  // framed art: on the right wall between the door and the dresser (the RGB flower light now owns the right screen corner).
  // It sits above the 1.1 m cut, so it only shows in interior views.
  { const g = G('framed_art', furn), z0 = 1.6, z1 = 2.2, y0 = 1.2, y1 = 2.0;
    io(B('framed_art_frame', g, std('#1e1a17', 0.4), W - 0.035, W, y0, y1, z0, z1, 0.006));
    const art = canvasTex(128, 170, (c, w, h) => { c.fillStyle = '#efe6d2'; c.fillRect(0, 0, w, h); c.fillStyle = '#b3121b'; c.fillRect(18, 22, w - 36, 60); c.fillStyle = '#1d1d1d'; c.font = 'bold 15px serif'; c.textAlign = 'center'; c.fillText('NOW', w / 2, 112); c.fillText('SHOWING', w / 2, 132); c.fillStyle = '#d9a54a'; c.fillRect(30, 146, w - 60, 6); });
    const pr = mesh('framed_art_print', new THREE.PlaneGeometry(z1 - z0 - 0.1, y1 - y0 - 0.1), std('#ffffff', 0.9, 0, { map: art }), g, W - 0.037, (y0 + y1) / 2, (z0 + z1) / 2, false); pr.rotation.y = -Math.PI / 2; io(pr); }
  { const g = G('right_wall_door', openings), { z0, z1, h } = RD;
    const leafLow = B('right_door_leaf', g, M.white, W - 0.04, W, 0, CUT, z0 + 0.03, z1 - 0.03); leafLow.material = [M.white, M.white, M.cut, M.white, M.white, M.white];
    io(B('right_door_leaf_upper', g, M.white, W - 0.04, W, CUT, h - 0.03, z0 + 0.03, z1 - 0.03));
    B('right_door_panel_low', g, M.white, W - 0.05, W - 0.04, 0.15, 0.95, z0 + 0.12, z1 - 0.12, 0.005);
    io(B('right_door_panel_high', g, M.white, W - 0.05, W - 0.04, 1.2, 1.95, z0 + 0.12, z1 - 0.12, 0.005));
    B('right_door_knob', g, M.brass, W - 0.1, W - 0.04, 0.98, 1.02, z0 + 0.1, z0 + 0.14, 0.01);
    B('right_door_casing_a', g, M.trim, W - 0.02, W, 0, CUT, z0 - 0.08, z0 + 0.03).material = [M.trim, M.trim, M.cut, M.trim, M.trim, M.trim];
    B('right_door_casing_b', g, M.trim, W - 0.02, W, 0, CUT, z1 - 0.03, z1 + 0.08).material = [M.trim, M.trim, M.cut, M.trim, M.trim, M.trim];
    io(B('right_door_casing_upper', g, M.trim, W - 0.02, W, CUT, h + 0.1, z0 - 0.08, z1 + 0.08)); }

  // ---------------- river-stone fireplace (floor to ceiling), raised hearth, beam mantel, brass screen
  const FZ0 = 2.65, FZ1 = 4.35, FC = (FZ0 + FZ1) / 2, FDp = 0.45, OZ0 = FC - 0.42, OZ1 = FC + 0.42, OY0 = 0.42, OY1 = 1.12;
  { const sm = M.stone(0.8, 1.15), topCut = [sm, sm, M.cut, sm, sm, sm];
    B('fireplace_stone_left', fire, sm, 0, FDp, 0, H, FZ0, OZ0).material = topCut;
    B('fireplace_stone_right', fire, sm, 0, FDp, 0, H, OZ1, FZ1).material = topCut;
    B('fireplace_stone_over', fire, sm, 0, FDp, OY1, H, OZ0, OZ1).material = topCut;
    B('fireplace_stone_under', fire, sm, 0, FDp, 0, OY0, OZ0, OZ1);
    const hs = M.stone(1.6, 0.4);
    B('fireplace_hearth', fire, hs, FDp - 0.02, FDp + 0.48, 0, OY0 - 0.04, FZ0 - 0.2, FZ1 + 0.2, 0.02);
    B('fireplace_hearth_cap', fire, std('#8d847a', 0.8), FDp - 0.02, FDp + 0.52, OY0 - 0.04, OY0, FZ0 - 0.22, FZ1 + 0.22, 0.012);
    B('fireplace_firebox_back', fire, M.soot, 0.0, 0.06, OY0, OY1, OZ0, OZ1);
    B('fireplace_firebox_side_a', fire, M.soot, 0.0, FDp - 0.02, OY0, OY1, OZ0, OZ0 + 0.03); B('fireplace_firebox_side_b', fire, M.soot, 0.0, FDp - 0.02, OY0, OY1, OZ1 - 0.03, OZ1);
    B('fireplace_firebox_top', fire, M.soot, 0.0, FDp - 0.02, OY1 - 0.03, OY1, OZ0, OZ1); B('fireplace_firebox_floor', fire, M.soot, 0.0, FDp, OY0, OY0 + 0.01, OZ0, OZ1);
    B('fireplace_mantel_beam', fire, M.darkWood, FDp - 0.02, FDp + 0.24, 1.42, 1.62, FZ0 - 0.12, FZ1 + 0.12, 0.012);
    // logs + grate
    B('fireplace_grate', fire, M.blackMatte, 0.12, 0.36, OY0 + 0.01, OY0 + 0.05, OZ0 + 0.12, OZ1 - 0.12);
    [[0.2, 0.09, 0.06], [0.3, 0.09, -0.04], [0.25, 0.17, 0.01]].forEach(([x, y, r], i) => { const l = cyl(`fireplace_log_${i + 1}`, fire, std('#4a2c19', 0.95), 0.05, 0.05, 0.62, x, OY0 + y, FC + r, 10); l.rotation.x = Math.PI / 2; l.rotation.z = 0.08 * (i - 1); });
    // brass folding screen in front of the opening
    const sg = G('fireplace_brass_screen', fire), sx = FDp + 0.08, mesh_ = new THREE.MeshStandardMaterial({ color: '#1b1612', roughness: 0.7, transparent: true, opacity: 0.45, side: THREE.DoubleSide, depthWrite: false });
    const panels = [[OZ0 - 0.12, OZ0 + 0.22, 0.12], [OZ0 + 0.22, OZ1 - 0.22, 0], [OZ1 - 0.22, OZ1 + 0.12, 0.12]];
    panels.forEach(([a, b, dx], i) => { const x = sx - dx * 0.0; const off = i === 1 ? 0.06 : 0;
      B(`fireplace_screen_frame_top_${i + 1}`, sg, M.brass, x + off, x + off + 0.02, OY0 + 0.64, OY0 + 0.67, a, b); B(`fireplace_screen_frame_bottom_${i + 1}`, sg, M.brass, x + off, x + off + 0.02, OY0, OY0 + 0.03, a, b);
      B(`fireplace_screen_frame_a_${i + 1}`, sg, M.brass, x + off, x + off + 0.02, OY0, OY0 + 0.67, a, a + 0.02); B(`fireplace_screen_frame_b_${i + 1}`, sg, M.brass, x + off, x + off + 0.02, OY0, OY0 + 0.67, b - 0.02, b);
      const m = mesh(`fireplace_screen_mesh_${i + 1}`, new THREE.PlaneGeometry(b - a - 0.04, 0.6), mesh_, sg, x + off + 0.01, OY0 + 0.335, (a + b) / 2, false); m.rotation.y = Math.PI / 2; });
    cyl('fireplace_screen_finial_a', sg, M.brass, 0.015, 0.015, 0.05, sx + 0.07, OY0 + 0.7, FC - 0.15, 8); cyl('fireplace_screen_finial_b', sg, M.brass, 0.015, 0.015, 0.05, sx + 0.07, OY0 + 0.7, FC + 0.15, 8);
  }

  // ---------------- candy wall: three stacked black floating shelves (backlit) with glass candy jars, bar sideboard under
  const CZ0 = 6.4, CZ1 = 7.6;   // 1.2 m
  { const g = G('candy_wall', furn);
    B('sideboard_body', g, M.walnut, 0.0, 0.48, 0.12, 0.9, CZ0 - 0.05, CZ1 + 0.05, 0.01);
    B('sideboard_top', g, M.blackMatte, 0.0, 0.5, 0.9, 0.94, CZ0 - 0.07, CZ1 + 0.07, 0.006);
    B('sideboard_plinth', g, M.blackMatte, 0.03, 0.44, 0.0, 0.12, CZ0, CZ1);
    for (let i = 0; i < 3; i++) { const a = CZ0 - 0.04 + i * (CZ1 - CZ0 + 0.08) / 3, b = a + (CZ1 - CZ0 + 0.08) / 3 - 0.01;
      B(`sideboard_door_${i + 1}`, g, M.walnut, 0.48, 0.495, 0.16, 0.86, a + 0.005, b - 0.005, 0.004);
      B(`sideboard_pull_${i + 1}`, g, M.brass, 0.495, 0.51, 0.7, 0.8, (a + b) / 2 - 0.008, (a + b) / 2 + 0.008, 0.003); }
    // bar items on top
    for (const [k, z, c, hh] of [[1, CZ0 + 0.15, '#3d5a2a', 0.3], [2, CZ0 + 0.28, '#6b3a1a', 0.26], [3, CZ0 + 0.4, '#e7e2d6', 0.28]]) cyl(`sideboard_bottle_${k}`, g, std(c, 0.15, 0.1, { transparent: true, opacity: 0.85 }), 0.035, 0.04, hh, 0.22, 0.94 + hh / 2, z, 12);
    B('sideboard_tray', g, M.brass, 0.12, 0.38, 0.94, 0.955, CZ1 - 0.45, CZ1 - 0.1, 0.004);
    const jarGlass = new THREE.MeshPhysicalMaterial({ color: '#ffffff', roughness: 0.05, transparent: true, opacity: 0.25, depthWrite: false });
    const candy = ['#e8344a', '#f5b82e', '#5bc56a', '#3f8fe0', '#ff7fbf', '#9b5de5', '#ff8c2e', '#f2f2f2'].map(c => std(c, 0.35));
    const r = rng(57);
    [1.3, 1.68, 2.06].forEach((sy, si) => {
      B(`candy_shelf_${si + 1}`, g, M.blackMatte, 0.0, 0.26, sy - 0.04, sy, CZ0, CZ1, 0.006);
      for (let j = 0; j < 6; j++) { const z = CZ0 + 0.1 + j * 0.2, h = 0.17 + r() * 0.06, cm = candy[Math.floor(r() * candy.length)];
        cyl(`candy_jar_${si + 1}_${j + 1}`, g, jarGlass, 0.065, 0.065, h, 0.13, sy + h / 2, z, 16).castShadow = false;
        cyl(`candy_fill_${si + 1}_${j + 1}`, g, cm, 0.058, 0.058, h * 0.72, 0.13, sy + h * 0.36 + 0.004, z, 14);
        cyl(`candy_lid_${si + 1}_${j + 1}`, g, M.steel, 0.05, 0.055, 0.025, 0.13, sy + h + 0.012, z, 14); }
    });
  }

  // ---------------- popcorn machine cart in the corner after the sideboard
  const PX = 0.36, PZ = 8.12;
  const popKettleMat = std('#c9ccd0', 0.3, 0.8, { emissive: new THREE.Color('#ffb24a'), emissiveIntensity: 0 });
  const popInnerMat = std('#fff3d6', 0.6, 0, { emissive: new THREE.Color('#ffcf7a'), emissiveIntensity: 0 });
  const popCornMat = std('#f7e3a6', 0.8, 0, { emissive: new THREE.Color('#ffcf7a'), emissiveIntensity: 0 });
  { const g = G('popcorn_machine_cart', furn); g.position.set(PX, 0, PZ); g.rotation.y = -Math.PI / 4;   // faces into the room, diagonally out of the corner
    for (const [x, z] of [[-0.24, -0.18], [0.24, -0.18], [-0.24, 0.18], [0.24, 0.18]]) { const w = cyl('popcorn_cart_wheel', g, M.blackMatte, 0.07, 0.07, 0.04, x, 0.07, z, 14); w.rotation.z = Math.PI / 2; }
    B('popcorn_cart_base', g, M.red, -0.28, 0.28, 0.12, 0.2, -0.22, 0.22, 0.01);
    B('popcorn_cart_cabinet', g, M.red, -0.26, 0.26, 0.2, 0.82, -0.2, 0.2, 0.012);
    B('popcorn_cart_cabinet_door', g, std('#8f0d15', 0.45), -0.22, 0.22, 0.25, 0.78, 0.2, 0.21, 0.006);
    B('popcorn_cart_pull', g, M.steel, -0.08, 0.08, 0.7, 0.72, 0.21, 0.23, 0.004);
    const hb = new THREE.TorusGeometry(0.12, 0.012, 6, 16, Math.PI); const handle = mesh('popcorn_cart_handle', hb, M.steel, g, 0.3, 0.75, 0); handle.rotation.y = Math.PI / 2; handle.rotation.z = -Math.PI / 2;
    // glass popper cabinet on top
    B('popcorn_case_floor', g, M.red, -0.25, 0.25, 0.82, 0.86, -0.19, 0.19, 0.006);
    for (const [x, z] of [[-0.24, -0.18], [0.24, -0.18], [-0.24, 0.18], [0.24, 0.18]]) B('popcorn_case_post', g, M.red, x - 0.015, x + 0.015, 0.86, 1.42, z - 0.015, z + 0.015);
    B('popcorn_case_roof', g, M.red, -0.27, 0.27, 1.42, 1.48, -0.21, 0.21, 0.01);
    B('popcorn_case_crown', g, M.red, -0.2, 0.2, 1.48, 1.62, -0.03, 0.03, 0.006);
    mesh('popcorn_sign', new THREE.PlaneGeometry(0.38, 0.1), std('#ffffff', 0.6, 0, { map: T.popSign }), g, 0, 1.55, 0.032, false);
    B('popcorn_case_glass_front', g, M.cabGlass, -0.235, 0.235, 0.86, 1.42, 0.17, 0.175).castShadow = false;
    B('popcorn_case_glass_side_a', g, M.cabGlass, -0.235, -0.23, 0.86, 1.42, -0.17, 0.17).castShadow = false;
    B('popcorn_case_glass_side_b', g, M.cabGlass, 0.23, 0.235, 0.86, 1.42, -0.17, 0.17).castShadow = false;
    B('popcorn_case_light_panel', g, popInnerMat, -0.2, 0.2, 1.405, 1.415, -0.15, 0.15);
    cyl('popcorn_kettle_rod', g, M.steel, 0.008, 0.008, 0.16, 0, 1.33, 0, 8);
    cyl('popcorn_kettle', g, popKettleMat, 0.11, 0.085, 0.12, 0, 1.2, 0, 20);
    cyl('popcorn_kettle_lid', g, M.steel, 0.112, 0.112, 0.012, 0, 1.265, 0, 20);
    // popcorn heap on the case floor
    const r = rng(77); for (let i = 0; i < 46; i++) { const a = r() * 6.28, d = Math.sqrt(r()) * 0.17; sphere(`popcorn_kernel_${i}`, g, popCornMat, 0.022 + r() * 0.01, Math.cos(a) * d, 0.88 + r() * 0.06 * (1 - d / 0.17) + 0.02, Math.sin(a) * d * 0.8).castShadow = false; }
  }

  // ---------------- mid-century dresser by the right wall
  { const g = G('mid_century_dresser', furn), x0 = W - 0.48, x1 = W - 0.02, z0 = 2.35, z1 = 3.75;
    B('dresser_body', g, M.walnut, x0, x1, 0.22, 0.86, z0, z1, 0.012);
    for (let c = 0; c < 2; c++) for (let rr = 0; rr < 3; rr++) { const a = z0 + 0.03 + c * 0.68, y = 0.26 + rr * 0.2;
      B(`dresser_drawer_${c + 1}_${rr + 1}`, g, M.walnut, x0 - 0.012, x0, y, y + 0.18, a, a + 0.64, 0.004);
      B(`dresser_pull_${c + 1}_${rr + 1}`, g, M.brass, x0 - 0.02, x0 - 0.01, y + 0.13, y + 0.145, a + 0.25, a + 0.39, 0.003); }
    for (const [x, z] of [[x0 + 0.05, z0 + 0.06], [x1 - 0.05, z0 + 0.06], [x0 + 0.05, z1 - 0.06], [x1 - 0.05, z1 - 0.06]]) { const l = cyl('dresser_leg', g, M.darkWood, 0.02, 0.012, 0.22, x, 0.11, z, 8); l.rotation.z = x < (x0 + x1) / 2 ? 0.08 : -0.08; }
    cyl('dresser_lamp_base', g, M.brass, 0.06, 0.08, 0.04, x0 + 0.24, 0.88, z0 + 0.3, 16); }

  // ---------------- seating: two rows of brown leather recliners FACING THE SCREEN, right ends against the right wall; back row on a riser
  const sofaW = 3 * 0.72 + 0.4, SX = W - 0.02 - sofaW / 2;
  const RX0 = SX - sofaW / 2 - 0.62, RZ0 = 6.45, RZ1 = L, RHt = 0.36;
  B('riser', arch, M.carpet(2.6, 1.6), RX0, W, 0, RHt, RZ0, RZ1, 0.006);
  B('riser_nosing', arch, std('#8a7458', 0.8), RX0, W, RHt - 0.03, RHt, RZ0 - 0.012, RZ0 + 0.03, 0.006);
  B('riser_side_trim', arch, std('#8a7458', 0.8), RX0 - 0.012, RX0 + 0.03, RHt - 0.03, RHt, RZ0, RZ1, 0.006);
  const sofa = (name, cx, cz, y0, seats) => {
    const g = G(name, furn); g.position.set(cx, y0, cz); const sw = 0.72, aw = 0.2, w = seats * sw + 2 * aw, hx = w / 2;   // seat front at -z (faces the screen)
    B(`${name}_base`, g, M.leatherDark, -hx, hx, 0.04, 0.4, -0.42, 0.46, 0.04);
    B(`${name}_arm_l`, g, M.leather, -hx, -hx + aw, 0.04, 0.68, -0.44, 0.48, 0.07); B(`${name}_arm_r`, g, M.leather, hx - aw, hx, 0.04, 0.68, -0.44, 0.48, 0.07);
    for (let i = 0; i < seats; i++) { const a = -hx + aw + i * sw;
      B(`${name}_seat_${i + 1}`, g, M.leather, a + 0.01, a + sw - 0.01, 0.38, 0.52, -0.42, 0.3, 0.06);
      B(`${name}_back_${i + 1}`, g, M.leather, a + 0.02, a + sw - 0.02, 0.46, 1.02, 0.22, 0.48, 0.09);
      B(`${name}_back_pillow_${i + 1}`, g, M.leather, a + 0.06, a + sw - 0.06, 0.6, 0.92, 0.14, 0.24, 0.05);
      B(`${name}_footrest_seam_${i + 1}`, g, M.leatherDark, a + 0.04, a + sw - 0.04, 0.1, 0.36, -0.445, -0.42, 0.01); }
    for (const x of [-hx + 0.05, hx - 0.05]) for (const z of [-0.36, 0.4]) cyl(`${name}_foot`, g, M.blackMatte, 0.025, 0.025, 0.04, x, 0.02, z, 8);
    return g;
  };
  const FZ = 5.15, BZ = 7.4;
  sofa('sofa_front_row', SX, FZ, 0, 3);       // floor
  sofa('sofa_back_row', SX, BZ, RHt, 3);      // riser, directly behind
  // a small side table at the left end of each sofa (each carries a table lamp; see table_lamps device)
  const stX = SX - sofaW / 2 - 0.3;
  const sideTables = [{ name: 'side_table_front_row', x: stX, y: 0, z: FZ }, { name: 'side_table_back_row', x: stX, y: RHt, z: BZ }];
  sideTables.forEach(({ name, x, y, z }) => { const g = G(name, furn);
    B(`${name}_top`, g, M.walnut, x - 0.22, x + 0.22, y + 0.5, y + 0.54, z - 0.22, z + 0.22, 0.01);
    B(`${name}_shelf`, g, M.darkWood, x - 0.19, x + 0.19, y + 0.14, y + 0.17, z - 0.19, z + 0.19, 0.006);
    for (const [k, dx, dz] of [[1, -1, -1], [2, 1, -1], [3, -1, 1], [4, 1, 1]]) B(`${name}_leg_${k}`, g, M.darkWood, x + dx * 0.19 - 0.02, x + dx * 0.19 + 0.02, y, y + 0.5, z + dz * 0.19 - 0.02, z + dz * 0.19 + 0.02); });
  // coffee table / ottoman, round side table, rug (in front of the front row)
  B('area_rug', arch, std('#ffffff', 1, 0, { map: T.rug }), 1.25, 4.95, 0, 0.012, 1.7, 4.55).castShadow = false;
  { const g = G('coffee_ottoman', furn); B('coffee_ottoman_top', g, M.walnut, 2.6, 4.0, 0.36, 0.44, 3.3, 4.1, 0.02); B('coffee_ottoman_body', g, M.darkWood, 2.65, 3.95, 0.06, 0.36, 3.35, 4.05, 0.02);
    B('coffee_ottoman_plinth', g, M.blackMatte, 2.71, 3.89, 0, 0.06, 3.41, 3.99); cyl('coffee_ottoman_bowl', g, std('#d9d2c3', 0.5), 0.13, 0.08, 0.07, 3.3, 0.475, 3.7, 20); }
  { const g = G('round_side_table', furn), x = 1.15, z = 5.2; cyl('round_side_table_top', g, M.walnut, 0.26, 0.26, 0.04, x, 0.56, z, 32); cyl('round_side_table_pedestal', g, M.darkWood, 0.05, 0.07, 0.52, x, 0.28, z, 14); cyl('round_side_table_base', g, M.darkWood, 0.18, 0.2, 0.03, x, 0.015, z, 24); }

  // ---------------- reeded glass panels in brass frames, flat against the rear black curtain: 5 on the popcorn/candy side to the x=0 end, 2 behind the sofas to the right wall, gap between
  { const g = G('reeded_glass_partition', furn), z = L + 0.045, pw = 0.5, gap = 0.035, y0 = 0.42, y1 = 2.3;
    const reeded = new THREE.MeshPhysicalMaterial({ color: '#efe3c4', map: T.ribs, roughness: 0.25, transparent: true, opacity: 0.62, side: THREE.DoubleSide, depthWrite: false });
    // Dylan: 5 panels behind the popcorn machine running all the way to the fireplace-wall end (x=0),
    // 2 panels behind the sofas running all the way to the right wall (x=W); open gap between the groups in the middle
    const groups = [{ k: 'popcorn_side_5', x0: 0.02, n: 5 }, { k: 'sofa_side_2', x0: W - 0.02 - 2 * pw - gap, n: 2 }];
    let idx = 0;
    groups.forEach(({ k, x0, n }) => { const x1 = x0 + n * pw + (n - 1) * gap;
      io(B(`reeded_rail_${k}`, g, M.brass, x0 - 0.03, x1 + 0.03, y1 + 0.05, y1 + 0.075, z - 0.012, z + 0.012));
      for (let i = 0; i < n; i++) { idx++; const a = x0 + i * (pw + gap), b = a + pw, nm = `reeded_panel_${idx}`;
        // full-height panel for interior views, and a version cut at the wall height for the dollhouse (style-guide camera-side rule)
        for (const [suffix, top, flag] of [['', y1, 'interiorOnly'], ['_cut', 1.1, 'dollhouseOnly']]) {
          const parts = [B(`${nm}${suffix}_frame_bottom`, g, M.brass, a, b, y0, y0 + 0.025, z - 0.012, z + 0.012),
            B(`${nm}${suffix}_frame_a`, g, M.brass, a, a + 0.025, y0, top, z - 0.012, z + 0.012), B(`${nm}${suffix}_frame_b`, g, M.brass, b - 0.025, b, y0, top, z - 0.012, z + 0.012),
            B(`${nm}${suffix}_frame_top`, g, M.brass, a, b, top - 0.025, top, z - 0.012, z + 0.012)];
          const gl = mesh(`${nm}${suffix}_glass`, new THREE.PlaneGeometry(pw - 0.05, top - y0 - 0.05), reeded, g, (a + b) / 2, (y0 + top) / 2, z, false); gl.renderOrder = 3; parts.push(gl);
          if (!suffix) for (const xx of [a + 0.06, b - 0.06]) parts.push(cyl(`${nm}_hanger`, g, M.brass, 0.004, 0.004, 0.05, xx, y1 + 0.025, z, 6));
          parts.forEach(m => { m.userData[flag] = true; }); } } });
  }

  // ---------------- projector (ceiling-mounted)
  const PJ = { x: SC, y: H - 0.32, z: 4.4 };
  const lensMat = std('#1a2230', 0.1, 0.3, { emissive: new THREE.Color('#dfe9ff'), emissiveIntensity: 0 });
  { const g = G('projector_body', furn);
    B('projector_case', g, std('#f2f1ee', 0.35), PJ.x - 0.22, PJ.x + 0.22, PJ.y - 0.08, PJ.y + 0.08, PJ.z - 0.2, PJ.z + 0.22, 0.03);
    B('projector_mount_plate', g, std('#d8d6d2', 0.4), PJ.x - 0.08, PJ.x + 0.08, PJ.y + 0.08, PJ.y + 0.1, PJ.z - 0.08, PJ.z + 0.08);
    cyl('projector_mount_pole', g, std('#d8d6d2', 0.4), 0.02, 0.02, H - PJ.y - 0.1, PJ.x, (H + PJ.y + 0.1) / 2, PJ.z, 10);
    cyl('projector_ceiling_plate', g, std('#d8d6d2', 0.4), 0.07, 0.07, 0.015, PJ.x, H - 0.008, PJ.z, 16);
    const lr = cyl('projector_lens_ring', g, M.blackMatte, 0.055, 0.055, 0.04, PJ.x + 0.08, PJ.y, PJ.z - 0.21, 20); lr.rotation.x = Math.PI / 2;
    const lens = cyl('projector_lens', g, lensMat, 0.042, 0.042, 0.01, PJ.x + 0.08, PJ.y, PJ.z - 0.235, 20); lens.rotation.x = Math.PI / 2; }

  // ---------------- devices
  const room = 'movie_room';
  // projector: beam pyramid + movie on the screen
  { const g = G('projector', devGroup), ax = PJ.x + 0.08, ay = PJ.y, az = PJ.z - 0.24;
    const c = [[SC - SW / 2, SY0], [SC + SW / 2, SY0], [SC + SW / 2, SY0 + SH], [SC - SW / 2, SY0 + SH]];
    const pos = [], col = [];
    for (let i = 0; i < 4; i++) { const [x1, y1] = c[i], [x2, y2] = c[(i + 1) % 4]; pos.push(ax, ay, az, x1, y1, 0.11, x2, y2, 0.11); col.push(1, 1, 1, 0.25, 0.25, 0.3, 0.25, 0.25, 0.3); }
    const bg = new THREE.BufferGeometry(); bg.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3)); bg.setAttribute('color', new THREE.Float32BufferAttribute(col, 3));
    const beam = mesh('projector_beam', bg, new THREE.MeshBasicMaterial({ color: '#cfdcff', vertexColors: true, transparent: true, opacity: 0.22, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide, toneMapped: false }), g, 0, 0, 0, false);
    beam.receiveShadow = false; beam.visible = false; beam.renderOrder = 6;
    const decals = [decal('glow_projector_screen_spill', g, T.glow, '#f0a060', 0.35, 4.4, 2.4, SC, 1.4, 0.006, '+z'), decal('glow_projector_floor_spill', g, T.glow, '#f0a060', 0.2, 3.2, 3.2, SC, 0.02, 1.2, 'up')];
    const l = new THREE.SpotLight('#ffd2a0', 0, 9, 0.38, 0.5, 1.5); l.name = 'light_projector_screen'; l.position.set(SC, 1.5, 3.5); l.target.position.set(SC, 1.4, 0); g.add(l); g.add(l.target);
    register('projector', { room, label: 'Projector', type: 'media', group: g, emissive: [{ mat: lensMat, on: 6 }], lights: [{ light: l, on: 2.5 }], decals, show: [beam],
      custom: (on) => { screenMat.map = on ? T.movie : null; screenMat.emissiveMap = on ? T.movie : null; screenMat.color.set(on ? '#ffffff' : '#e8e8e4'); screenMat.emissiveIntensity = on ? 1.15 : 0; screenMat.needsUpdate = true; },
      anchor: new THREE.Vector3(PJ.x, PJ.y - 0.12, PJ.z) });
  }
  // screen bias light: LED strip on the back of the frame washing the red wall
  { const g = G('screen_bias_light', devGroup), m = lightMats(), ledMat = std('#ddd', 0.4, 0, { emissive: new THREE.Color('#9fb8ff'), emissiveIntensity: 0 });
    const x0 = SC - SW / 2 - 0.04, x1 = SC + SW / 2 + 0.04, y0 = SY0 - 0.04, y1 = SY0 + SH + 0.04;
    for (const [n, a, b, c, d] of [['top', x0, x1, y1 - 0.01, y1], ['bottom', x0, x1, y0, y0 + 0.01], ['left', x0, x0 + 0.01, y0, y1], ['right', x1 - 0.01, x1, y0, y1]]) B(`light_bias_strip_${n}`, g, ledMat, a, b, c, d, 0.028, 0.034).castShadow = false;
    const halo = canvasTex(256, 160, (c2, w, h) => { const pad = 34; c2.filter = 'blur(18px)'; c2.fillStyle = 'white'; c2.fillRect(pad, pad, w - 2 * pad, h - 2 * pad); });
    const decals = [decal('glow_bias_halo', g, halo, '#8aa8ff', 0.9, SW + 1.2, SH + 0.9, SC, SY0 + SH / 2, 0.008, '+z')];
    const l = pl('light_bias', g, '#8aa8ff', SC, SY0 + SH / 2, 0.25, 3.5);
    register('screen_bias_light', { room, label: 'Screen bias light', type: 'light', group: g, emissive: [{ mat: ledMat, on: 5 }], lights: [{ light: l, on: 1.4 }], decals, anchor: new THREE.Vector3(SC, SY0 + SH + 0.12, 0.12) });
  }
  // recessed lights (floating cans: the ceiling is omitted)
  { const g = G('recessed_lights', devGroup), m = lightMats(), decals = [], lights = [], show = [];
    const coneTex = canvasTex(8, 256, (c2, w, h) => { const gr = c2.createLinearGradient(0, 0, 0, h); gr.addColorStop(0, 'rgba(255,255,255,0.9)'); gr.addColorStop(1, 'rgba(255,255,255,0)'); c2.fillStyle = gr; c2.fillRect(0, 0, w, h); });
    const coneMat = new THREE.MeshBasicMaterial({ map: coneTex, color: STYLE.warm, transparent: true, opacity: 0.15, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide, toneMapped: false });
    const trim = std('#cfcac2', 0.6);
    [[1.4, 1.4], [4.0, 1.4], [1.4, 3.4], [4.0, 3.4], [1.4, 5.4], [4.0, 5.4], [2.2, 7.5], [4.2, 7.5]].forEach(([x, z], i) => {
      cyl(`recessed_light_${i + 1}_trim`, g, trim, 0.075, 0.075, 0.012, x, H - 0.006, z, 20).castShadow = false;
      cyl(`recessed_light_${i + 1}`, g, m.bulb, 0.05, 0.05, 0.004, x, H - 0.013, z, 20).castShadow = false;
      const fy = z > RZ0 && z < RZ1 && x > RX0 ? RHt : 0.012;
      const cone = mesh(`light_cone_recessed_${i + 1}`, new THREE.ConeGeometry(0.48, H - 0.06 - fy, 24, 1, true), coneMat, g, x, (H - 0.06 + fy) / 2, z, false);
      cone.visible = false; cone.renderOrder = 6; show.push(cone);
      decals.push(decal(`glow_recessed_${i + 1}_pool`, g, T.glow, STYLE.warm, 0.45, 1.3, 1.3, x, fy + 0.004, z, 'up'));
    });
    for (const [x, z] of [[2.7, 2.4], [2.7, 6.6]]) { const s = new THREE.SpotLight(STYLE.warm, 0, 7, 1.1, 0.7, 2); s.name = `light_recessed_${z}`; s.position.set(x, H - 0.1, z); s.target.position.set(x, 0, z); g.add(s); g.add(s.target); lights.push(s); }
    register('recessed_lights', { room, label: 'Recessed lights', type: 'light', group: g, emissive: [{ mat: m.bulb, on: 6 }], lights: lights.map(l => ({ light: l, on: 7 })), decals, show, anchor: new THREE.Vector3(2.7, H - 0.08, 3.4) });
  }
  // fireplace: flames + hearth glow
  { const g = G('fireplace_flames', devGroup), show = [];
    const flameA = std('#ff9a3a', 0.8, 0, { emissive: new THREE.Color('#ff7a1a'), emissiveIntensity: 5, transparent: true, opacity: 0.95 });
    const flameB = std('#ffd36b', 0.8, 0, { emissive: new THREE.Color('#ffcc55'), emissiveIntensity: 7 });
    [[-0.26, 0.26, flameA], [-0.1, 0.34, flameA], [0.08, 0.3, flameA], [0.24, 0.24, flameA], [-0.18, 0.18, flameB], [0.0, 0.22, flameB], [0.15, 0.17, flameB]].forEach(([dz, h, mat], i) => {
      const f = mesh(`fire_flame_${i + 1}`, new THREE.ConeGeometry(mat === flameB ? 0.045 : 0.07, h, 6), mat, g, 0.24 + (mat === flameB ? 0.04 : 0), OY0 + 0.14 + h / 2, FC + dz, false);
      f.visible = false; show.push(f); });
    const emberMat = std('#3a1a0c', 0.9, 0, { emissive: new THREE.Color('#ff5a14'), emissiveIntensity: 0 });
    B('fireplace_embers', g, emberMat, 0.1, 0.38, OY0 + 0.01, OY0 + 0.03, OZ0 + 0.1, OZ1 - 0.1);
    const decals = [decal('glow_fireplace_hearth', g, T.glow, STYLE.fire, 0.8, 1.3, 2.2, FDp + 0.24, OY0 + 0.003, FC, 'up'),
      decal('glow_fireplace_floor', g, T.glow, STYLE.fire, 0.5, 3.0, 3.4, FDp + 1.2, 0.02, FC, 'up'),
      decal('glow_fireplace_opening', g, T.glow, STYLE.fire, 0.9, 1.4, 1.1, FDp + 0.005, (OY0 + OY1) / 2, FC, '-x')];
    const l = pl('light_fireplace', g, STYLE.fire, 0.9, 0.75, FC, 5);
    register('fireplace', { room, label: 'Fireplace', type: 'fireplace', group: g, emissive: [{ mat: emberMat, on: 3 }], lights: [{ light: l, on: 3.5 }], decals, show, anchor: new THREE.Vector3(FDp + 0.1, OY1 + 0.1, FC) });
  }
  // rear string lights on a ceiling track along the back curtain
  { const g = G('rear_string_lights', devGroup), m = lightMats(), decals = [], lights = [];
    io(B('rear_track', g, M.blackMatte, 0.3, W - 0.2, H - 0.05, H - 0.02, L - 0.24, L - 0.2));
    const n = 22; for (let i = 0; i < n; i++) { const x = 0.4 + (W - 0.7) * i / (n - 1), sag = 0.07 * Math.sin(Math.PI * ((i % 6) / 5));
      sphere(`light_string_bulb_${i + 1}`, g, m.bulb, 0.022, x, H - 0.12 - sag, L - 0.22).castShadow = false; }
    const wire = new THREE.CatmullRomCurve3(Array.from({ length: 23 }, (_, i) => new THREE.Vector3(0.4 + (W - 0.7) * i / 22, H - 0.1 - 0.07 * Math.sin(Math.PI * ((i % 6) / 5)), L - 0.22)));
    mesh('rear_string_wire', new THREE.TubeGeometry(wire, 120, 0.004, 4), M.blackMatte, g, 0, 0, 0, false);
    decals.push(decal('glow_string_floor', g, T.glow, STYLE.warm, 0.35, W, 1.6, W / 2 + 0.3, 0.014, L - 0.6, 'up'));
    for (const x of [1.4, 3.0, 4.6]) lights.push(pl(`light_string_${x}`, g, STYLE.warm, x, H - 0.3, L - 0.45, 3));
    register('rear_string_lights', { room, label: 'Rear string lights', type: 'light', group: g, emissive: [{ mat: m.bulb, on: 6 }], lights: lights.map(l => ({ light: l, on: 0.9 })), decals, anchor: new THREE.Vector3(W / 2, H - 0.15, L - 0.22) });
  }
  // riser step lights
  { const g = G('riser_step_lights', devGroup), m = lightMats(), decals = [];
    for (let i = 0; i < 6; i++) { const x = RX0 + 0.3 + i * (W - RX0 - 0.6) / 5; B(`light_riser_step_${i + 1}`, g, m.bulb, x - 0.03, x + 0.03, 0.15, 0.17, RZ0 - 0.018, RZ0 - 0.012).castShadow = false;
      decals.push(decal(`glow_riser_step_${i + 1}`, g, T.glow, STYLE.warm, 0.6, 0.55, 0.55, x, 0.016, RZ0 - 0.2, 'up')); }
    register('riser_step_lights', { room, label: 'Riser step lights', type: 'light', group: g, emissive: [{ mat: m.bulb, on: 6 }], decals, anchor: new THREE.Vector3((RX0 + W) / 2, 0.16, RZ0 - 0.02) });
  }
  // table lamps on the two sofa side tables (dimmed for movie night)
  { const g = G('table_lamps', devGroup), m = lightMats(), decals = [], lights = [];
    sideTables.forEach(({ name, x, y, z }, i) => { const n = `table_lamp_${i + 1}`, ty = y + 0.54;
      cyl(`${n}_base`, g, std('#2b2b2b', 0.35, 0.3), 0.07, 0.09, 0.2, x, ty + 0.1, z, 20);
      cyl(`${n}_stem`, g, M.brass, 0.008, 0.008, 0.16, x, ty + 0.28, z, 8);
      cyl(`${n}_shade`, g, m.shade, 0.12, 0.15, 0.2, x, ty + 0.44, z, 28, true);
      sphere(`${n}_bulb`, g, m.bulb, 0.03, x, ty + 0.38, z);
      decals.push(decal(`glow_${n}_table`, g, T.glow, STYLE.warm, 0.8, 0.9, 0.9, x, ty + 0.004, z, 'up'), decal(`glow_${n}_floor`, g, T.glow, STYLE.warm, 0.45, 1.6, 1.6, x, y + 0.006, z, 'up'));
      lights.push(pl(`light_${n}`, g, STYLE.warm, x, ty + 0.38, z, 3.2)); });
    register('table_lamps', { room, label: 'Table lamps', type: 'light', group: g, emissive: [{ mat: m.shade, on: 1.6 }, { mat: m.bulb, on: 5 }], lights: lights.map(l => ({ light: l, on: 1.5 })), decals, anchor: new THREE.Vector3(sideTables[0].x, sideTables[0].y + 1.1, sideTables[0].z) });
  }
  // decorative RGB LED flower-light plant in the RIGHT-hand screen corner (screen wall / right wall)
  { const g = G('flower_light', devGroup), cx = W - 0.36, cz = 0.3, r = rng(91), decals = [], show = [];
    const core = std('#fff3d0', 0.4, 0, { emissive: new THREE.Color('#ffe9b8'), emissiveIntensity: 0 });
    cyl('flower_light_pot', g, std('#2f2c2a', 0.45), 0.17, 0.13, 0.36, cx, 0.18, cz, 24);
    cyl('flower_light_soil', g, std('#2a1f18', 0.95), 0.155, 0.155, 0.01, cx, 0.355, cz, 20);
    const stems = [[0.0, 0.0, 1.85], [-0.12, 0.06, 1.55], [0.06, 0.12, 1.7], [-0.06, -0.08, 1.35], [-0.12, 0.1, 1.2]];
    const rgb = ['#ff4fa8', '#4f8cff', '#b45cff', '#3fe0a0', '#ffb347'].map(c => std(c, 0.5, 0, { emissive: new THREE.Color(c), emissiveIntensity: 0 }));
    stems.forEach(([dx, dz, top], i) => { const n = `flower_light_stem_${i + 1}`;
      const curve = new THREE.CatmullRomCurve3([new THREE.Vector3(cx + dx * 0.2, 0.36, cz + dz * 0.2), new THREE.Vector3(cx + dx * 0.7, (0.36 + top) / 2, cz + dz * 0.7), new THREE.Vector3(cx + dx * 1.6, top, cz + dz * 1.6)]);
      mesh(n, new THREE.TubeGeometry(curve, 20, 0.006, 5), std('#3b4a2c', 0.6), g, 0, 0, 0, false);
      const bx = cx + dx * 1.6, bz = cz + dz * 1.6;
      for (let k = 0; k < 6; k++) { const a = k / 6 * Math.PI * 2; const p = mesh(`flower_light_petal_${i + 1}_${k + 1}`, new THREE.SphereGeometry(0.045, 10, 6), rgb[i], g, bx + Math.cos(a) * 0.04, top + 0.01, bz + Math.sin(a) * 0.04, false); p.scale.set(1, 0.35, 0.6); p.rotation.y = -a; p.rotation.z = 0.5; }
      sphere(`flower_light_bloom_${i + 1}`, g, core, 0.02, bx, top + 0.02, bz).castShadow = false;
      if (i < 3) for (let k = 0; k < 3; k++) { const ly = 0.6 + r() * (top - 0.8); const lf = mesh(`flower_light_leaf_${i + 1}_${k + 1}`, new THREE.SphereGeometry(0.05, 8, 5), std('#4f6e36', 0.7), g, cx + dx * (ly / top) * 1.4 + (r() - 0.5) * 0.08, ly, cz + dz * (ly / top) * 1.4 + (r() - 0.5) * 0.08); lf.scale.set(0.35, 1.3, 0.12); lf.rotation.z = (r() - 0.5) * 1.2; } });
    decals.push(decal('glow_flower_light_wall_screen', g, T.glow, '#d58cff', 0.55, 1.4, 1.6, cx, 1.45, 0.006, '+z'), decal('glow_flower_light_wall_side', g, T.glow, '#7fb0ff', 0.5, 1.4, 1.6, W - 0.006, 1.45, cz + 0.3, '-x'), decal('glow_flower_light_floor', g, T.glow, '#ff9ccf', 0.4, 1.2, 1.2, cx - 0.2, 0.014, cz + 0.2, 'up'));
    const l = pl('light_flower_light', g, '#e0a8ff', cx - 0.1, 1.55, cz + 0.1, 3);
    register('flower_light', { room, label: 'RGB flower light', type: 'light', group: g, emissive: [...rgb.map(mat => ({ mat, on: 4.5 })), { mat: core, on: 6 }], lights: [{ light: l, on: 1.2 }], decals, show, anchor: new THREE.Vector3(cx, 2.0, cz) });
  }
  // candy shelf backlight (LED behind each floating shelf, washing the red wall)
  { const g = G('candy_shelf_backlight', devGroup), m = lightMats(), decals = [], lights = [];
    [1.3, 1.68, 2.06].forEach((sy, si) => {
      B(`light_candy_shelf_${si + 1}`, g, m.bulb, 0.004, 0.012, sy - 0.035, sy - 0.005, CZ0 + 0.03, CZ1 - 0.03).castShadow = false;
      const d = decal(`glow_candy_shelf_${si + 1}`, g, T.wash, '#ffd9a0', 0.85, CZ1 - CZ0 + 0.3, 0.45, 0.006, sy + 0.18, (CZ0 + CZ1) / 2, '-x'); d.rotation.z = 0; decals.push(d);
      const d2 = decal(`glow_candy_shelf_${si + 1}_under`, g, T.wash, '#ffd9a0', 0.55, CZ1 - CZ0 + 0.2, 0.3, 0.006, sy - 0.18, (CZ0 + CZ1) / 2, '-x'); d2.rotation.x = Math.PI; decals.push(d2);
    });
    lights.push(pl('light_candy_shelves', g, '#ffd9a0', 0.35, 1.7, (CZ0 + CZ1) / 2, 2.4));
    register('candy_shelf_backlight', { room, label: 'Candy shelf backlight', type: 'light', group: g, emissive: [{ mat: m.bulb, on: 6 }], lights: lights.map(l => ({ light: l, on: 1.2 })), decals, anchor: new THREE.Vector3(0.2, 2.25, (CZ0 + CZ1) / 2) });
  }
  // popcorn machine: lit kettle + warm cabinet glow
  { const g = G('popcorn_machine', devGroup);
    const decals = [decal('glow_popcorn_floor', g, T.glow, STYLE.warm, 0.45, 1.4, 1.4, PX + 0.15, 0.016, PZ - 0.15, 'up')];
    const l = pl('light_popcorn', g, '#ffcf7a', PX + 0.05, 1.15, PZ - 0.05, 2.2);
    register('popcorn_machine', { room, label: 'Popcorn machine', type: 'appliance', group: g, emissive: [{ mat: popKettleMat, on: 1.2 }, { mat: popInnerMat, on: 3.5 }, { mat: popCornMat, on: 0.5 }], lights: [{ light: l, on: 1.6 }], decals, anchor: new THREE.Vector3(PX, 1.72, PZ) });
  }

  Object.keys(K.devices).forEach(n => K.setDevice(n, false));
  return { root, devices: K.devices, setDevice: K.setDevice, textures: T };
}

export const PRESETS = {
  all_off: {},
  all_on: { projector: 1, screen_bias_light: 1, recessed_lights: 1, fireplace: 1, rear_string_lights: 1, riser_step_lights: 1, candy_shelf_backlight: 1, popcorn_machine: 1, flower_light: 1, table_lamps: 1 },
  movie_night: { projector: 1, screen_bias_light: 1, rear_string_lights: 1, riser_step_lights: 1, fireplace: 1, candy_shelf_backlight: 1, flower_light: 1, table_lamps: 0.3 },
  party: { recessed_lights: 1, fireplace: 1, rear_string_lights: 1, riser_step_lights: 1, candy_shelf_backlight: 1, popcorn_machine: 1, flower_light: 1, table_lamps: 1 },
};

export const VIEWS = {
  dollhouse: { pos: [10.2, 12.2, 19.8], target: [2.5, 0.5, 4.0], fov: 30 },
  hero: { pos: [7.9, 7.0, 14.2], target: [2.2, 0.8, 4.4], fov: 34 },
  screen: { pos: [3.1, 1.75, 7.6], target: [2.7, 1.25, 0], fov: 58, interior: true },
  fireplace: { pos: [4.4, 1.45, 5.6], target: [0.2, 1.1, 3.0], fov: 62, interior: true },
  riser: { pos: [1.0, 1.8, 3.4], target: [3.9, 0.7, 7.0], fov: 62, interior: true },
  rear_panels: { pos: [1.4, 1.75, 5.4], target: [2.8, 1.25, 8.6], fov: 66, interior: true },
  flower: { pos: [2.8, 1.5, 3.0], target: [5.0, 1.2, 0.3], fov: 55, interior: true },
  seats_from_screen: { pos: [3.6, 1.7, 0.6], target: [3.9, 0.7, 6.5], fov: 62, interior: true },
  candy_wall: { pos: [3.0, 1.55, 6.0], target: [0.0, 1.35, 7.4], fov: 55, interior: true },
  top: { pos: [6.0, 20, 13.5], target: [2.7, 0, 4.3], fov: 34 },
};

export const createMovieRoom = (container, opts = {}) => createApp(container, { build: buildMovieRoom, views: VIEWS, presets: PRESETS, shadow: { center: [2.7, 0, 4.3], half: 8 }, ...opts });
