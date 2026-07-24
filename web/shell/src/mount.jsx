/*
 * Mounts the unified Hanzo shell onto the studio.hanzo.ai logged-out page.
 *
 * The header, pre-footer CTA and footer are the ONE canonical chrome shipped in
 * `@hanzogui/shell` — consumed as data from the "studio.hanzo.ai" surface, never
 * reimplemented here. `@hanzo/brand` supplies the CSS custom properties the shell
 * themes from. esbuild bundles React + the three marketing components into one
 * self-contained ESM asset (`../marketing/shell.{js,css}`) served at /marketing.
 *
 * Only the three logged-out components are imported, so the signed-in graph
 * (HanzoAppHeader → useTenantAuth → @hanzo/iam) is tree-shaken away.
 */
import { createRoot } from 'react-dom/client'
import { HanzoHeader, HanzoPreFooterCTA, HanzoFooter } from '@hanzogui/shell'
import '@hanzo/brand/styles/variables.css'

const SURFACE = 'studio.hanzo.ai'

// The header's account slot: a same-origin "Sign in" link. `/login` is the OIDC
// entry point — the server's handle_login decides framing-aware connect-card vs
// IdP 302 — so we never build the IAM URL client-side.
const account = (
  <a
    href="/login"
    style={{
      display: 'inline-flex',
      alignItems: 'center',
      height: 34,
      padding: '0 16px',
      borderRadius: 9,
      background: 'var(--hanzo-white, #ffffff)',
      color: '#000',
      fontSize: 14,
      fontWeight: 600,
      textDecoration: 'none',
      whiteSpace: 'nowrap',
    }}
  >
    Sign in
  </a>
)

function mount(id, node) {
  const el = document.getElementById(id)
  if (el) createRoot(el).render(node)
}

mount('hanzo-header', <HanzoHeader surface={SURFACE} account={account} />)
mount(
  'hanzo-footer',
  <>
    <HanzoPreFooterCTA surface={SURFACE} />
    <HanzoFooter currentProductId="studio" />
  </>,
)
