// Capture promo screenshots of the LIVE site into this folder.
// Run with the deck's puppeteer:  cd ../../podsearch-deck && node ../podsearch-public/screenshots/capture-live.js
const puppeteer = require('/Users/ankitaggarwal/Codes/PodcastSearch/podsearch-deck/node_modules/puppeteer');
const path = require('path');
const OUT = '/Users/ankitaggarwal/Codes/PodcastSearch/podsearch-public/screenshots';
const W = 1440, H = 900;
const BASE = 'https://podsearch.space';

const shots = [
  { file: '01-homepage.png',  url: BASE + '/',               wait: 2500 },
  { file: '02-pipeline.png',  url: BASE + '/#/pipeline',      wait: 2500 },
  { file: '03-library.png',   url: BASE + '/#/library',       wait: 2800 },
  { file: '04-search.png',    url: BASE + '/#/search?q=' + encodeURIComponent('what makes a great product manager?'), wait: 16000 },
];

(async () => {
  const browser = await puppeteer.launch({ headless: 'new', args: ['--no-sandbox'] });
  const page = await browser.newPage();
  await page.setViewport({ width: W, height: H, deviceScaleFactor: 2 });
  for (const s of shots) {
    await page.goto(s.url, { waitUntil: 'networkidle2', timeout: 30000 }).catch(() => {});
    await page.evaluate(() => document.fonts && document.fonts.ready);
    await new Promise(r => setTimeout(r, s.wait));
    await page.screenshot({ path: path.join(OUT, s.file) });
    process.stdout.write('captured ' + s.file + '  ');
  }
  // GitHub repo page
  await page.goto('https://github.com/ankitaggarwal/podsearch.space', { waitUntil: 'networkidle2', timeout: 30000 }).catch(() => {});
  await new Promise(r => setTimeout(r, 2500));
  await page.screenshot({ path: path.join(OUT, '05-github.png') });
  console.log('captured 05-github.png\nDone → ' + OUT);
  await browser.close();
})();
