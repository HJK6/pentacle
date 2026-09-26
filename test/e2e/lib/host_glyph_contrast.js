'use strict';
// Browser acceptance for the actual stylesheet cascade and SVG currentColor.
async function hostGlyphContrast(session) {
 const originalTheme=await session.eval('document.documentElement.dataset.theme');
 const results=[];
 const realLabels=await session.eval(`(()=>{const els=[...document.querySelectorAll('.source-filter-bar .source-filter-btn[class*="color-"], #session-list .s-machine-avatar')];return els.length>0&&els.every(e=>e.title&&e.getAttribute('aria-label')===e.title&&e.querySelector('.machine-sigil[aria-hidden="true"]'));})()`);
 try {
  await session.eval(`(()=>{const panel=document.createElement('div');panel.id='host-glyph-proof';panel.style.cssText='position:fixed;right:12px;bottom:12px;width:240px;z-index:99999;background:var(--pc-side)';for(const [host,color,kind] of [['Thoth','yellow','ibis'],['Amaterasu','red','sun'],['Merlin','royal-blue','mage'],['Orange','orange','flower']]){const row=document.createElement('div');row.className='session-item';row.dataset.proofHost=host;const avatar=document.createElement('span');avatar.className='s-machine-avatar color-'+color;avatar.title=host;const svg=window.PentacleCosmic.machineSigil(kind,{size:15,color:'currentColor'});svg.classList.add('machine-sigil');svg.setAttribute('aria-hidden','true');avatar.append(svg);row.append(avatar,document.createTextNode(host));const button=document.createElement('button');button.className='source-filter-btn color-'+color;button.title=host;button.append(svg.cloneNode(true));row.append(button);panel.append(row);}document.body.append(panel);})()`);
  for(const theme of ['dark','light']) {
   await session.eval(`document.querySelector('[data-setting="theme"] [data-value="${theme}"]').click()`);
   for(const state of ['normal','selected','hover']) for(const host of ['Thoth','Amaterasu','Merlin','Orange']) for(const type of ['avatar','filter']) {
    await session.send('Input.dispatchMouseEvent',{type:'mouseMoved',x:400,y:10});
    const rect=await session.eval(`(()=>{const row=document.querySelector('[data-proof-host="${host}"]');row.classList.toggle('active',${state==='selected'});const btn=row.querySelector('button');btn.classList.toggle('active',${state==='selected'});const el=row.querySelector('${type==='avatar'?'.s-machine-avatar':'button'}');const r=el.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
    if(state==='hover')await session.send('Input.dispatchMouseEvent',{type:'mouseMoved',...rect});
    // Measure the settled real transition, rather than a wall-clock guess under load.
    await session.eval(`(()=>{const row=document.querySelector('[data-proof-host="${host}"]');const el=row.querySelector('${type==='avatar'?'.s-machine-avatar':'button'}');for(const node of [row,el])getComputedStyle(node).backgroundColor;return Promise.all([row,el].flatMap(node=>node.getAnimations().map(animation=>animation.finished.catch(()=>null))));})()`);
    const observed=await session.eval(`(()=>{const row=document.querySelector('[data-proof-host="${host}"]');const el=row.querySelector('${type==='avatar'?'.s-machine-avatar':'button'}');const style=getComputedStyle(el);const reference=document.createElement('span');reference.style.backgroundColor='var(--pc-chip-bg)';row.append(reference);const neutralBackground=getComputedStyle(reference).backgroundColor;reference.remove();let bg=style.backgroundColor;let parent=el;while(bg==='rgba(0, 0, 0, 0)'&&(parent=parent.parentElement))bg=getComputedStyle(parent).backgroundColor;return {neutralBackground,color:style.color,background:style.backgroundColor,effectiveBackground:bg,hover:el.matches(':hover'),width:el.querySelector('svg').getBoundingClientRect().width,label:el.title,strokes:[...el.querySelectorAll('[stroke]')].map(x=>getComputedStyle(x).stroke)};})()`);
    const luminance=rgb=>{const c=rgb.match(/[\d.]+/g).slice(0,3).map(Number).map(n=>{n/=255;return n<=.04045?n/12.92:((n+.055)/1.055)**2.4});return c[0]*.2126+c[1]*.7152+c[2]*.0722};
    const l1=luminance(observed.color),l2=luminance(observed.effectiveBackground);const contrast=(Math.max(l1,l2)+.05)/(Math.min(l1,l2)+.05);
    results.push({theme,state,host,type,...observed,contrast,pass:realLabels&&contrast>=3&&observed.color!=='rgb(255, 255, 255)'&&observed.background===(type==='filter'&&state!=='normal'?observed.neutralBackground:'rgba(0, 0, 0, 0)')&&(host!=='Orange'||(()=>{const [r,g,b]=observed.color.match(/\d+/g).map(Number);return r>g&&g>b})())&&observed.width===15&&observed.label===host&&observed.strokes.some(c=>c===observed.color)&&observed.strokes.every(c=>c==='none'||c===observed.color)&&(state!=='hover'||observed.hover)});
   }
  }
 } finally {await session.eval(`document.getElementById('host-glyph-proof')?.remove();document.querySelector('[data-setting="theme"] [data-value="${originalTheme||'dark'}"]').click()`);await session.send('Input.dispatchMouseEvent',{type:'mouseMoved',x:400,y:10});}
 return results;
}
module.exports={hostGlyphContrast};
