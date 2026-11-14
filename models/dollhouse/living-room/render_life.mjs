// Renders the cat-pose close-ups (one image per pose) and assembles renders/cat_poses_sheet.png
import puppeteer from 'puppeteer-core';
import { execSync } from 'child_process';
const poses = ['default', 'sleep', 'sit', 'stretch', 'walk_1', 'walk_2'];
const browser = await puppeteer.launch({ executablePath: '/usr/bin/google-chrome', headless: 'new', args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist', '--no-sandbox'] });
const page = await browser.newPage(); await page.setViewport({ width: 800, height: 600 });
for (const c of poses) {
  await page.goto(`http://localhost:${process.env.PORT || 8766}/src/render.html?state=evening&w=800&h=600&cat=${c}`);
  await page.waitForFunction('window.__done || window.__err', { timeout: 120000 });
  const err = await page.evaluate('window.__err'); if (err) { console.log('ERR', c, err); continue; }
  await page.screenshot({ path: `renders/cat_${c}.png` }); console.log('wrote renders/cat_' + c + '.png');
}
await browser.close();
execSync('python3 make_cat_sheet.py'); console.log('wrote renders/cat_poses_sheet.png');
