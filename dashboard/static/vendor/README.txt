heic-to 1.6.7, upstream unmodified CSP ES module build.
Source: https://github.com/hoppergee/heic-to
Package: https://registry.npmjs.org/heic-to/-/heic-to-1.6.7.tgz
Artifact: package/dist/csp/heic-to.js
SHA256: e0a515585872c319c601c33551925c9d5fee31746caf0311291ed7e6364c592c
License: LGPL-3.0; see heic-to-LICENSE.txt and GPL-3.txt.
Includes libheif 1.23.7 (LGPL-3.0) and libde265 1.0.16 (LGPL-3.0).
Corresponding source and build instructions:
https://github.com/hoppergee/heic-to
https://github.com/strukturag/libheif/tree/v1.23.7
https://github.com/strukturag/libde265/tree/v1.0.16

The library is loaded from this application only if native browser image decoding
fails for a HEIC image. HEIC decoding runs in its browser Web Worker; no external
CDN or photo upload to third parties. Replace the standalone ES module with a
compatible heicTo export to use a modified build. Server only streams originals.
