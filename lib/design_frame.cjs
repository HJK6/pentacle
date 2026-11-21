'use strict';

// A live Design frame opts in to forwarding these navigation keys. Other
// embeddings of prepared documents keep their own keyboard behavior.
const DESIGN_FRAME_NAME = 'pentacle-design-live';
const DESIGN_FRAME_KEYS = Object.freeze(['Escape', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown']);

module.exports = { DESIGN_FRAME_NAME, DESIGN_FRAME_KEYS };
