'use strict';

// The asset window uses the existing asset renderer and authenticated /cc API.
// Bootstrap the browser shim before asset_popout.js registers its listeners.
require('./web_cc').installWebCc();
require('./asset_popout');
