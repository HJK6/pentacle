'use strict';
const { createHash } = require('node:crypto');

const isModelPath = p => p === '/api/models' || p === '/model-media' || p.startsWith('/model-media/');
const MEDIA = /^\/model-media\/([a-z0-9][a-z0-9-]{0,79})\/([a-z0-9][a-z0-9-]{0,79})\/([a-f0-9]{64})\/(picture|mesh)(?:\/([a-z0-9][a-z0-9-]{0,79}))?$/;

function createModelRoutes(root, canReadForge) {
  const library = root ? require('../lib/model_library.cjs').createLibrary(root) : null;
  return (req,res,url,viewer) => {
    res.setHeader('cache-control','private, no-store');
    const missing = () => res.writeHead(404).end('not found');
    if (!canReadForge(viewer) || !library) return missing();
    if (!['GET','HEAD'].includes(req.method)) return res.writeHead(405,{allow:'GET, HEAD'}).end();
    let body, digest, type;
    let rangeAllowed = false;
    try {
      if (url === '/api/models') {
        body = Buffer.from(JSON.stringify(library.catalog()));
        digest = createHash('sha256').update(body).digest('hex'); type = 'application/json; charset=utf-8';
      } else {
        const m = MEDIA.exec(url);
        if (!m || (m[4] === 'mesh' && !m[5])) return missing();
        const media = library.media(...m.slice(1));
        if (!media) return missing();
        ({bytes:body,digest} = media);
        type = m[4] === 'mesh' ? 'model/gltf-binary' : 'image/png';
        rangeAllowed = true;
      }
    } catch { return missing(); }
    const etag = '"' + digest + '"';
    const headers = {etag, 'content-type':type, 'x-content-type-options':'nosniff'};
    if (rangeAllowed) headers['accept-ranges'] = 'bytes';
    if (req.headers['if-none-match']?.split(',').some(s => s.trim() === '*' || s.trim().replace(/^W\//,'') === etag)) {
      return res.writeHead(304,headers).end();
    }
    let code = 200;
    if (rangeAllowed && req.method === 'GET' && req.headers.range && (!req.headers['if-range'] || req.headers['if-range'] === etag)) {
      const length = body.length;
      const m = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range);
      const start = m?.[1] ? Number(m[1]) : Math.max(0,length-Number(m?.[2]));
      const end = m?.[1] && m?.[2] ? Math.min(length-1,Number(m[2])) : length-1;
      if (!m || (!m[1] && !m[2]) || !(start <= end)) {
        return res.writeHead(416,{...headers,'content-range':'bytes */'+length}).end();
      }
      headers['content-range'] = 'bytes '+start+'-'+end+'/'+length;
      body = body.subarray(start,end+1); code = 206;
    }
    headers['content-length'] = body.length;
    return res.writeHead(code,headers).end(req.method === 'HEAD' ? undefined : body);
  };
}

module.exports = { isModelPath, createModelRoutes };
