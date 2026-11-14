# living-room — life build

Self-contained copy of the room builder with the life-scene nodes (see ../LIFE_NOTE.md).

```bash
npm install            # three@0.170 + puppeteer-core (Chrome at /usr/bin/google-chrome)
npm run serve &        # serves THIS folder as the web root on :8766
node render.mjs export # -> GLBs + devices.json (devices.json has a "life" section)
node render.mjs evening:dollhouse  # state:view[:w:h] -> renders/<view>_<state>.png
node build_viewer.mjs  # -> viewer.html (standalone, three from unpkg)
npm run check          # lists life node names inside the GLBs (no deps)
npm run smoke          # viewer + GLTFLoader reload check
```

`node render_life.mjs` renders each cat pose (`src/render.html?cat=<pose>`) and builds `renders/cat_poses_sheet.png`.

Files: `src/` builders (life.js, life_viewer.js, render.html, scene.js).
