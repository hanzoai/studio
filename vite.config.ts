import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { hanzo } from '@hanzo/vite'
import react from '@vitejs/plugin-react'

/**
 * studio.hanzo.ai, built: Studio mounted whole (src/app). The runtime
 * resolution every Hanzo app needs comes from `hanzo()`; what is left is this
 * site's own.
 *
 * On a laptop the page is same-origin with what it calls: /v1 goes to
 * api.hanzo.ai and /engine to the studio engine (STUDIO_ENGINE, default
 * https://studio.hanzo.ai), each with its own Origin rewritten, so the built
 * page is exercised against real data. The dev server runs on :3000, the
 * address the `hanzo-studio` IAM client accepts a callback at.
 */
const API = 'https://api.hanzo.ai'
const ENGINE = process.env.STUDIO_ENGINE ?? 'https://studio.hanzo.ai'

const proxy = {
  '/v1': { target: API, changeOrigin: true },
  '/engine': { target: ENGINE, changeOrigin: true, rewrite: (p: string) => p.replace(/^\/engine/, '') },
}

/**
 * Every address that is not a file arrives at the one document: a static
 * plane answers a miss with the site's 404.html, so the document ships under
 * that name too. The notices ship beside it.
 */
const site = {
  name: 'studio-site',
  async writeBundle(options: { dir?: string }) {
    const { copyFile, writeFile } = await import('node:fs/promises')
    const { join } = await import('node:path')
    const dir = options.dir ?? 'dist'
    await copyFile(join(dir, 'index.html'), join(dir, '404.html'))
    const css = createRequire(import.meta.url).resolve('@hanzo/font/css')
    const ofl = css.slice(0, css.lastIndexOf('/dist/')) + '/LICENSE.txt'
    await writeFile(join(dir, 'LICENSE.txt'), readFileSync(new URL('./LICENSE', import.meta.url)))
    await writeFile(join(dir, 'NOTICE.txt'), readFileSync(new URL('./NOTICE', import.meta.url)))
    await writeFile(join(dir, 'OFL.txt'), readFileSync(ofl))
  },
}

const config = hanzo(
  {
    plugins: [react(), site],
    server: { port: 3000, strictPort: true, allowedHosts: true, proxy },
    preview: { port: 3000, strictPort: true, allowedHosts: true, proxy },
    build: { sourcemap: false, chunkSizeWarningLimit: 4096 },
  },
  {
    root: import.meta.dirname,
    dedupe: ['@hanzogui/core', '@hanzogui/web', '@hanzogui/portal', '@hanzogui/toast'],
  },
)

export default {
  ...config,
  optimizeDeps: {
    rolldownOptions: {
      resolve: { extensions: (config.resolve as { extensions: string[] }).extensions },
    },
  },
}
