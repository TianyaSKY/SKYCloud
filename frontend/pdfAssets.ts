import { readFileSync, readdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { createRequire } from 'node:module'
import type { Plugin } from 'vite'

/** Ship PDF fonts/decoders with the pinned parser; never fetch a public CDN. */
export function pdfAssets(): Plugin {
  const require = createRequire(import.meta.url)
  const root = dirname(require.resolve('pdfjs-dist/package.json'))
  const files = new Map<string, Buffer>()
  for (const directory of ['cmaps', 'standard_fonts', 'wasm', 'iccs']) {
    for (const name of readdirSync(join(root, directory))) {
      if (/\.(bcmap|pfb|ttf|wasm|icc|js)$/.test(name)) {
        files.set(`/pdfjs/${directory}/${name}`, readFileSync(join(root, directory, name)))
      }
    }
  }
  return {
    name: 'local-pdf-assets',
    generateBundle() {
      for (const [path, source] of files) this.emitFile({ type: 'asset', fileName: path.slice(1), source })
    },
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        const path = request.url?.split('?')[0] || ''
        const bytes = files.get(path)
        if (!bytes) return next()
        response.setHeader('Content-Type', path.endsWith('.wasm') ? 'application/wasm' :
          path.endsWith('.js') ? 'application/javascript' : 'application/octet-stream')
        response.end(bytes)
      })
    },
  }
}
