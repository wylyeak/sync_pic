// Compatibility fix for affected ws-scrcpy-web images.
// Keep bytes buffered between metadata parsing and frame-reader attachment.
// Without pause(), a flowing socket can discard the beginning of the video.
const fs = require('node:fs');
const Module = require('node:module');
const originalLoader = Module._extensions['.js'];
const replacements = [
  [
    's.length>=t&&(e.removeListener("data",i),e.removeListener("error",a),s.length>t&&e.unshift(s.subarray(t)),n(s.subarray(0,t)))',
    's.length>=t&&(e.pause(),e.removeListener("data",i),e.removeListener("error",a),s.length>t&&e.unshift(s.subarray(t)),n(s.subarray(0,t)))',
  ],
  [
    'e.on("data",i),e.once("error",a)})}static PTS_FLAG_CONFIG',
    'e.on("data",i),e.once("error",a),e.resume()})}static PTS_FLAG_CONFIG',
  ],
  [
    'DEVICE_MSG,e)})}logCongestion',
    'DEVICE_MSG,e)}),this.videoSocket.resume(),this.audioSocket.resume()}logCongestion',
  ],
];
Module._extensions['.js'] = function (mod, filename) {
  if (filename !== '/app/dist/index.js') return originalLoader(mod, filename);
  let source = fs.readFileSync(filename, 'utf8');
  if (!replacements.every(([before]) => source.split(before).length === 2)) {
    console.warn('[pixel] upstream code changed; skipping local stream fix. Verify video after upgrading.');
    return originalLoader(mod, filename);
  }
  for (const [before, after] of replacements) {
    source = source.replace(before, after);
  }
  console.log('[pixel] applied video metadata buffering fix');
  mod._compile(source, filename);
};
