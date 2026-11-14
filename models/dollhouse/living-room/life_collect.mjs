// Page-evaluable collector for the "life" nodes named in ios/Rooms/README.md (cat, pose groups, spots, props, drums, fan, curtains, sparkles).
// Usage in a render.mjs: const life = await page.evaluate(lifeInfo);
export function lifeInfo() {
  const root = window.app.room ? window.app.room.root : window.app.den.root;
  const SPARKLE = ['table_lamp_shade', 'table_lamp_booth_shade', 'table_lamp_1_shade', 'table_lamp_2_shade', 'dresser_lamp_shade', 'sconce_left_shade', 'sconce_right_shade', 'sconce_left_bulb_1', 'sconce_right_bulb_1', 'chandelier_bulb_1', 'chandelier_lower_bulb_1', 'dining_pendant_bulb', 'flower_light_head', 'flower_light_bloom_1', 'prop_lamp'];
  const out = { cat: null, cat_spots: [], props: [], drums: [], fan_blades: null, curtains: [], sparkles: [], exterior_nodes: [] };
  root.updateMatrixWorld(true);
  const r3 = (v) => v.toArray().map(x => +x.toFixed(3)), wp = (o) => r3(o.getWorldPosition(o.position.clone()));
  // skip nodes that never reach the GLB (no-export or interior-only, on the node or any parent), so life lists only exported names
  const notExported = (o) => { for (let p = o; p; p = p.parent) if (p.userData.noExport || p.userData.interiorOnly) return true; return false; };
  root.traverse(o => {
    const n = o.name;
    if (notExported(o)) return;
    if (/^exterior_|_glimpse$/.test(n)) out.exterior_nodes.push(n);
    if (n === 'pet_cat') out.cat = { name: n, world_position_m: wp(o), yaw_rad: +o.rotation.y.toFixed(3),
      fallback_meshes: o.children.filter(c => c.isMesh).map(c => c.name),
      pose_groups: o.children.filter(c => c.name.startsWith('pet_cat_pose_')).map(c => ({ name: c.name, default_visible: c.userData.defaultVisible !== false,
        head_joint: c.getObjectByName(c.name + '_head')?.name || null, tail_joint: c.getObjectByName(c.name + '_tail')?.name || null })) };
    else if (n.startsWith('cat_spot_')) out.cat_spots.push({ name: n, world_position_m: wp(o), yaw_rad: +o.rotation.y.toFixed(3) });
    else if (/^prop_[a-z0-9_]+$/.test(n)) out.props.push({ name: n, world_position_m: wp(o), parent: o.parent.name });
    else if (/^(washer|dryer)_drum$/.test(n)) out.drums.push({ name: n, world_position_m: wp(o), spin_axis_local: 'z', door_axis_local: '+z' });
    else if (n === 'ceiling_fan_blades') out.fan_blades = { name: n, world_position_m: wp(o), spin_axis_local: 'y' };
    if (o.isMesh && /curtain/.test(n) && !/rod|track|light/.test(n)) out.curtains.push({ name: n, heading_world_m: wp(o), sway_axis_local: 'x' });
    if (SPARKLE.includes(n)) out.sparkles.push({ name: n, world_position_m: wp(o) });
  });
  return out;
}
