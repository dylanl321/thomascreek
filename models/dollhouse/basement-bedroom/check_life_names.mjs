// Verifies the "life" node names inside the exported GLBs by parsing the GLB JSON chunk directly (no deps).
// Usage: node check_life_names.mjs <file.glb> [more.glb ...]   (room is inferred from the file name)
import fs from 'fs';
const POSES = ['sleep', 'sit', 'stretch', 'walk_1', 'walk_2'];
// final names per ios/Rooms/README.md (Node names for Model Generator)
const EXPECT = {
  den: ['pet_cat', 'pet_cat_body', 'pet_cat_head', 'pet_cat_tail', 'pet_cat_tail_tip_white', 'pet_cat_ear_left', 'pet_cat_ear_right', 'pet_cat_ear_inner_left', 'pet_cat_ear_inner_right',
    ...POSES.flatMap(p => [`pet_cat_pose_${p}`, `pet_cat_pose_${p}_head`, `pet_cat_pose_${p}_tail`]),
    'cat_spot_sofa', 'cat_spot_sofa_main', 'cat_spot_rug', 'cat_spot_window', 'cat_spot_fireplace', 'window_sill_low',
    'prop_throw_blanket', 'prop_open_book', 'prop_mug', 'prop_shoes', 'prop_toy', 'chandelier_bulb_1', 'chandelier_lower_bulb_1', 'table_lamp_shade', 'sconce_left_bulb_1', 'sconce_right_bulb_1', 'fire_flame_1', 'light_fireplace', 'tv_screen', 'exterior_deck'],
  kitchen: ['prop_mug', 'prop_kettle', 'prop_mail', 'prop_dishes', 'prop_cutting_board', 'prop_detergent', 'prop_laundry_basket', 'washer_drum', 'dryer_drum', 'glow_washer_drum', 'glow_dryer_drum',
    'table_lamp_booth_shade', 'dining_pendant_bulb', 'exterior_yard', 'living_room_glimpse', 'sunroom_glimpse'],
  movie: ['prop_popcorn_bowl', 'prop_popcorn_bowl_2', 'prop_drink', 'prop_drink_2', 'prop_throw_blanket', 'fireplace_curtain_1', 'fireplace_curtain_2', 'screen_curtain_1', 'screen_curtain_2', 'rear_black_curtain_cut',
    'table_lamp_1_shade', 'table_lamp_2_shade', 'flower_light_head', 'flower_light_bloom_1', 'screen_surface', 'exterior_patio'],
  bedroom: ['prop_throw_blanket', 'prop_open_book', 'prop_shoes', 'prop_laundry_basket', 'prop_lamp', 'ceiling_fan_blades', 'dresser_lamp_shade', 'sconce_left_shade', 'sconce_right_shade'],
};
// names from the earlier life pass that must be GONE (renamed per the README)
const RETIRED = ['pet_cat_pose_walk_a', 'pet_cat_pose_walk_b', 'prop_slippers', 'prop_remote', 'prop_coffee_mug', 'prop_fruit_bowl', 'prop_mail_stack', 'prop_sink_dishes', 'prop_dish_towel', 'prop_popcorn_bowl_a', 'prop_popcorn_bowl_b',
  'prop_soda_cup_a', 'prop_soda_cup_b', 'prop_blanket', 'prop_unmade_throw', 'prop_nightstand_book', 'fan_blades', 'chandelier_upper_bulb_1', 'curtain_screen_1', 'curtain_fireplace_1', 'curtain_rear_black', 'pet_cat_head_ball', 'pet_cat_tail_tube'];
const VOCAB = /^prop_(throw_blanket|open_book|mug|kettle|toy|mail|dishes|cutting_board|shoes|laundry_basket|detergent|popcorn_bowl|drink|lamp)(_\d+)?$/;
const LIFE = /^(pet_cat|cat_spot_|prop_|steam_spot_|washer_drum$|dryer_drum$|exterior_)|_glimpse$|curtain/;
let fail = 0;
for (const f of process.argv.slice(2)) {
  const b = fs.readFileSync(f); const len = b.readUInt32LE(12); const j = JSON.parse(b.subarray(20, 20 + len).toString());
  const nodes = j.nodes || []; const names = nodes.map(n => n.name || '');
  const room = /room_corner|living/.test(f) ? 'den' : /kitchen/.test(f) ? 'kitchen' : /movie/.test(f) ? 'movie' : /bedroom/.test(f) ? 'bedroom' : null;
  const count = {}; names.forEach(n => { count[n] = (count[n] || 0) + 1; });
  const allOn = /_all_on\.glb$/.test(f);   // flames + glow decals are only in the all-on export (README: the committed USDZ is the all-on file)
  const missing = (EXPECT[room] || []).filter(n => !count[n] && (allOn || !/^(fire_flame_|glow_)/.test(n)));
  const dupLife = Object.keys(count).filter(n => count[n] > 1 && LIFE.test(n));
  const top = names.filter(n => /^(pet_cat(_pose_(sleep|sit|stretch|walk_1|walk_2))?(_head|_tail)?$|cat_spot_|prop_[a-z0-9_]+$|steam_spot_|(washer|dryer)_drum$|ceiling_fan_blades$|exterior_)|_glimpse$/.test(n));
  console.log(`\n${f}  [${room}]  nodes=${nodes.length}`);
  console.log('  life nodes:', top.join(', '));
  const hidden = nodes.filter(n => n.extras && n.extras.defaultVisible === false).map(n => n.name);
  if (hidden.length) console.log('  extras.defaultVisible=false:', hidden.join(', '));
  const piv = nodes.filter(n => n.extras && n.extras.pivotAxis).map(n => `${n.name}(${n.extras.pivotAxis})`);
  if (piv.length) console.log('  extras.pivotAxis:', piv.join(', '));
  console.log('  missing:', missing.length ? missing.join(', ') : 'none', '| duplicate life names:', dupLife.length ? dupLife.join(', ') : 'none');
  const retired = RETIRED.filter(n => count[n]); const strayProps = top.filter(n => n.startsWith('prop_') && !VOCAB.test(n)); const steamEmpties = names.filter(n => n.startsWith('steam_spot_'));
  const catKids = room === 'den' ? (() => { const cat = nodes.find(n => n.name === 'pet_cat'); const kids = (cat.children || []).map(i => nodes[i].name);
    return ['pet_cat_body', 'pet_cat_head', 'pet_cat_tail', 'pet_cat_tail_tip_white', 'pet_cat_ear_left', 'pet_cat_ear_right', 'pet_cat_ear_inner_left', 'pet_cat_ear_inner_right', ...POSES.map(p => 'pet_cat_pose_' + p)].filter(n => !kids.includes(n)); })() : [];
  console.log('  retired names still present:', retired.join(', ') || 'none', '| props outside app vocabulary:', strayProps.join(', ') || 'none', '| steam_spot_* empties:', steamEmpties.length, room === 'den' ? '| not direct children of pet_cat: ' + (catKids.join(', ') || 'none') : '');
  if (missing.length || dupLife.length || retired.length || strayProps.length || steamEmpties.length || catKids.length) fail = 1;
}
process.exit(fail);
