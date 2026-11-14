// Shared dollhouse kit — style tokens, base textures, geometry helpers, device registry and the
// studio renderer. Matches ../living-room/style.md (the den keeps its own copy so its look is frozen).
import * as THREE from 'three';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { GLTFExporter } from 'three/addons/exporters/GLTFExporter.js';

export const STYLE = {
  background: ['#f1ece4', '#d9d0c3'],
  cut: '#efe3cf', slab: '#e2d3bb', plaster: '#ebe0cc',
  warm: '#ffc77d', warmHot: '#ffe2b0', fire: '#ff8a2a', led: '#7fd3ff',
  brass: '#b8893a', ceramic: '#efe8dc', shade: '#efe2c8',
};

export const rng = (seed) => { let s = seed; return () => (s = (s * 16807) % 2147483647) / 2147483647; };

export function canvasTex(w, h, draw, srgb = true) {
  const c = document.createElement('canvas'); c.width = w; c.height = h;
  draw(c.getContext('2d'), w, h);
  const t = new THREE.CanvasTexture(c);
  if (srgb) t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 4;
  return t;
}

export function makeBaseTextures() {
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
  return { glow, scallop, wash, background, sky };
}

// ---------------------------------------------------------------- build

export const std = (color, rough = 0.7, metal = 0, extra = {}) => new THREE.MeshStandardMaterial({ color, roughness: rough, metalness: metal, ...extra });

// Geometry + device kit bound to one room root.
export function makeKit(root, T) {
  const G = (name, parent = root) => { const g = new THREE.Group(); g.name = name; parent.add(g); return g; };
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
  const cyl = (name, parent, mat, rt, rb, h, x, y, z, seg = 20, open = false) => mesh(name, new THREE.CylinderGeometry(rt, rb, h, seg, 1, open), mat, parent, x, y, z);
  const sphere = (name, parent, mat, r, x, y, z) => mesh(name, new THREE.SphereGeometry(r, 16, 12), mat, parent, x, y, z);
  const anchors = G('device_anchors');
  const devices = {};
  const decalMat = (tex, color, opacity) => new THREE.MeshBasicMaterial({ map: tex, color, transparent: true, opacity, blending: THREE.AdditiveBlending, depthWrite: false, toneMapped: false, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: -2 });
  // orient: 'up' floor/counter, '+z' wall facing +z, '-x' wall facing -x
  const decal = (name, parent, tex, color, opacity, w, h, x, y, z, orient) => {
    const m = new THREE.Mesh(new THREE.PlaneGeometry(w, h), decalMat(tex, color, opacity)); m.name = name;
    if (orient === 'up') m.rotation.x = -Math.PI / 2; else if (orient === '-x') m.rotation.y = -Math.PI / 2;
    m.position.set(x, y, z); m.renderOrder = 5; m.visible = false; parent.add(m); return m;
  };
  const lightMats = () => ({
    bulb: std('#d8d2c8', 0.4, 0, { emissive: new THREE.Color(STYLE.warmHot), emissiveIntensity: 0 }),
    shade: std(STYLE.shade, 0.9, 0, { side: THREE.DoubleSide, emissive: new THREE.Color(STYLE.warm), emissiveIntensity: 0 }),
  });
  const pl = (name, parent, color, x, y, z, dist) => { const l = new THREE.PointLight(color, 0, dist, 2); l.name = name; l.position.set(x, y, z); parent.add(l); return l; };
  const register = (name, cfg) => {
    const d = { name, label: cfg.label, type: cfg.type, room: cfg.room, on: false, emissive: cfg.emissive || [], lights: cfg.lights || [], decals: cfg.decals || [], show: cfg.show || [], custom: cfg.custom, anchor: cfg.anchor, group: cfg.group };
    const a = new THREE.Object3D(); a.name = `anchor_${name}`; a.position.copy(cfg.anchor); a.userData = { device: name, type: cfg.type }; anchors.add(a);
    if (cfg.group) cfg.group.userData = { device: name, type: cfg.type };
    devices[name] = d; return d;
  };
  const setDevice = (name, on) => {
    // on: boolean or a 0..1 dim level (numbers < 1 dim emissives, lights and glow decals)
    const d = devices[name]; if (!d) return; const k = on === true ? 1 : Math.max(0, Math.min(1, +on || 0)); d.on = k > 0; d.level = k;
    d.emissive.forEach(e => { e.mat.emissiveIntensity = e.on * k; });
    d.lights.forEach(l => { l.light.intensity = l.on * k; });
    d.decals.forEach(m => { if (m.userData.baseOpacity === undefined) m.userData.baseOpacity = m.material.opacity; m.material.opacity = m.userData.baseOpacity * (k > 0 ? 0.2 + 0.8 * k : 0); m.visible = k > 0; });
    d.show.forEach(m => { m.visible = k > 0; });
    if (d.custom) d.custom(k > 0, k);
  };
  // stencil portal (exterior is drawn only through glass)
  const portalMat = new THREE.MeshBasicMaterial({ colorWrite: false, depthWrite: false, side: THREE.DoubleSide, stencilWrite: true, stencilRef: 1, stencilFunc: THREE.AlwaysStencilFunc, stencilZPass: THREE.ReplaceStencilOp });
  const portal = (name, parent, geo) => { const p = mesh(name, geo, portalMat, parent, 0, 0, 0, false); p.receiveShadow = false; p.renderOrder = 1; p.userData.noExport = true; return p; };
  const extMat = (color, opts = {}) => new THREE.MeshBasicMaterial({ color, stencilWrite: true, stencilRef: 1, stencilFunc: THREE.EqualStencilFunc, stencilFail: THREE.KeepStencilOp, stencilZFail: THREE.KeepStencilOp, stencilZPass: THREE.KeepStencilOp, ...opts });
  const extMesh = (m) => { m.castShadow = false; m.receiveShadow = false; m.renderOrder = 2; return m; };
  return { G, mesh, B, cyl, sphere, decal, lightMats, pl, register, setDevice, devices, anchors, portal, extMat, extMesh };
}

// Studio renderer shared by every room (identical lighting = comparable states).
export function createApp(container, { build, views, presets, width = 1600, height = 1000, pixelRatio = 1, bloom = true, shadow = { center: [-3, 0, 3], half: 9 } } = {}) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
  renderer.setPixelRatio(pixelRatio); renderer.setSize(width, height);
  renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.toneMappingExposure = 1.0;
  renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  container.appendChild(renderer.domElement);
  const scene = new THREE.Scene();
  const room = build();
  scene.add(room.root);
  scene.background = room.textures.background;
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.environmentIntensity = 0.28;
  const studio = new THREE.Group(); studio.name = 'studio_lighting'; scene.add(studio);
  const hemi = new THREE.HemisphereLight('#fff3e3', '#5b4636', 0.85); hemi.name = 'studio_hemi'; studio.add(hemi);
  const [cx, cy, cz] = shadow.center, hs = shadow.half;
  const sun = new THREE.DirectionalLight('#fff1dd', 1.25); sun.name = 'studio_key'; sun.position.set(cx - 7, 14, cz + 6); sun.target.position.set(cx, cy, cz);
  sun.castShadow = true; sun.shadow.mapSize.set(4096, 4096); Object.assign(sun.shadow.camera, { left: -hs, right: hs, top: hs, bottom: -hs, near: 1, far: 50 });
  sun.shadow.bias = -0.0004; sun.shadow.normalBias = 0.02; sun.shadow.radius = 4;
  studio.add(sun); studio.add(sun.target);
  const camera = new THREE.PerspectiveCamera(30, width / height, 0.05, 150);
  const rt = new THREE.WebGLRenderTarget(width * pixelRatio, height * pixelRatio, { type: THREE.HalfFloatType, samples: 4, stencilBuffer: true });
  const composer = new EffectComposer(renderer, rt);
  composer.addPass(new RenderPass(scene, camera));
  const bloomPass = new UnrealBloomPass(new THREE.Vector2(width, height), 0.38, 0.45, 1.0);
  if (bloom) composer.addPass(bloomPass);
  composer.addPass(new OutputPass());
  const setView = (v) => { const V = typeof v === 'string' ? views[v] : v; camera.position.set(...V.pos); camera.fov = V.fov; camera.updateProjectionMatrix(); camera.lookAt(...V.target); camera.userData.target = new THREE.Vector3(...V.target); room.root.traverse(o => { if (o.userData.interiorOnly) o.visible = !!V.interior; if (o.userData.dollhouseOnly) o.visible = !V.interior; }); };
  const applyPreset = (p) => { const P = typeof p === 'string' ? presets[p] : p; Object.keys(room.devices).forEach(n => room.setDevice(n, P[n] || 0)); };
  const render = () => composer.render();
  const resize = (w, h) => { camera.aspect = w / h; camera.updateProjectionMatrix(); renderer.setSize(w, h); composer.setSize(w, h); };
  const exportGLB = () => new Promise((res, rej) => {
    const hidden = []; room.root.traverse(o => { if (o.userData.noExport && o.visible) { o.visible = false; hidden.push(o); } });
    const restore = () => hidden.forEach(o => { o.visible = true; });
    new GLTFExporter().parse(room.root, (buf) => { restore(); res(buf); }, (e) => { restore(); rej(e); }, { binary: true, onlyVisible: true, maxTextureSize: 1024 });
  });
  setView(Object.keys(views)[0]);
  return { renderer, scene, camera, composer, bloomPass, room, devices: room.devices, setDevice: room.setDevice, setView, applyPreset, render, resize, exportGLB, views, presets };
}
