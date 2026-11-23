'use strict';

// CDP emits non-interlaced 8-bit RGB/RGBA PNG. Decode just that contract so
// the browser gate can assert painted pixels, rather than trusting CSSOM.
const { inflateSync } = require('node:zlib');

function paeth(a, b, c) {
  const p = a + b - c;
  const pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
  return pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
}

function readPng(buf) {
  if (!buf.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10]))) throw new Error('not a PNG screenshot');
  let width, height, channels;
  const data = [];
  for (let i = 8; i + 12 <= buf.length;) {
    const len = buf.readUInt32BE(i);
    const key = buf.toString('ascii',i+4,i+8);
    const body = buf.subarray(i+8,i+8+len);
    if (body.length !== len) throw new Error('partial PNG chunk');
    if (key === 'IHDR') {
      width = body.readUInt32BE(0); height = body.readUInt32BE(4);
      channels = body[9] === 2 ? 3 : body[9] === 6 ? 4 : 0;
      if (!channels || body[8] !== 8 || body[12]) throw new Error('unsupported screenshot PNG encoding');
    }
    if (key === 'IDAT') data.push(body);
    if (key === 'IEND') break;
    i += len + 12;
  }
  if (!width || !height || !data.length) throw new Error('incomplete screenshot PNG');
  const scan = inflateSync(Buffer.concat(data));
  const stride = width * channels;
  if (scan.length !== (stride + 1) * height) throw new Error('incomplete screenshot pixels');
  const output = Buffer.alloc(stride * height);
  for (let y = 0; y < height; y++) {
    const filter = scan[y*(stride+1)];
    if (filter > 4) throw new Error('unknown PNG scan filter');
    for (let x = 0; x < stride; x++) {
      const i = y*stride+x;
      const a = x>=channels ? output[i-channels] : 0, b = y ? output[i-stride] : 0;
      const c = y && x>=channels ? output[i-stride-channels] : 0;
      const predictor = filter === 0 ? 0 : filter === 1 ? a : filter === 2 ? b
        : filter === 3 ? (a+b)>>1 : paeth(a,b,c);
      output[i] = scan[y*(stride+1)+1+x] + predictor;
    }
  }
  return (x,y) => {
    if (x<0 || y<0 || x>=width || y>=height) throw new Error('pixel outside screenshot');
    const start=(y*width+x)*channels;
    return [...output.subarray(start,start+3)];
  };
}

const delta = (a,b) => Math.max(...a.map((x,i)=>Math.abs(x-b[i])));

// The fixture supplies a flat background in the scroller and an overflowing
// area whose native thumb is at minimum size. Inspect the right gutter only.
function thumbSample(pixel, box, gutter, yOffset, trackInset=0) {
  const right = Math.floor(box.right), top = Math.ceil(box.top)+trackInset;
  const x = right - Math.ceil(gutter/2);
  const y = top+yOffset;
  const baseline = pixel(right - gutter - 2,y);
  const color = pixel(x,y), contrast = delta(color,baseline);
  if (contrast < 6) throw new Error('no visible native thumb in screenshot');
  const lit = (px,py) => delta(pixel(px,py),pixel(right-gutter-2,py)) >= contrast*.55;
  let width = 0, length = 0;
  for (let i=right-gutter;i<right;i++) if (lit(i,y)) width++;
  for (let j=top;j<top+52;j++) if (lit(x,j)) length++;
  return {x,y,width,length,color,contrast};
}

module.exports = {readPng, thumbSample, delta};
