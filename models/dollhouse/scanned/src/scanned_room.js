// Dollhouse room built from a tidy scan plan (scanner/tools/scan2room.py -> <room>/plan.json).
// Follows living-room/style.md: cut cream wall tops, 0.30 m slab, soft low-poly furniture, the
// shared studio lighting from kitchen/src/common.js. The plan already sits in the style frame
// (back corner at the origin, kept walls on x=0 and z=0, interior x<0, z>0).
//
// This is the starting point for a room, not the finished room: it gets the size, the doors, the
// windows and where the furniture goes right, and the hand pass (like kitchen.js) adds the detail
// and the devices. A whole house is the same rooms placed by house.json (scanner/tools/floorplan.py).
import * as THREE from 'three';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import { STYLE, std, makeBaseTextures, makeKit, createApp } from '../../kitchen/src/common.js';

const CUT = STYLE.cut;                // '#efe3cf'
const SLAB_SIDE = STYLE.slab;         // '#e2d3bb'
const WALL_T = 0.22;                  // exterior wall thickness (style.md)
const SLAB_T = 0.30;
const HOUSE_CUT = 1.2;                // every wall in the whole-house view is cut at this height
const PARTITION_CUT = 1.1;            // style.md section 9

// Furniture palette by category; a sampled colour only nudges these (scans are a guide).
const PALETTE = {
  sofa: '#6f7377', chair: '#7a4a2b', bed: '#e8dccb', table: '#6c4325', storage: '#b9a38b',
  television: '#1c1c1e', fireplace: '#9c8f84', refrigerator: '#d9dde1', stove: '#d9dde1', oven: '#d9dde1',
  dishwasher: '#d9dde1', washerDryer: '#eef0f2', sink: '#f1efea', toilet: '#f4f6f8', bathtub: '#f4f6f8',
  stairs: '#6c4325', wall: STYLE.plaster, floor: '#c8a27a', door: '#6c4325', default: '#c9c2b8',
};

const hexToColor = (h) => new THREE.Color(h);
/** Blend a sampled colour toward the style colour: k = how much of the sample survives. */
export function nudge(sample, style, k = 0.45) {
  const base = hexToColor(style);
  if (!sample) return base;
  try { return base.clone().lerp(hexToColor(sample), k); } catch { return base; }
}

const len2 = (a, b) => Math.hypot(b[0] - a[0], b[1] - a[1]);
const signedArea = (poly) => poly.reduce((s, p, i) => { const q = poly[(i + 1) % poly.length]; return s + p[0] * q[1] - q[0] * p[1]; }, 0) / 2;
const ccw = (poly) => (signedArea(poly) > 0 ? poly : [...poly].reverse());

export function pointInPolygon([x, z], poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, zi] = poly[i], [xj, zj] = poly[j];
    if ((zi > z) !== (zj > z) && x < ((xj - xi) * (z - zi)) / (zj - zi) + xi) inside = !inside;
  }
  return inside;
}

/** Push every edge of a CCW polygon outward by d (miter joins). */
export function offsetPolygon(poly, d) {
  const P = ccw(poly), n = P.length, out = [];
  const nrm = (a, b) => { const L = len2(a, b) || 1; return [(b[1] - a[1]) / L, -(b[0] - a[0]) / L]; };
  for (let i = 0; i < n; i++) {
    const a = P[(i - 1 + n) % n], b = P[i], c = P[(i + 1) % n];
    const n1 = nrm(a, b), n2 = nrm(b, c);
    const m = [n1[0] + n2[0], n1[1] + n2[1]];
    const dot = n1[0] * m[0] + n1[1] * m[1];
    const k = Math.abs(dot) < 1e-6 ? d : d / dot;
    out.push([b[0] + m[0] * k, b[1] + m[1] * k]);
  }
  return out;
}

// ---------------------------------------------------------------- pieces

function box(name, parent, mat, w, h, d, x, y, z, r = 0) {
  const rr = Math.min(r, w / 2 - 1e-4, h / 2 - 1e-4, d / 2 - 1e-4);
  const geo = rr > 0.002 ? new RoundedBoxGeometry(w, h, d, 2, rr) : new THREE.BoxGeometry(w, h, d);
  const m = new THREE.Mesh(geo, mat); m.name = name; m.position.set(x, y, z);
  m.castShadow = true; m.receiveShadow = true; parent.add(m); return m;
}

function cyl(name, parent, mat, r, h, x, y, z, seg = 20) {
  const m = new THREE.Mesh(new THREE.CylinderGeometry(r, r, h, seg), mat); m.name = name; m.position.set(x, y, z);
  m.castShadow = true; m.receiveShadow = true; parent.add(m); return m;
}

function floorSlab(name, parent, outline, floorColor, grow = 0) {
  const pts = grow ? offsetPolygon(outline, grow) : ccw(outline);
  const shape = new THREE.Shape(pts.map(([x, z]) => new THREE.Vector2(x, -z)));
  const slabGeo = new THREE.ExtrudeGeometry(shape, { depth: SLAB_T, bevelEnabled: false });
  const slab = new THREE.Mesh(slabGeo, [std(floorColor, 0.85), std(SLAB_SIDE, 0.9)]);
  slab.name = name; slab.rotation.x = -Math.PI / 2; slab.position.y = -SLAB_T;
  slab.receiveShadow = true; parent.add(slab);
  return slab;
}

/** A wall along a->b (interior face on the line, thickness outward), with holes for its openings. */
function wallWithOpenings(group, wall, openings, { height, cut, thickness, color, extendA, extendB, prefix }) {
  const a = wall.a, b = wall.b, L = len2(a, b);
  const holder = new THREE.Group(); holder.name = prefix;
  holder.position.set(a[0], 0, a[1]);
  holder.rotation.y = -Math.atan2(b[1] - a[1], b[0] - a[0]);
  group.add(holder);
  const H = Math.min(height, cut ?? height);
  const mat = std(color, 0.9), capMat = std(CUT, 0.95);
  const zc = -thickness / 2;          // local -z is outward for a CCW outline
  const ops = openings.map(o => ({ ...o, s: o.offset, e: o.offset + o.width })).sort((p, q) => p.s - q.s);
  const spans = []; let cur = -extendA;
  for (const o of ops) { if (o.s > cur) spans.push([cur, o.s, 0, H]); cur = Math.max(cur, o.e); }
  if (L + extendB > cur) spans.push([cur, L + extendB, 0, H]);
  for (const o of ops) {
    const top = Math.min(o.bottom + o.height, H);
    if (o.bottom > 0.01) spans.push([o.s, o.e, 0, Math.min(o.bottom, H)]);
    if (top < H - 0.01) spans.push([o.s, o.e, top, H]);
  }
  spans.forEach(([s, e, y0, y1], i) => {
    if (e - s < 0.005 || y1 - y0 < 0.005) return;
    box(`${prefix}_part_${i + 1}`, holder, mat, e - s, y1 - y0, thickness, (s + e) / 2, (y0 + y1) / 2, zc);
    if (Math.abs(y1 - H) < 1e-4) box(`${prefix}_cap_${i + 1}`, holder, capMat, e - s, 0.012, thickness + 0.004, (s + e) / 2, H + 0.006, zc);
  });
  return holder;
}

function doorOrWindow(group, wall, o, { cut, thickness, colors, prefix, drawLeaf }) {
  const a = wall.a, b = wall.b;
  const holder = new THREE.Group(); holder.name = `${prefix}_${o.id}`;
  holder.position.set(a[0], 0, a[1]);
  holder.rotation.y = -Math.atan2(b[1] - a[1], b[0] - a[0]);
  group.add(holder);
  const trim = std('#6c4325', 0.55);
  const x0 = o.offset, x1 = o.offset + o.width, top = o.bottom + o.height, xm = (x0 + x1) / 2;
  const visTop = Math.min(top, cut ?? top);
  const casing = 0.05;
  // casing on the room side
  if (visTop > o.bottom + 0.05) {
    box(`${holder.name}_casing_l`, holder, trim, casing, visTop - o.bottom, 0.03, x0 - casing / 2, (o.bottom + visTop) / 2, 0.015, 0.004);
    box(`${holder.name}_casing_r`, holder, trim, casing, visTop - o.bottom, 0.03, x1 + casing / 2, (o.bottom + visTop) / 2, 0.015, 0.004);
    if (top <= (cut ?? top)) box(`${holder.name}_casing_top`, holder, trim, o.width + 2 * casing, casing, 0.03, xm, top + casing / 2, 0.015, 0.004);
  }
  if (o.kind === 'window') {
    if (o.bottom < (cut ?? 99)) box(`${holder.name}_sill`, holder, trim, o.width + 0.12, 0.035, 0.12, xm, o.bottom - 0.0175, 0.03, 0.006);
    const gh = visTop - o.bottom;
    if (gh > 0.02) {
      const glass = new THREE.MeshStandardMaterial({ color: '#cfe2e8', transparent: true, opacity: 0.22, roughness: 0.05 });
      const pane = box(`${holder.name}_pane`, holder, glass, o.width, gh, 0.02, xm, o.bottom + gh / 2, -thickness / 2);
      pane.castShadow = false;
      box(`${holder.name}_muntin`, holder, trim, 0.03, gh, 0.04, xm, o.bottom + gh / 2, -thickness / 2);
    }
  } else if (o.kind === 'door' && drawLeaf) {
    const h = Math.min(o.height, cut ?? o.height) - 0.01;
    if (h > 0.05) {
      const leaf = new THREE.Group(); leaf.name = `${holder.name}_leaf`;
      leaf.position.set(x0 + 0.02, 0, 0.0);
      leaf.rotation.y = o.is_open === false ? 0 : -1.2;     // swing into the room
      holder.add(leaf);
      box(`${holder.name}_leaf_panel`, leaf, std(nudge(colors[o.id] || o.color, PALETTE.door, 0.35), 0.6), o.width - 0.04, h, 0.04, (o.width - 0.04) / 2, h / 2, 0.02, 0.006);
      cyl(`${holder.name}_leaf_knob`, leaf, std(STYLE.brass, 0.35, 0.85), 0.025, 0.03, o.width - 0.12, Math.min(1.0, h * 0.9), 0.05, 12).rotation.x = Math.PI / 2;
    }
  }
  return holder;
}

// ---------------------------------------------------------------- furniture

/** Which local side of a piece is its back: the one closest to a wall. */
function backSign(obj, outline) {
  const [x, , z] = obj.position, [, , d] = obj.size, a = obj.rotation_y || 0;
  const probe = (s) => {
    const lx = 0, lz = s * (d / 2 + 0.25);
    const wx = x + lx * Math.cos(a) + lz * Math.sin(a), wz = z - lx * Math.sin(a) + lz * Math.cos(a);
    return pointInPolygon([wx, wz], outline) ? 1 : 0;
  };
  const pPlus = probe(1), pMinus = probe(-1);
  if (pPlus === pMinus) return -1;
  return pPlus ? -1 : 1;     // the side that pokes out of the room (into a wall) is the back
}

export function buildFurniture(obj, parent, outline) {
  const g = new THREE.Group(); g.name = `furniture_${obj.id}`;
  g.position.set(obj.position[0], obj.position[1], obj.position[2]);
  g.rotation.y = obj.rotation_y || 0;
  g.userData = { category: obj.category, label: obj.label, source_id: obj.source_id };
  parent.add(g);
  const [w, h, d] = obj.size;
  const col = nudge(obj.color, PALETTE[obj.category] || PALETTE.default, 0.4);
  const m = std(col, obj.category === 'television' ? 0.12 : 0.85, obj.category === 'television' ? 0.3 : 0);
  const wood = std('#6c4325', 0.55);
  const steel = std('#c9ced3', 0.35, 0.6);
  const dark = std('#2e1a0e', 0.4);
  const back = backSign(obj, outline);       // -1: back is local -z
  const bz = (t) => back * (d / 2 - t / 2);   // centre z of a slab of thickness t against the back
  const n = (p) => `${g.name}_${p}`;
  switch (obj.category) {
    case 'sofa': {
      const seatH = Math.min(0.45, h * 0.5), armW = Math.min(0.18, w * 0.12), backT = Math.min(0.24, d * 0.28);
      box(n('base'), g, m, w, seatH * 0.6, d, 0, seatH * 0.3, 0, 0.05);
      box(n('back'), g, m, w, h, backT, 0, h / 2, bz(backT), 0.07);
      box(n('arm_l'), g, m, armW, h * 0.68, d, -w / 2 + armW / 2, h * 0.34, 0, 0.06);
      box(n('arm_r'), g, m, armW, h * 0.68, d, w / 2 - armW / 2, h * 0.34, 0, 0.06);
      const cw = (w - 2 * armW) / 2;
      for (let i = 0; i < 2; i++) box(n(`cushion_${i + 1}`), g, std(col.clone().offsetHSL(0, 0, 0.04), 0.95), cw - 0.02, seatH * 0.4, d - backT - 0.02, -w / 2 + armW + cw * (i + 0.5), seatH * 0.8, -back * backT / 2, 0.06);
      break;
    }
    case 'chair': {
      const seatH = Math.min(0.46, h * 0.5);
      box(n('seat'), g, m, w, 0.06, d, 0, seatH, 0, 0.02);
      box(n('back'), g, m, w, h - seatH, 0.05, 0, seatH + (h - seatH) / 2, bz(0.05), 0.02);
      for (const [sx, sz] of [[-1, -1], [1, -1], [-1, 1], [1, 1]]) box(n(`leg_${sx}_${sz}`), g, wood, 0.04, seatH, 0.04, sx * (w / 2 - 0.04), seatH / 2, sz * (d / 2 - 0.04));
      break;
    }
    case 'bed': {
      const baseH = Math.min(0.3, h * 0.45), headT = 0.08;
      box(n('frame'), g, wood, w, baseH, d, 0, baseH / 2, 0, 0.02);
      box(n('mattress'), g, std('#f1ece2', 0.95), w - 0.06, 0.22, d - headT - 0.04, 0, baseH + 0.11, -back * headT / 2, 0.06);
      box(n('duvet'), g, m, w - 0.02, 0.06, (d - headT) * 0.62, 0, baseH + 0.23, -back * (d - headT) * 0.19, 0.03);
      box(n('headboard'), g, wood, w, Math.max(h, baseH + 0.6), headT, 0, Math.max(h, baseH + 0.6) / 2, bz(headT), 0.02);
      const pw = Math.min(0.6, (w - 0.2) / 2);
      for (const s of (w > 1.2 ? [-1, 1] : [0])) box(n(`pillow_${s}`), g, std('#f6f2ea', 0.95), pw, 0.12, 0.35, s * (pw / 2 + 0.05), baseH + 0.28, back * (d / 2 - headT - 0.22), 0.06);
      break;
    }
    case 'table': {
      if (h < 0.55) {   // coffee / side table
        box(n('top'), g, wood, w, 0.05, d, 0, h - 0.025, 0, 0.01);
        box(n('base'), g, dark, w * 0.8, h - 0.05, d * 0.7, 0, (h - 0.05) / 2, 0, 0.01);
      } else {
        box(n('top'), g, wood, w, 0.045, d, 0, h - 0.0225, 0, 0.008);
        for (const [sx, sz] of [[-1, -1], [1, -1], [-1, 1], [1, 1]]) box(n(`leg_${sx}_${sz}`), g, wood, 0.06, h - 0.045, 0.06, sx * (w / 2 - 0.07), (h - 0.045) / 2, sz * (d / 2 - 0.07));
      }
      break;
    }
    case 'storage': {
      box(n('plinth'), g, dark, w - 0.04, 0.08, d - 0.04, 0, 0.04, 0);
      box(n('body'), g, m, w, h - 0.08, d, 0, 0.08 + (h - 0.08) / 2, 0, 0.008);
      const doors = Math.max(1, Math.round(w / 0.5));
      for (let i = 1; i < doors; i++) box(n(`gap_${i}`), g, dark, 0.008, h - 0.12, 0.01, -w / 2 + (w * i) / doors, 0.08 + (h - 0.08) / 2, -back * (d / 2 + 0.002));
      break;
    }
    case 'television': {
      const t = Math.min(d, 0.06);
      box(n('screen'), g, m, w, h, t, 0, h / 2, bz(t), 0.01);
      break;
    }
    case 'fireplace': {
      box(n('surround'), g, m, w, h, d, 0, h / 2, 0, 0.01);
      box(n('firebox'), g, std('#17161a', 0.9), w * 0.55, h * 0.45, 0.05, 0, h * 0.3, -back * (d / 2 + 0.001));
      break;
    }
    case 'refrigerator': case 'oven': case 'dishwasher': case 'washerDryer': case 'stove': {
      box(n('body'), g, steel, w, h, d, 0, h / 2, 0, 0.012);
      box(n('door_line'), g, dark, w - 0.04, 0.006, 0.006, 0, h * (obj.category === 'refrigerator' ? 0.62 : 0.8), -back * (d / 2 + 0.002));
      if (obj.category === 'stove') for (const [sx, sz] of [[-1, -1], [1, -1], [-1, 1], [1, 1]]) cyl(n(`burner_${sx}_${sz}`), g, dark, Math.min(w, d) * 0.12, 0.01, sx * w * 0.22, h + 0.005, sz * d * 0.2, 18);
      if (obj.category === 'washerDryer') cyl(n('drum'), g, std('#9fb2bf', 0.2), Math.min(w, h) * 0.3, 0.02, 0, h * 0.5, -back * (d / 2 + 0.01), 24).rotation.x = Math.PI / 2;
      break;
    }
    case 'sink': {
      box(n('cabinet'), g, std('#2b1f1a', 0.6), w, h - 0.04, d, 0, (h - 0.04) / 2, 0, 0.006);
      box(n('counter'), g, std('#f1efea', 0.35), w + 0.02, 0.04, d + 0.02, 0, h - 0.02, 0, 0.004);
      box(n('basin'), g, std('#d4d7da', 0.3, 0.5), Math.min(w * 0.6, 0.6), 0.012, d * 0.55, 0, h + 0.001, 0, 0.02);
      cyl(n('faucet'), g, steel, 0.015, 0.25, 0, h + 0.125, back * (d / 2 - 0.06), 12);
      break;
    }
    case 'toilet': {
      box(n('tank'), g, m, w * 0.9, h * 0.5, d * 0.28, 0, h * 0.5 + h * 0.25, bz(d * 0.28), 0.03);
      cyl(n('bowl'), g, m, Math.min(w, d) * 0.3, h * 0.5, 0, h * 0.25, -back * d * 0.1, 24);
      break;
    }
    case 'bathtub': {
      box(n('tub'), g, m, w, h, d, 0, h / 2, 0, 0.05);
      box(n('basin'), g, std('#e3ecef', 0.2), w - 0.14, 0.01, d - 0.14, 0, h + 0.001, 0, 0.05);
      break;
    }
    case 'stairs': {
      const steps = Math.max(2, Math.round(h / 0.19)); const along = w >= d ? 'x' : 'z'; const run = along === 'x' ? w : d;
      for (let i = 0; i < steps; i++) {
        const sh = (h * (i + 1)) / steps, s0 = -run / 2 + (run * i) / steps, len = run / steps;
        if (along === 'x') box(n(`step_${i + 1}`), g, wood, len, sh, d, s0 + len / 2, sh / 2, 0, 0.004);
        else box(n(`step_${i + 1}`), g, wood, w, sh, len, 0, sh / 2, s0 + len / 2, 0.004);
      }
      break;
    }
    default:
      box(n('body'), g, m, w, h, d, 0, h / 2, 0, Math.min(0.05, Math.min(w, h, d) * 0.15));
  }
  return g;
}

// ---------------------------------------------------------------- one room

/**
 * mode 'room': back walls full height, camera-side walls left out (style.md).
 * mode 'house': every wall cut at HOUSE_CUT, walls shared with a neighbour are half thickness.
 * neighbours(x, z) -> true when a point just outside a wall is inside another room (house mode).
 */
export function buildRoomGroup(plan, { mode = 'room', neighbours = null, drawDoorLeaf = () => true } = {}) {
  const root = new THREE.Group(); root.name = `scanned_${plan.slug}`;
  const arch = new THREE.Group(); arch.name = 'architecture'; root.add(arch);
  const dw = new THREE.Group(); dw.name = 'doors_and_windows'; root.add(dw);
  const furn = new THREE.Group(); furn.name = 'furniture'; root.add(furn);
  const outline = ccw(plan.outline);
  const colors = {};
  floorSlab('floor_slab', arch, outline, nudge(plan.floor_color, PALETTE.floor, 0.5), mode === 'house' ? 0.075 : WALL_T);
  const walls = plan.walls;
  const n = walls.length;
  const convex = (i) => { const p = walls[(i - 1 + n) % n], c = walls[i]; const ax = c.a[0] - p.a[0], az = c.a[1] - p.a[1], bx = c.b[0] - c.a[0], bz = c.b[1] - c.a[1]; return ax * bz - az * bx > 0; };
  walls.forEach((w, i) => {
    if (w.open) return;
    const ops = plan.openings.filter(o => o.wall === w.id);
    let cut = null, thickness = WALL_T;
    if (mode === 'room') {
      if (w.side === 'front') return;      // style.md: leave the walls nearest the camera out
    } else {
      cut = HOUSE_CUT;
      const mid = [(w.a[0] + w.b[0]) / 2 + w.normal[0] * 0.2, (w.a[1] + w.b[1]) / 2 + w.normal[1] * 0.2];
      if (neighbours && neighbours(mid)) thickness = 0.075;
      else thickness = 0.18;
    }
    const ext = (j) => (convex(j) ? thickness : 0);
    wallWithOpenings(arch, w, ops, { height: w.height || plan.height, cut, thickness, color: nudge(w.color, PALETTE.wall, 0.45),
      extendA: ext(i), extendB: ext((i + 1) % n), prefix: `wall_${i + 1}` });
    ops.forEach(o => doorOrWindow(dw, w, o, { cut, thickness, colors, prefix: 'opening', drawLeaf: drawDoorLeaf(o) }));
  });
  (plan.partitions || []).forEach((p, i) => {
    const pw = { a: p.a, b: p.b };
    const ops = plan.openings.filter(o => o.wall === p.id);
    wallWithOpenings(arch, pw, ops, { height: p.height || plan.height, cut: mode === 'house' ? HOUSE_CUT : PARTITION_CUT, thickness: 0.12,
      color: nudge(p.color, PALETTE.wall, 0.45), extendA: 0, extendB: 0, prefix: `partition_${i + 1}` }).position.z += 0;
  });
  (plan.objects || []).forEach(o => buildFurniture(o, furn, outline));
  return root;
}

/** style.md dollhouse camera: FOV 30, ~45 deg azimuth from -x/+z, ~33 deg down, 2.6 x the diagonal. */
export function dollhouseViews(outline, height = 2.5) {
  const xs = outline.map(p => p[0]), zs = outline.map(p => p[1]);
  const cx = (Math.min(...xs) + Math.max(...xs)) / 2, cz = (Math.min(...zs) + Math.max(...zs)) / 2;
  const diag = Math.hypot(Math.max(...xs) - Math.min(...xs), Math.max(...zs) - Math.min(...zs));
  const dist = 2.6 * diag, el = (33 * Math.PI) / 180, hz = dist * Math.cos(el);
  const r = (v) => +v.toFixed(2);
  return {
    dollhouse: { pos: [r(cx - hz * Math.SQRT1_2), r(dist * Math.sin(el)), r(cz + hz * Math.SQRT1_2)], target: [r(cx), 0.5, r(cz)], fov: 30 },
    close: { pos: [r(cx - hz * 0.45), r(dist * 0.42), r(cz + hz * 0.45)], target: [r(cx), 0.8, r(cz)], fov: 31 },
    top: { pos: [r(cx - 0.4 * diag), r(diag * 2.8), r(cz + 0.9 * diag)], target: [r(cx), 0, r(cz)], fov: 34 },
  };
}

export function buildScannedRoom(plan) {
  const T = makeBaseTextures();
  const root = buildRoomGroup(plan, { mode: 'room' });
  const K = makeKit(root, T);   // empty device registry + device_anchors, for the export
  return { root, devices: K.devices, setDevice: K.setDevice, textures: T };
}

export const createScannedRoom = (container, plan, opts = {}) => {
  const views = dollhouseViews(plan.outline, plan.height);
  const t = views.dollhouse.target;
  return createApp(container, { build: () => buildScannedRoom(plan), views, presets: { all_off: {}, all_on: {} }, shadow: { center: [t[0], 0, t[2]], half: 9 }, ...opts });
};

// ---------------------------------------------------------------- whole house

/** house: house.json from floorplan.py; plans: { name: plan.json }. Stories are stacked by y. */
export function buildHouse(house, plans, { story = null } = {}) {
  const T = makeBaseTextures();
  const root = new THREE.Group(); root.name = 'house_plan';
  const rooms = house.rooms.filter(r => story === null || r.story === story);
  const polys = rooms.map(r => ({ name: r.name, story: r.story, poly: r.outline }));
  const seenDoors = [];
  for (const r of rooms) {
    const plan = plans[r.name]; if (!plan) continue;
    const pl = r.placement;
    const c = Math.cos(pl.quarter_turns * Math.PI / 2), s = Math.sin(pl.quarter_turns * Math.PI / 2);
    const toHouse = ([x, z]) => [x * c - z * s + pl.x, x * s + z * c + pl.z];
    const neighbours = (p) => polys.some(o => o.name !== r.name && o.story === r.story && pointInPolygon(toHouse(p), o.poly));
    const drawDoorLeaf = (o) => {
      const w = plan.walls.find(w => w.id === o.wall); if (!w) return true;
      const L = len2(w.a, w.b) || 1, t = (o.offset + o.width / 2) / L;
      const at = toHouse([w.a[0] + t * (w.b[0] - w.a[0]), w.a[1] + t * (w.b[1] - w.a[1])]);
      if (seenDoors.some(q => Math.hypot(q[0] - at[0], q[1] - at[1]) < 0.4)) return false;
      seenDoors.push(at); return true;
    };
    const g = buildRoomGroup(plan, { mode: 'house', neighbours, drawDoorLeaf });
    g.position.set(pl.x, pl.y, pl.z);
    g.rotation.y = pl.rotation_y;
    g.userData = { room: r.name, story: r.story };
    root.add(g);
  }
  const K = makeKit(root, T);
  return { root, devices: K.devices, setDevice: K.setDevice, textures: T };
}

export function houseViews(house, story = null) {
  const pts = house.rooms.filter(r => story === null || r.story === story).flatMap(r => r.outline);
  const v = dollhouseViews(pts.length ? pts : [[0, 0], [1, 1]]);
  return v;
}

export const createHouse = (container, house, plans, opts = {}) => {
  const story = opts.story ?? null;
  const views = houseViews(house, story);
  const t = views.dollhouse.target;
  const xs = house.rooms.flatMap(r => r.outline.map(p => p[0])), zs = house.rooms.flatMap(r => r.outline.map(p => p[1]));
  const half = Math.max(9, (Math.max(...xs) - Math.min(...xs)) / 2 + 2, (Math.max(...zs) - Math.min(...zs)) / 2 + 2);
  return createApp(container, { build: () => buildHouse(house, plans, { story }), views, presets: { all_off: {}, all_on: {} }, shadow: { center: [t[0], 0, t[2]], half }, ...opts });
};
