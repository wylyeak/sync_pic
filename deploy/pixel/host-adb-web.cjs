// Host networking is required for ADB's loopback TCP forward endpoints.
// Keep every web/probe listener for port 8001 restricted to IPv4 loopback.
const net = require('node:net');
const listen = net.Server.prototype.listen;
net.Server.prototype.listen = function (...args) {
  const first = args[0];
  if (first && typeof first === 'object' && Number(first.port) === Number(process.env.WS_SCRCPY_WEB_PORT || 8001)) {
    args[0] = { ...first, host: '127.0.0.1' };
  } else if (Number(first) === Number(process.env.WS_SCRCPY_WEB_PORT || 8001)) {
    if (typeof args[1] === 'string') args[1] = '127.0.0.1';
    else args.splice(1, 0, '127.0.0.1');
  }
  return listen.apply(this, args);
};
