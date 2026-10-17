# Hanzo Studio (@hanzo/studio)

A chat that makes things, with every result open in a tab beside it: images,
posters, documents, slides, type systems, 3D, short video, ads, voice and music.
One React component, two hosts:

- **hanzo.ai/studio**: the Hanzo app mounts `<Studio host={…} rail={false}>` and
  draws `nav(host, go)` in its own column (the way it mounts @hanzo/build's
  Builder for Dev). `host` is @hanzo/build's `Host`.
- **studio.hanzo.ai**: `src/app` mounts it whole, with its own rail, as the static
  site `hanzo-studio`.

BSD-3-Clause. Nothing here derives from the engine (hanzoai/studio-v1, GPL-3.0),
the ComfyUI frontend or LiteGraph; it talks to the engine over HTTP only. This
repository is a fresh root: the private prototype it grew from (hanzo-inc/studio,
archived) held a deleted GPL-derived scaffold in its history, which is why that
history was not carried over.

## Shape

    src/index.ts        Studio, nav, NEW, route/path: the package's whole surface
    src/studio.tsx      the panes by address, and Studio's own rail (rail=true)
    src/talk.tsx        a conversation: @hanzo/ui `Chat` + @hanzo/build `Panel`/`useDeck`
    src/desk.tsx        one tab: versions, compare (wipe for pictures), export
    src/home.tsx        under an empty composer: category pills, starters, Recent
    src/places.tsx      Library, Explore, Activity
    src/chat/turn.ts    one turn: tools to the brain, kinds run, tabs open
    src/chat/store.ts   conversations as stored, and their index
    src/kinds/*         one file (or folder) per kind; kind.ts is the contract
    src/wire.ts         THE ONE CLIENT MODULE: gateway, store, engine
    src/live.ts         shared readers: the wire from the host, GPUs + makers, chats
    src/app/*           studio.hanzo.ai: sign-in through hanzo.ai, the History API

Addresses (`route.ts`): `''` new, `c/<id>`, `library`, `explore`, `activity`. None
may be a word the engine answers on studio.hanzo.ai (/templates, /settings,
/models, /history, /view, /upload, /prompt, /login, /callback, /logout, /v1, /ws).

## A kind

`kinds/kind.ts`: `tool` (what the chat calls), `needs(available)` (why it cannot
run now, or null), `make`, `revise` (an instruction, or a patch from an edit in
place), `View` (the tab), `exports`. A kind never touches the network: it is
handed a `Work` (`keep` a file into this version, `picture` a stored file as a
data: URL, `find` another tab, `brand`, `fleet`, `models`, `writer`).

| kind | runs on | file |
|---|---|---|
| image | gateway image models (chat completions, `modalities: [image, text]`), or Zen Image on a GPU (`image.zen`) | kinds/image.tsx |
| poster | image lane + layout in the browser (SVG with faces and picture inline → PNG; PDF at the print size from the same SVG) | kinds/poster |
| document, slides | the writer writes the engine's IR; pictures drawn and kept; the engine renders PDF + DOCX/PPTX, kept here | kinds/document.tsx |
| type | the writer, constrained to OFL faces; specimen; CSS / tokens / gui theme | kinds/type |
| mesh (3D) | engine /v1/library/model3d on a GPU advertising `mesh`; three.js viewer | kinds/mesh |
| video | gateway /v1/videos when the catalog serves a video model, else Wan on a GPU (`video.wan`) | kinds/video.tsx |
| ad | the poster composer at the social set and the IAB banners; a banner layout for 728x90 / 320x50 / 160x600; ZIP | kinds/ad.tsx |
| voice | /v1/audio/speech, zen-voice-mini | kinds/sound.tsx |
| music | ACE-Step on a GPU (`music.ace`) | kinds/sound.tsx |

A GPU lane name is what `/v1/gpu-status` `nodes[].lanes` advertises (`lit`);
the home shows a starter only when its kind's `needs` is null.

## Models (live.ts)

`BRAIN = zen5` calls the tools; `WRITER = zen5-flash` answers a strict JSON schema.
Measured 2026-10-16: zen5 calls tools in ~3 s, zen5-flash answers a schema in ~2 s;
zen5 spent 30 s reasoning over a schema and zen5-flash answered tools in prose.
That day most premium models (enso, gemini/gpt image, claude) answered 503
`providers_exhausted` (the upstream was out of credit), which is why the image
lane falls back to type-only posters with the reason said in the turn.

## Storage (behind wire.ts)

A person's work is theirs alone until they share it, so it is never written
where the org can list it (an org bucket on /v1/s3 is readable by every member:
that is why an earlier interim on the workspace bucket was removed).

- Today: IndexedDB in this browser, one database per person and org
  (`hanzo-studio:<org>:<sub>`), keys `chats/<id>.json`, `chats/index.json`,
  `files/<kind>/<chat>/<made>/<version>/<name>`. The UI says "Kept in this
  browser · Saving to Library when it's available".
- When `GET api.hanzo.ai/v1/library` answers, every file is also saved there
  (`POST /v1/library` {name, mime, data, source: studio, kind, ref: <chat>,
  origin: /studio/c/<chat>, key: <the key>}, idempotent by key), read back by
  signed URL, and `sync` carries up what was kept only here.
- When `/v1/studio/*` answers (scratchpad studio2/contract/studio.yaml), the turn
  moves to `POST /v1/studio/chat` and a kind's `make` to `POST /v1/studio/runs`;
  conversations move to `/v1/studio/threads`. Nothing outside wire.ts and chat/
  changes.

Documents carry their pictures inside the IR as data: URLs: no address of a
person's file is handed to the engine.

## Auth

Embedded: `host.token()`. Standalone (`src/app/signin.ts`): a signed-out person
goes to `https://hanzo.ai/login` with the `hanzo-studio` authorize request (PKCE)
and `next`; the code comes back to `/auth/callback` and is exchanged at
`api.hanzo.ai/v1/iam/oauth/token`; the bearer is held in memory only. There is no
login screen here. The engine is reached cross-origin from hanzo.ai with the
bearer and `X-Org-Id`, never a cookie.

## Run, test, ship

    pnpm install
    pnpm typecheck && pnpm test      # vitest: routes, type systems, PDF, ZIP, keys
    pnpm build && pnpm preview       # the site on :3000 (the IAM client's dev callback)
    pnpm build:lib                   # lib/, what npm ships

`vite dev` cannot optimize react-native-svg's web build under Vite 8; use
`build` + `preview`. react-native-svg is held at 15.15.5 (pnpm-workspace.yaml):
15.16 reaches react-native's TurboModuleRegistry from its web entry.

Ship: a push to main runs hanzo.yml (tests, the site `hanzo-studio`) and
publish.yml (npm, when package.json names a version npm does not serve).
