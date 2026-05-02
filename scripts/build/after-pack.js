/**
 * electron-builder afterPack hook.
 * Makes the bundled bridge binary executable after packing.
 */
const path = require('path');
const fs   = require('fs');

exports.default = async function afterPack({ appOutDir, packager }) {
  const bridgeBin = path.join(appOutDir, 'resources', 'bridge');
  if (fs.existsSync(bridgeBin)) {
    fs.chmodSync(bridgeBin, 0o755);
    console.log('afterPack: bridge binary marked executable');
  } else {
    console.warn('afterPack: bridge binary not found at', bridgeBin);
  }
};
