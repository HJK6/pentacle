// A standalone bundle. No normal Pentacle route imports or loads it.
import {
  AmbientLight, Box3, BufferGeometry, Color, DirectionalLight, DoubleSide, EdgesGeometry, Float32BufferAttribute,
  GridHelper, Group, LineBasicMaterial, LineSegments, Mesh, MeshStandardMaterial,
  Matrix4, OrthographicCamera, Quaternion, Scene, Vector3, WebGLRenderer,
} from 'three';
import { makeMeshReader } from '../../lib/model_mesh.cjs';

const readMesh = makeMeshReader({ Matrix4, Vector3, Quaternion, Box3 });

const rad = Math.PI / 180;
const STROKE = [130, 130, 130];
const STROKE_RADIUS = 2;
const COVERAGE_ALPHA = 32;
const STROKE_TOLERANCE = 3;

export function openModelViewer(canvas, { up, view, bytes, picture = false }) {
  let renderer;
  try { renderer = new WebGLRenderer({ canvas, alpha:true, antialias:true, preserveDrawingBuffer:picture }); }
  catch { throw new Error('Model viewer needs WebGL 2'); }
  try {
    renderer.setPixelRatio(1);
    const width = canvas.width, height = canvas.height, aspect = width / height;
    renderer.setSize(width,height,false);
    renderer.setClearAlpha(0);
    const scene = new Scene();
    const camera = new OrthographicCamera();
    const content = new Group();
    // Convert model coordinates to a Y-up staging space. +X is screen-right
    // at zero turn, and model-front maps to world +Z under both up conventions.
    if (up === 'z') content.rotation.x = -Math.PI/2;
    scene.add(content);
    const { geometry } = readMesh(new Uint8Array(bytes));
    const byName = new Map(geometry.map(m => [m.name,m]));
    const groups = new Map();
    const owned = new Set();
    const make = resource => { owned.add(resource); return resource; };
    const thin = make(new LineBasicMaterial({ color:new Color(0x252a30), transparent:true, opacity:0.36 }));
    const highlight = make(new LineBasicMaterial({color:new Color(0x38bdf8),depthTest:false}));
    for (const part of view.parts) {
      const copies = [];
      const material = make(new MeshStandardMaterial({ color:new Color(part.color), roughness:1, metalness:0, side:DoubleSide }));
      for (const name of part.nodes) {
        const data = byName.get(name);
        const frame = new Group(), placement = new Group();
        placement.matrixAutoUpdate = false;
        placement.matrix.fromArray(data.matrix);
        frame.add(placement); content.add(frame);
        const outlines = [];
        for (const positions of data.primitives) {
          const g = make(new BufferGeometry());
          g.setAttribute('position',new Float32BufferAttribute(positions,3));
          g.computeVertexNormals();
          const mesh = new Mesh(g,material);
          const edges = make(new EdgesGeometry(g,28));
          if (!picture) mesh.add(new LineSegments(edges,thin));
          const outline = new LineSegments(edges,highlight);
          outline.visible = false; outline.renderOrder = 2;
          if (!picture) mesh.add(outline);
          outlines.push(outline);
          placement.add(mesh);
        }
        copies.push({ frame, outlines });
      }
      groups.set(part.id,{part,copies});
    }
    scene.add(new AmbientLight(0xffffff,1.9));
    const sun = new DirectionalLight(0xffffff,2);
    sun.position.set(3,5,4); scene.add(sun);
    const assembled = new Box3().setFromObject(content,true);
    const center = assembled.getCenter(new Vector3());
    const radius = Math.max(assembled.getSize(new Vector3()).length()/2,0.001);
    if (!picture) {
      const grid = new GridHelper(4*radius,16,0x57616a,0x39434b);
      grid.position.set(center.x,assembled.min.y-radius*0.005,center.z);
      scene.add(grid); make(grid);
    }
    const target = center.clone();
    let turn = view.camera?.turn ?? -35, tilt = view.camera?.tilt ?? 25, zoom = 1;
    const focus = new Vector3();
    function render() {
      const t = turn*rad, e = tilt*rad;
      const visibleBounds = new Box3().setFromObject(content,true);
      const span = visibleBounds.getSize(new Vector3()).length();
      camera.position.set(Math.sin(t)*Math.cos(e),Math.sin(e),Math.cos(t)*Math.cos(e))
        .multiplyScalar(Math.max(radius,span/2)*4).add(target);
      camera.lookAt(target);
      const half = radius*1.15*Math.max(1,1/aspect)/zoom;
      camera.left=-half*aspect; camera.right=half*aspect; camera.top=half; camera.bottom=-half;
      const depth = visibleBounds.getCenter(focus).distanceTo(camera.position) + span;
      camera.near = 0; camera.far = Math.max(radius*10,depth*2); camera.updateProjectionMatrix();
      renderer.render(scene,camera);
    }
    function bounds() {
      content.updateMatrixWorld(true);
      camera.updateMatrixWorld(true);
      return [...groups].map(([id,{copies}])=>{
        const s = new Box3();
        for (const {frame} of copies) frame.traverse(obj=>{
          if (!obj.isMesh) return;
          const pos = obj.geometry.getAttribute('position');
          const point = new Vector3();
          for (let i=0;i<pos.count;i++) {
            point.fromBufferAttribute(pos,i).applyMatrix4(obj.matrixWorld).project(camera);
            s.expandByPoint(point);
          }
        });
        return { id, left: (1+s.min.x)*width/2, right:(1+s.max.x)*width/2,
          top:(1-s.max.y)*height/2, bottom:(1-s.min.y)*height/2 };
      });
    }
    const pointer = { active:null };
    const drag = event=>{
      if (event.type === 'pointerdown') {
        pointer.active=event.pointerId; canvas.setPointerCapture(pointer.active);
      } else if (event.pointerId === pointer.active) {
        if (event.type !== 'pointermove') pointer.active=null;
        else if (event.shiftKey || (event.buttons & 2)) slide(event.movementX,event.movementY);
        else orbit(event.movementX/3,event.movementY/3);
      }
    };
    function orbit(dx,dy) { turn+=dx; tilt=Math.max(-89,Math.min(89,tilt+dy)); render(); }
    function slide(dx,dy) {
      target.addScaledVector(new Vector3().setFromMatrixColumn(camera.matrixWorld,0),-dx*(camera.right-camera.left)/width);
      target.addScaledVector(new Vector3().setFromMatrixColumn(camera.matrixWorld,1),dy*(camera.top-camera.bottom)/height);
      render();
    }
    function magnify(factor) { if (Number.isFinite(factor) && factor>0) zoom=Math.min(10,Math.max(0.1,zoom*factor)); render(); }
    const wheel = e=>{ e.preventDefault(); magnify(Math.exp(-e.deltaY*0.002)); };
    const menu = e=>e.preventDefault();
    const drags = ['pointerdown','pointermove','pointerup','pointercancel'];
    if (!picture) {
      drags.forEach(e=>canvas.addEventListener(e,drag));
      canvas.addEventListener('wheel',wheel,{passive:false});
      canvas.addEventListener('contextmenu',menu);
    }
    render();
    return {
      render, orbit, slide, zoom:magnify, projectedPartBoxes:bounds,
      fit() { const box = new Box3().setFromObject(content,true); box.getCenter(target);
        zoom = Math.max(0.00001, radius / Math.max(radius*0.03,box.getSize(new Vector3()).length()/2)); render(); },
      explode(amount) {
        if (!Number.isFinite(amount)) return;
        for (const {part,copies} of groups.values()) for (const {frame} of copies) {
          frame.position.fromArray(part.explode ?? [0,0,0]).multiplyScalar(Math.max(0,Math.min(amount,1)));
        }
        render();
      },
      select(id) {
        for (const [key,{copies}] of groups) for (const {outlines} of copies) for (const o of outlines) o.visible = key===id;
        render();
      },
      dispose() {
        drags.forEach(e=>canvas.removeEventListener(e,drag));
        canvas.removeEventListener('wheel',wheel); canvas.removeEventListener('contextmenu',menu);
        for (const res of owned) res.dispose();
        renderer.dispose(); renderer.forceContextLoss();
      },
    };
  } catch (error) {
    renderer.dispose(); renderer.forceContextLoss();
    throw error;
  }
}

function coverage(raw) {
  return Uint8Array.from({length:raw.length/4},(_,i)=>Number(raw[i*4+3]>=COVERAGE_ALPHA));
}

function boundary(mask,w,h) {
  const expected = new Uint8Array(w*h);
  for (let y=0;y<h;y++) for (let x=0;x<w;x++) {
    const i=y*w+x;
    if (mask[i]) continue;
    for (let dy=-STROKE_RADIUS;dy<=STROKE_RADIUS;dy++) for (let dx=-STROKE_RADIUS;dx<=STROKE_RADIUS;dx++) {
      if (dx*dx+dy*dy<=STROKE_RADIUS**2 && x+dx>=0 && x+dx<w && y+dy>=0 && y+dy<h && mask[(y+dy)*w+x+dx]) {
        expected[i]=1;
      }
    }
  }
  return expected;
}

const luminance = rgb=>rgb.reduce((sum,v,i)=>{
  const c=v/255;
  return sum+[0.2126,0.7152,0.0722][i]*(c<=0.04045 ? c/12.92 : ((c+0.055)/1.055)**2.4);
},0);

export function assessPicture(raw, captured, w, h, wells) {
  const mask=coverage(raw), ring=boundary(mask,w,h);
  const expected=ring.reduce((a,b)=>a+b,0);
  if (!expected) throw new Error('Model picture is blank or lacks a visible exterior');
  const passed=wells.map(()=>0);
  let matched=0;
  for (let i=0;i<ring.length;i++) if (ring[i]) {
    const [r,g,b,a]=captured.subarray(i*4,i*4+4);
    if (a && [r,g,b].every((v,j)=>Math.abs(v-STROKE[j])<=STROKE_TOLERANCE)) {
      matched++;
      wells.forEach((well,j)=>{
        const foreground=luminance([r,g,b].map((v,k)=>v*a/255+well[k]*(1-a/255)));
        const background=luminance(well);
        if ((Math.max(foreground,background)+0.05)/(Math.min(foreground,background)+0.05)>=1.5) passed[j]++;
      });
    }
  }
  if (matched!==expected || passed.some(n=>n/expected<0.9)) throw new Error('Model picture needs the complete two-pixel exterior silhouette and contrast on all wells');
  return {expected,matched,contrast:passed.map(n=>n/expected)};
}

export async function makeModelPicture({bytes,view,up}, wells) {
  const w=448,h=280;
  const canvas = Object.assign(document.createElement('canvas'),{width:w,height:h});
  const stage = Object.assign(document.createElement('canvas'),{width:w,height:h});
  const ctx=stage.getContext('2d',{willReadFrequently:true});
  const renderer=openModelViewer(canvas,{bytes,view,up,picture:true});
  try {
    ctx.drawImage(canvas,0,0);
    const raw=ctx.getImageData(0,0,w,h);
    const ring=boundary(coverage(raw.data),w,h);
    const stroked=new ImageData(new Uint8ClampedArray(raw.data),w,h);
    for (let i=0;i<ring.length;i++) if (ring[i]) stroked.data.set([...STROKE,255],i*4);
    ctx.putImageData(stroked,0,0);
    const png=stage.toDataURL('image/png');
    const image=new Image();
    image.src=png; await image.decode();
    ctx.clearRect(0,0,w,h); ctx.drawImage(image,0,0);
    const measured=assessPicture(raw.data,ctx.getImageData(0,0,w,h).data,w,h,wells);
    return {png, measured, mask:raw.data};
  } finally { renderer.dispose(); }
}
