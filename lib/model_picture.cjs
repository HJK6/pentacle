'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const { launchOptions } = require('./design_record.cjs');

const renderer = path.join(__dirname,'../renderer');
const viewerPath = path.join(renderer,'dist/web/next/model-viewer.bundle.js');
function themeWells() {
  const css=fs.readFileSync(path.join(renderer,'next/style.css'),'utf8');
  return ['dark','light','cosmic'].map(theme=>{
    const found=new RegExp('--forge-well-'+theme+':\\s*#([a-f0-9]{6})\\s*;','i').exec(css);
    if (!found) throw new Error('Missing Forge picture-well theme token');
    return Array.from(Buffer.from(found[1],'hex'));
  });
}
const startChromium = options => chromium.launch(options);
async function openPicturePage({ launch = (options,start) => start(options) } = {}) {
  if (!fs.existsSync(viewerPath)) throw new Error('Build the local viewer first with npm run build:web');
  let browser;
  try { browser=await launch(launchOptions(),startChromium); }
  catch { throw new Error('Could not launch sandboxed Chrome for model pictures; check the Design recorder Chrome configuration'); }
  try {
    const context=await browser.newContext({offline:true,serviceWorkers:'block'});
    await context.route('**/*',route=>route.abort());
    const page=await context.newPage();
    await page.addScriptTag({path:viewerPath});
    return {browser,page};
  } catch (error) { await browser.close(); throw error; }
}
async function capturePictures(checked,sheet,options) {
  const {browser,page}=await openPicturePage(options);
  try {
    const pngs=[];
    for (const {bytes,view} of checked) {
      try {
        const png = await page.evaluate(async ({base64,view,up,wells})=>{
          const bytes=Uint8Array.from(atob(base64),c=>c.charCodeAt(0));
          const result=await window.PentacleModelViewer.makeModelPicture({bytes,view,up},wells);
          return result.png;
        },{base64:bytes.toString('base64'),view,up:sheet.up,wells:themeWells()});
        pngs.push(Buffer.from(png.split(',')[1],'base64'));
      } catch (error) {
        throw new Error('Cannot capture view '+view.id+': '+error.message.split('\n')[0]);
      }
    }
    return pngs;
  } finally { await browser.close(); }
}

module.exports = {capturePictures,openPicturePage,themeWells};
