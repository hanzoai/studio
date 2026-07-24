/*
 * Builds the self-contained unified-Hanzo-shell bundle for the studio.hanzo.ai
 * logged-out page. Output (../marketing/shell.{js,css}) is committed and served
 * by the Python app at /marketing. Rebuild after bumping @hanzogui/shell:
 *   pnpm install && pnpm build
 */
import { build } from 'esbuild'

await build({
  entryPoints: ['src/mount.jsx'],
  bundle: true,
  minify: true,
  format: 'esm',
  jsx: 'automatic',
  target: 'es2020',
  // Production React: prunes dev-only warnings and removes bare `process` refs
  // (there is no `process` in the browser).
  define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  outfile: '../marketing/shell.js',
  logLevel: 'info',
})
