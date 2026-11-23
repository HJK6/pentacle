'use strict';
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const zlib = require('node:zlib');

const sheet = {
  schema:'model-drop.v1', project:{id:'bench',title:'Bench fixtures'}, id:'spacer', title:'Test spacer',
  updated:'2025-01-12', summary:'A spacer keeps two test blocks apart.',
  facts:[{label:'Use',value:'Fixture spacing'}], changes:[{label:'Width',from:'8 mm',to:'10 mm'}],
  unverified:['The fit has not been tested.'], units:'mm', up:'z', views:[{
    id:'front',title:'Front',file:'Sample_A.glb',camera:{turn:-35,tilt:25},
    parts:[{id:'spacer',name:'Spacer',role:'print',material:'PLA',color:'#708090',
      about:'The block sets the gap between the fixtures.',nodes:['Spacer A'],explode:[0,0,12]}],
  }],
};
const box = (x=10,y=20,z=30) => [
  [-x/2,-y/2,-z/2], [x/2,-y/2,-z/2], [x/2,y/2,-z/2], [-x/2,y/2,-z/2],
  [-x/2,-y/2,z/2], [x/2,-y/2,z/2], [x/2,y/2,z/2], [-x/2,y/2,z/2],
];
const faces = [0,2,1,0,3,2,4,5,6,4,6,7,0,1,5,0,5,4,1,2,6,1,6,5,2,3,7,2,7,6,3,0,4,3,4,7];
function glb(copies = [{}], edit = () => {}) {
  const nodes=[], meshes=[], accessors=[], bufferViews=[], binaries=[];
  let offset=0;
  const add = (data,type,componentType,count)=>{
    bufferViews.push({buffer:0,byteOffset:offset,byteLength:data.length});
    binaries.push(data); offset+=data.length;
    accessors.push({bufferView:bufferViews.length-1,type,componentType,count});
    return accessors.length-1;
  };
  for (const [i,copy] of copies.entries()) {
    const coords=(copy.points ?? box()).flat();
    const positions=Buffer.alloc(coords.length*4), ix=Buffer.from(faces);
    coords.forEach((x,j)=>positions.writeFloatLE(x,j*4));
    const pos=add(positions,'VEC3',5126,coords.length/3);
    const indices=add(ix,'SCALAR',5121,ix.length);
    meshes.push({primitives:[{attributes:{POSITION:pos},indices}]});
    nodes.push({name:'Spacer '+String.fromCharCode(65+i),mesh:i,...copy.node});
  }
  const g={asset:{version:'2.0'},buffers:[{byteLength:offset}],scene:0,scenes:[{nodes:nodes.map((_,i)=>i)}],
    nodes,meshes,accessors,bufferViews};
  edit(g);
  let json=Buffer.from(JSON.stringify(g));
  json=Buffer.concat([json,Buffer.alloc((4-json.length%4)%4,32)]);
  const bin=Buffer.concat(binaries);
  const chunk=(type,data)=>{
    const result=Buffer.alloc(8);
    result.writeUInt32LE(data.length); result.writeUInt32LE(type,4);
    return Buffer.concat([result,data]);
  };
  const payload=Buffer.concat([chunk(0x4e4f534a,json),chunk(0x004e4942,bin)]);
  const h=Buffer.alloc(12); h.write('glTF');h.writeUInt32LE(2,4);h.writeUInt32LE(payload.length+12,8);
  return Buffer.concat([h,payload]);
}
function drop(dir,{edit=()=>{},mesh=glb()}={}) {
  fs.mkdirSync(dir,{recursive:true});
  const value=structuredClone(sheet);
  edit(value);
  fs.writeFileSync(path.join(dir,'model.json'),JSON.stringify(value));
  if (mesh) fs.writeFileSync(path.join(dir,'Sample_A.glb'),mesh);
  return value;
}
function temp(t) {
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'model-library-'));
  t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  return dir;
}
function png() {
  // Independently encoded valid RGBA fixture. Store tests inject capture;
  // the browser scenario proves silhouette and actual WebGL rendering.
  const width=448,height=280;
  const hdr=Buffer.alloc(13);
  hdr.writeUInt32BE(width);hdr.writeUInt32BE(height,4);hdr[8]=8;hdr[9]=6;
  const rows=Buffer.alloc(height*(1+width*4));
  rows.fill(200,140*(1+width*4)+4*224+1,140*(1+width*4)+4*225+1);
  function chunk(type,data) {
    const body=Buffer.concat([Buffer.from(type),data]);
    let crc=~0;
    for (const b of body) {
      crc^=b; for(let i=0;i<8;i++) crc=(crc>>>1)^((crc&1)?0xedb88320:0);
    }
    const len=Buffer.alloc(4), sum=Buffer.alloc(4);
    len.writeUInt32BE(data.length);sum.writeUInt32BE((~crc)>>>0);
    return Buffer.concat([len,body,sum]);
  }
  return Buffer.concat([Buffer.from('89504e470d0a1a0a','hex'),chunk('IHDR',hdr),chunk('IDAT',zlib.deflateSync(rows)),chunk('IEND',Buffer.alloc(0))]);
}
const fakeCapture = async views=>views.map(png);
module.exports={sheet,box,glb,drop,temp,png,fakeCapture};
