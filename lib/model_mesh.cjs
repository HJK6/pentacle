'use strict';

const valid = (check, why) => { if (!check) throw new Error(why); };
const index = (i, a) => Number.isInteger(i) && i >= 0 && i < a.length;
const finite = v => typeof v === 'number' && Number.isFinite(v);
const round = n => Math.round(n * 10) / 10;

function chunks(bytes) {
  const data = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  valid(bytes.length >= 28 && data.getUint32(0, true) === 0x46546c67
    && data.getUint32(4, true) === 2 && data.getUint32(8, true) === bytes.length, 'Use a GLB 2.0 file with a complete header');
  const result = [];
  let i = 12;
  while (i + 8 <= bytes.length) {
    const n = data.getUint32(i, true);
    valid(n % 4 === 0 && i + 8 + n <= bytes.length, 'Repair the GLB chunk lengths');
    result.push([data.getUint32(i + 4, true), bytes.subarray(i + 8, i + 8 + n)]);
    i += 8 + n;
  }
  valid(i === bytes.length && result.length === 2 && result[0][0] === 0x4e4f534a && result[1][0] === 0x004e4942, 'Embed one JSON and one binary chunk');
  return [JSON.parse(new TextDecoder('utf-8', { fatal:true }).decode(result[0][1])), result[1][1]];
}

function makeMeshReader({ Matrix4, Vector3, Quaternion, Box3 }) {
  return function readMesh(bytes, triangleLimit = Infinity, measureOnly = false) {
    const [g, bin] = chunks(bytes);
    valid(g && g.asset?.version === '2.0', 'Export glTF 2.0');
    // No URL even in unused material, image, buffer, or extension data.
    const inspect = [g];
    while (inspect.length) {
      const v = inspect.pop();
      if (!v || typeof v !== 'object') continue;
      for (const [k, x] of Object.entries(v)) {
        if (k === 'uri') valid(typeof x === 'string' && x.startsWith('data:'), 'Embed every resource; remove external URIs');
        if (typeof x === 'object' && x) inspect.push(x);
      }
    }
    valid(!g.extensionsRequired?.length, 'Export without required GLB extensions');
    valid(Array.isArray(g.buffers) && g.buffers.length === 1 && g.buffers[0]?.uri === undefined
      && Number.isSafeInteger(g.buffers[0].byteLength) && g.buffers[0].byteLength > 0
      && bin.length >= g.buffers[0].byteLength && bin.length - g.buffers[0].byteLength <= 3, 'Embed a complete binary buffer');
    const binary = new DataView(bin.buffer, bin.byteOffset, bin.byteLength);
    const bufferLength = g.buffers[0].byteLength;
    const { nodes, meshes, scenes, bufferViews, accessors } = g;
    valid([nodes, meshes, scenes, bufferViews, accessors].every(Array.isArray), 'Export a scene with mesh nodes and accessors');
    const scene = g.scene ?? 0;
    valid(index(scene, scenes) && Array.isArray(scenes[scene]?.nodes), 'Select a valid default scene');

    function accessor(id, positions) {
      valid(index(id, accessors) && accessors[id] && typeof accessors[id] === 'object', 'Reference a valid mesh accessor');
      const a = accessors[id];
      const size = { 5121: 1, 5123: 2, 5125: 4, 5126: 4 }[a.componentType];
      const width = positions ? 3 : 1;
      valid(!a.sparse && !a.normalized && a.type === (positions ? 'VEC3' : 'SCALAR')
        && (positions ? a.componentType === 5126 : [5121, 5123, 5125].includes(a.componentType))
        && Number.isSafeInteger(a.count) && a.count > 0 && index(a.bufferView, bufferViews),
      'Export dense float positions and unsigned triangle indices');
      const v = bufferViews[a.bufferView];
      const start = v?.byteOffset ?? 0;
      const offset = a.byteOffset ?? 0;
      const stride = v?.byteStride ?? size * width;
      valid(v?.buffer === 0 && [start, offset, stride, v.byteLength].every(n => Number.isSafeInteger(n) && n >= 0)
        && stride >= size * width && stride % size === 0 && (start + offset) % size === 0
        && start + v.byteLength <= bufferLength
        && offset + (a.count - 1) * stride + size * width <= v.byteLength, 'Repair out-of-bounds or misaligned mesh data');
      return { count: a.count, get(i, j = 0) {
        const at = start + offset + i * stride + j * size;
        return positions ? binary.getFloat32(at,true) : size === 1 ? binary.getUint8(at) : size === 2 ? binary.getUint16(at,true) : binary.getUint32(at,true);
      } };
    }

    const seen = new Set(), work = [], names = new Map(), assembled = new Box3();
    let triangles = 0;
    for (const i of [...scenes[scene].nodes].reverse()) work.push([i, new Matrix4()]);
    const records = [];
    const decoded = new Map();
    while (work.length) {
      const [id, parent] = work.pop();
      valid(index(id, nodes) && nodes[id] && !seen.has(id), 'Use a tree of valid node placements without cycles or shared nodes');
      seen.add(id);
      const node = nodes[id];
      const { translation = [0, 0, 0], scale = [1, 1, 1], rotation = [0, 0, 0, 1], children = [] } = node;
      const vec = (x,n) => Array.isArray(x) && x.length === n && x.every(finite);
      valid(!('skin' in node) && !('weights' in node) && Array.isArray(children), 'Use static mesh nodes without skinning or morphing');
      let world;
      if (node.matrix !== undefined) {
        valid(vec(node.matrix,16) && [3,7,11].every(i => node.matrix[i] === 0) && node.matrix[15] === 1
          && !['translation','scale','rotation'].some(k => k in node), 'Use one finite affine matrix or TRS placement');
        world = new Matrix4().fromArray(node.matrix);
      } else {
        valid(vec(translation,3) && vec(scale,3) && vec(rotation,4)
          && Math.abs(Math.hypot(...rotation) - 1) <= 0.001, 'Use finite translation, scale and a unit rotation');
        world = new Matrix4().compose(new Vector3(...translation), new Quaternion(...rotation), new Vector3(...scale));
      }
      world.premultiply(parent);
      valid(world.elements.every(finite), 'Keep composed node placements finite');
      for (const child of [...children].reverse()) work.push([child, world]);
      if (node.mesh === undefined) continue;
      valid(typeof node.name === 'string' && node.name.trim() && !names.has(node.name), 'Give each mesh node a distinct nonempty name');
      valid(index(node.mesh, meshes), 'Reference a valid mesh');
      if (!decoded.has(node.mesh)) {
        const primitives = meshes[node.mesh]?.primitives;
        valid(Array.isArray(primitives) && primitives.length > 0, 'Export at least one mesh primitive');
        decoded.set(node.mesh, primitives.map(p => {
          valid(p && (p.mode === undefined || p.mode === 4) && !p.targets?.length, 'Export static triangle-list primitives');
          const pos = accessor(p.attributes?.POSITION, true);
          const ix = p.indices === undefined ? null : accessor(p.indices, false);
          const count = ix?.count ?? pos.count;
          valid(count % 3 === 0, 'Use complete triangles');
          return { pos, ix, count };
        }));
      }
      const primitives = decoded.get(node.mesh);
      triangles += primitives.reduce((n,p) => n + p.count / 3, 0);
      valid(Number.isSafeInteger(triangles), 'Reduce the triangle count to a safe integer');
      names.set(node.name, null);
      records.push({ name: node.name, world, primitives });
    }
    valid(triangles > 0, 'Export actual triangle geometry in the default scene');
    if (triangles > triangleLimit) return { triangles, names, extents: null, geometry: [] };
    const geometry = [];
    const a = new Vector3(), b = new Vector3(), c = new Vector3(), ab = new Vector3(), ac = new Vector3();
    for (const record of records) {
      const { name, world, primitives } = record;
      const local = new Box3(); let nondegenerate = false;
      const out = [];
      for (const { pos, ix, count } of primitives) {
        const points = measureOnly ? null : new Float32Array(count * 3);
        for (let i = 0; i < count; i += 3) {
          for (const [j, point] of [a,b,c].entries()) {
            const k = ix ? ix.get(i+j) : i+j;
            valid(k < pos.count, 'Repair a triangle index outside its position accessor');
            point.set(pos.get(k,0),pos.get(k,1),pos.get(k,2));
            valid([point.x,point.y,point.z].every(finite), 'Use finite mesh positions');
            local.expandByPoint(point);
            if (points) point.toArray(points,(i+j)*3);
          }
          ab.subVectors(b,a); ac.subVectors(c,a);
          if (ab.cross(ac).lengthSq() > 0) nondegenerate = true;
          for (const point of [a,b,c]) {
            point.applyMatrix4(world);
            valid([point.x,point.y,point.z].every(finite), 'Keep placed mesh positions finite');
            assembled.expandByPoint(point);
          }
        }
        if (points) out.push(points);
      }
      valid(nondegenerate, 'Replace degenerate-only geometry with a drawable mesh');
      names.set(name, local.getSize(new Vector3()).toArray().sort((x,y)=>y-x));
      if (!measureOnly) geometry.push({ name, matrix: world.toArray(), primitives: out });
    }
    const extents = assembled.getSize(new Vector3()).toArray().map(round);
    valid(extents.every(finite), 'Reduce model dimensions to finite millimetres');
    return { triangles, names, extents, geometry };
  };
}

module.exports = { makeMeshReader, round };
