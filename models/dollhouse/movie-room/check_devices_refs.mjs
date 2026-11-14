// Checks every node name referenced by a room's devices.json against the exported GLBs (parsing the GLB JSON, no deps).
// Flags: missing from the all-on GLB; missing from the all-off GLB (glow_/fire_flame_ and other on-only nodes are allowed there);
// names the USDZ converter would change ('.' etc. -> '_'); names that occur more than once in the GLB (the converter suffixes
// repeats, so only the first keeps the name); anchor_* world position != devices.json position_m.
// Usage: node check_devices_refs.mjs <devices.json> <off.glb> <all_on.glb>
import fs from 'fs';
const [dj, offF, onF] = process.argv.slice(2);
const D = JSON.parse(fs.readFileSync(dj));
const load = (f) => { const b = fs.readFileSync(f); const len = b.readUInt32LE(12); return JSON.parse(b.subarray(20, 20 + len).toString()); };
const mul = (a, b) => { const o = new Array(16).fill(0); for (let r = 0; r < 4; r++) for (let c = 0; c < 4; c++) for (let k = 0; k < 4; k++) o[c * 4 + r] += a[k * 4 + r] * b[c * 4 + k]; return o; };
const local = (n) => { if (n.matrix) return n.matrix; const [x, y, z, w] = n.rotation || [0, 0, 0, 1], [sx, sy, sz] = n.scale || [1, 1, 1], [tx, ty, tz] = n.translation || [0, 0, 0];
  return [(1 - 2 * (y * y + z * z)) * sx, 2 * (x * y + z * w) * sx, 2 * (x * z - y * w) * sx, 0, 2 * (x * y - z * w) * sy, (1 - 2 * (x * x + z * z)) * sy, 2 * (y * z + x * w) * sy, 0, 2 * (x * z + y * w) * sz, 2 * (y * z - x * w) * sz, (1 - 2 * (x * x + y * y)) * sz, 0, tx, ty, tz, 1]; };
const info = (j) => { const count = {}, world = {}; const walk = (i, M) => { const n = j.nodes[i], W = mul(M, local(n)); count[n.name] = (count[n.name] || 0) + 1; if (!(n.name in world)) world[n.name] = [W[12], W[13], W[14]]; (n.children || []).forEach(c => walk(c, W)); };
  const I = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]; j.scenes[j.scene || 0].nodes.forEach(i => walk(i, I)); return { count, world }; };
const on = info(load(onF)), off = info(load(offF));
const onOnly = (n) => /^(glow_|fire_flame_|light_cone_|light_shaft)/.test(n);
const issues = []; let refs = 0;
for (const d of D.devices) {
  const names = [['anchor_node', d.anchor_node], ['group_node', d.group_node], ...(d.light_nodes || []).map(n => ['light_nodes', n]), ...(d.glow_decals || []).map(n => ['glow_decals', n]),
    ...(d.mesh_nodes || []).map(n => ['mesh_nodes', n]), ...((d.on_state && d.on_state.shows) || []).map(n => ['on_state.shows', n])].filter(([, n]) => n);
  for (const [field, n] of names) { refs++;
    if (!on.count[n]) issues.push(`${d.name}.${field}: "${n}" not in ${onF}`);
    else if (!off.count[n] && !onOnly(n) && !(d.on_state?.shows || []).includes(n) && !(d.glow_decals || []).includes(n)) issues.push(`${d.name}.${field}: "${n}" not in ${offF}`);
    if (/[^A-Za-z0-9_]/.test(n) || /^\d/.test(n)) issues.push(`${d.name}.${field}: "${n}" would be renamed by the USDZ converter`);
    if (on.count[n] > 1) issues.push(`${d.name}.${field}: "${n}" occurs ${on.count[n]}x in the GLB (converter suffixes repeats)`); }
  const w = on.world[d.anchor_node]; if (w && d.position_m && w.some((v, k) => Math.abs(v - d.position_m[k]) > 0.002)) issues.push(`${d.name}: anchor at ${w.map(v => v.toFixed(3))} but position_m ${d.position_m}`);
}
const L = D.life || {}; for (const n of [L.cat?.name, ...(L.cat_spots || []).map(s => s.name), ...(L.props || []).map(p => p.name), ...(L.drums || []).map(p => p.name), L.fan_blades?.name, ...(L.curtains || []).map(c => c.name || c), ...(L.sparkles || []).map(s => s.name || s)].filter(Boolean)) {
  refs++; if (!on.count[n]) issues.push(`life: "${n}" not in ${onF}`); else if (on.count[n] > 1) issues.push(`life: "${n}" occurs ${on.count[n]}x`); }
console.log(`${dj}: ${refs} references checked, ${issues.length} issue(s)`); issues.forEach(s => console.log('  - ' + s));
process.exit(issues.length ? 1 : 0);
