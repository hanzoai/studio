/*
 * Mounts the unified Hanzo shell onto the studio.hanzo.ai logged-out page.
 *
 * The header, pre-footer CTA and footer are the ONE canonical chrome shipped in
 * `@hanzogui/shell` — consumed as data from the "studio.hanzo.ai" surface, never
 * reimplemented here. esbuild bundles React + the three marketing components into
 * one self-contained ESM asset (`../marketing/shell.{js,css}`) served at /marketing.
 *
 * Only the three logged-out components are imported, so the signed-in graph
 * (HanzoAppHeader → useTenantAuth → @hanzo/iam) is tree-shaken away.
 *
 * TOKENS. `@hanzo/design` is the authority and is imported LAST so it wins the
 * names both packages declare; `@hanzo/brand` stays for the `--hanzo-*` identity
 * values design deliberately does not define. Of the 44 colliding names only two
 * (`--hanzo-black`, `--hanzo-white`) are referenced by this bundle and they agree
 * on value modulo hex case, so the order is a no-op for the chrome — it is set
 * deliberately so the layering is stated, not left to whichever loads last.
 *
 * Four token files, one reference each: `colors` + `radius` paint login.html,
 * `typography` resolves the shell's `--font-size-*` and `z` its `--z-*`. Without
 * them those 11 fall back to a Tailwind scale (12/14/16/18/20/24px) that renders
 * studio's chrome 1-3px LARGER than every surface loading brand 1.4.4 or design
 * (both 11/13/14/15/17/21px) — this converges it. `base.css` is NOT imported (its
 * element rules would override login.html's own reset, Basel stack and link
 * colours) and neither is `fonts.css` (it self-hosts Geist via url(), and this
 * GPL-3 repo deliberately ships no font).
 */
import { createRoot } from 'react-dom/client'
import { HanzoHeader, HanzoPreFooterCTA, HanzoFooter } from '@hanzogui/shell'
import '@hanzo/brand/styles/variables.css'
import '@hanzo/design/tokens/colors.css'
import '@hanzo/design/tokens/typography.css'
import '@hanzo/design/tokens/radius.css'
import '@hanzo/design/tokens/z.css'

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
