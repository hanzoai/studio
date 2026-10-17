/**
 * A CONVERSATION and its tabs: the chat that makes things on the left, every
 * thing it made in a tab on the right. The same two panes as a Dev run, from
 * the same parts: @hanzo/ui's `Chat` for the thread and its composer (built by
 * @hanzo/build's `prompt`), and @hanzo/build's `Panel` over a `Deck` for the
 * tabs, scoped to this conversation so its tabs are kept with it.
 *
 * A new conversation has no id in the address until its first message is sent;
 * then the address names it, and the conversation in hand is the one shown.
 */
import { Grip, Panel, PanelToggle, prompt, Reply, useDeck, useKey, usePanel, type Host, type Kind as Tab } from '@hanzo/build'
import { SizableText, useMedia, XStack, YStack } from '@hanzo/gui'
import { Check, ChevronDown, Ellipsis, Image as ImageIcon, LoaderCircle, Palette, Sparkles, X } from '@hanzogui/lucide-icons-2'
import { DropdownMenu } from '@hanzo/ui'
import { Chat as Thread, Failure, type Turn as Said } from '@hanzo/ui/chat'
import { useCallback, useEffect, useMemo, useRef, useState, type ComponentProps, type ReactNode } from 'react'

import { edit as editInPlace, turn, type Asked } from './chat/turn.ts'
import { keep, listed, open } from './chat/store.ts'
import { Desk } from './desk.tsx'
import { Home } from './home.tsx'
import { useHost } from './host.tsx'
import { MODELS, useImageLane, ZEN } from './kinds/image.tsx'
import { ALL, kind } from './kinds/index.ts'
import { lit } from './kinds/kind.ts'
import { PRESETS } from './kinds/type/system.ts'
import { BRAIN, KEPT, touch, useAvailable, useLibrary, useWire, WRITER } from './live.ts'
import { blank, current, type Chat, type KindId, type Made, type Stored } from './made.ts'
import { take } from './places.tsx'
import { path } from './route.ts'
import type { Starter } from './starters.ts'
import { useUrl } from './use.ts'
import { put, type Wire } from './wire.ts'

/** The conversations open in this page, by id: an address change does not drop the one in hand. */
const held = new Map<string, Chat>()

/** The pane edge and gutter, as @hanzo/build cuts a Dev run's two panes. */
const pane = {
  bg: 'var(--pane-fill, var(--sheet-1))',
  rounded: 'var(--pane-round, var(--radius-xl))',
  borderWidth: 1,
  borderColor: 'var(--pane-edge, var(--white-08))',
  boxShadow: 'var(--shadow-sheet-2)',
} as const
const gap = 'var(--pane-gap, var(--space-2))'

const DESK = 560
const DESK_FLOOR = 320
const TALK_FLOOR = 320

interface Options {
  model: string
  aspect: string
  count: number
}

export function Talk({ id }: { id: string | null }) {
  const host = useHost()
  const wire = useWire(host)
  const available = useAvailable(wire)
  const [chat, setChat] = useState<Chat | null>(() => (id ? (held.get(id) ?? null) : blank()))
  const [failed, setFailed] = useState('')

  useEffect(() => {
    if (!id) {
      setChat(blank())
      return
    }
    const mine = held.get(id)
    if (mine) {
      setChat(mine)
      return
    }
    let live = true
    setChat(null)
    open(wire, id).then(
      (c) => {
        if (!live) return
        if (!c) return setFailed('This conversation is not here, or belongs to another organization.')
        // A turn that was working when its page closed did not finish: say so.
        const settled = { ...c, turns: c.turns.map((t) => (t.doing ? { ...t, doing: undefined, failed: t.failed ?? 'Stopped: the page closed before this finished.' } : t)) }
        held.set(c.id, settled)
        setChat(settled)
      },
      (e: unknown) => live && setFailed(e instanceof Error ? e.message : String(e)),
    )
    return () => {
      live = false
    }
  }, [id, wire])

  if (failed) {
    return (
      <YStack flex={1} items="center" justify="center" gap="$3" p="$5">
        <SizableText color="$soft">{failed}</SizableText>
        <XStack render="button" onPress={() => host.go('')} px="$3" height={34} items="center" rounded="$3" borderWidth={1} borderColor="$borderColor" cursor="pointer">
          <SizableText size="$2">Start a new one</SizableText>
        </XStack>
      </YStack>
    )
  }
  if (!chat) {
    return (
      <YStack flex={1} items="center" justify="center">
        <SizableText color="$soft">Opening…</SizableText>
      </YStack>
    )
  }
  return <Conversation key={chat.id} start={chat} host={host} wire={wire} available={available} />
}

function Conversation({ start, host, wire, available }: { start: Chat; host: Host; wire: Wire; available: ReturnType<typeof useAvailable> }) {
  const [chat, setChat] = useState(start)
  const latest = useRef(chat)
  const [draft, setDraft] = useState('')
  const [attached, setAttached] = useState<Stored[]>([])
  const [uploading, setUploading] = useState(0)
  const [picked, setPicked] = useState<KindId | null>(null)
  const [options, setOptions] = useState<Options>({ model: 'auto', aspect: '', count: 1 })
  const [working, setWorking] = useState<AbortController | null>(null)
  const [editing, setEditing] = useState<Record<string, boolean>>({})
  const [editFailed, setEditFailed] = useState<Record<string, string>>({})
  const wide = useMedia().md
  const shelf = useLibrary(wire)
  const imaging = useImageLane()
  const [desk, setDesk] = usePanel('studio', true, wide)
  useKey(() => setDesk(!desk))
  const deck = useDeck(`studio.${chat.id}`, { tabs: chat.made.map((m) => ({ id: m.id, kind: m.id, title: m.title })), at: chat.made.at(-1)?.id ?? null })
  const order = useRef<string[]>([])
  order.current = [...deck.tabs.map((t) => t.kind).filter((k) => chat.made.some((m) => m.id === k)), ...chat.made.map((m) => m.id).filter((m) => !deck.tabs.some((t) => t.kind === m))]
  const [span, setSpan] = useState(DESK)
  const deskBox = useRef<HTMLElement | null>(null)

  // Saved after every change, at most once a second and a half; at once when a turn ends.
  const saving = useRef<ReturnType<typeof setTimeout> | null>(null)
  const persist = useCallback(
    (c: Chat, now = false) => {
      if (saving.current) clearTimeout(saving.current)
      const go = () => {
        saving.current = null
        if (!c.turns.length) return
        void keep(wire, c).catch(() => undefined)
      }
      if (now) go()
      else saving.current = setTimeout(go, 1500)
    },
    [wire],
  )
  const changed = useCallback(
    (c: Chat) => {
      latest.current = c
      held.set(c.id, c)
      setChat(c)
      if (c.turns.length) touch(listed(c))
      persist(c)
    },
    [persist],
  )

  const show = useCallback(
    (mid: string) => {
      const m = latest.current.made.find((x) => x.id === mid)
      deck.open({ id: mid, kind: mid, title: m?.title ?? 'Made' })
      if (!order.current.includes(mid)) order.current = [...order.current, mid]
      setDesk(true)
    },
    [deck, setDesk],
  )

  const send = async (text: string, how: { kind?: KindId | null } = {}) => {
    const words = text.trim()
    if (!words && !attached.length) return
    if (working) return
    if (!host.person) {
      host.signIn?.()
      return
    }
    const ctl = new AbortController()
    setWorking(ctl)
    const files = attached
    setDraft('')
    setAttached([])
    // The address names the conversation from its first message on.
    if (!latest.current.turns.length) host.go(path({ kind: 'chat', id: latest.current.id }), { replace: true })
    const asked: Asked = {
      text: words || 'Here is a picture.',
      attached: files,
      kind: (how.kind ?? picked) || undefined,
      options: { model: options.model === 'auto' ? undefined : options.model, aspect: options.aspect || undefined, count: options.count > 1 ? options.count : undefined },
    }
    try {
      const end = await turn(latest.current, asked, {
        wire,
        brain: BRAIN,
        writer: WRITER,
        fleet: available.fleet,
        models: available.models,
        order: () => order.current,
        show,
        changed,
        signal: ctl.signal,
      })
      persist(end, true)
    } finally {
      setWorking(null)
    }
  }

  // A starter chosen in Explore starts this conversation.
  useEffect(() => {
    const s = take()
    if (s) begin(s)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const begin = (s: Starter) => {
    setPicked(s.kind)
    if (s.picture && !attached.length) {
      setDraft(s.prompt)
      attach()
      return
    }
    void send(s.prompt, { kind: s.kind })
  }

  const attach = () => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = 'image/*'
    input.multiple = true
    input.onchange = () => void upload(Array.from(input.files ?? []))
    input.click()
  }

  const upload = async (files: File[]) => {
    const pics = files.filter((f) => f.type.startsWith('image/')).slice(0, 4)
    if (!pics.length) return
    setUploading((n) => n + pics.length)
    for (const f of pics) {
      try {
        const name = f.name.replace(/[^\w.-]+/g, '-').slice(-80) || 'picture.png'
        const key = await put(wire, `files/image/${latest.current.id}/attached/${Date.now().toString(36)}/${name}`, f)
        const bmp = await createImageBitmap(f).catch(() => null)
        setAttached((a) => [...a, { key, mime: f.type, name, width: bmp?.width, height: bmp?.height }])
        bmp?.close()
      } catch {
        // The chip never appears; the person sees the picture did not attach.
      } finally {
        setUploading((n) => n - 1)
      }
    }
  }

  const pick = (m: Made, at: number) => changed({ ...latest.current, made: latest.current.made.map((x) => (x.id === m.id ? { ...x, at } : x)) })

  const editOne = async (m: Made, patch: Record<string, unknown>, note: string) => {
    setEditing((e) => ({ ...e, [m.id]: true }))
    setEditFailed((e) => ({ ...e, [m.id]: '' }))
    try {
      const now = latest.current.made.find((x) => x.id === m.id) ?? m
      await editInPlace(latest.current, now, patch, note, {
        wire,
        writer: WRITER,
        fleet: available.fleet,
        models: available.models,
        order: () => order.current,
        changed,
        signal: new AbortController().signal,
      })
    } catch (e) {
      setEditFailed((x) => ({ ...x, [m.id]: e instanceof Error ? e.message : String(e) }))
    } finally {
      setEditing((x) => ({ ...x, [m.id]: false }))
    }
  }

  const tabs: Tab[] = useMemo(
    () =>
      chat.made.map((m) => {
        const Icon = kind(m.kind).icon
        return {
          id: m.id,
          label: m.title,
          icon: <Icon size={14} />,
          bleed: true,
          render: () => (
            <Desk
              made={chat.made.find((x) => x.id === m.id) ?? m}
              wire={wire}
              others={chat.made.filter((x) => x.id !== m.id)}
              onPick={(at) => pick(m, at)}
              onEdit={(patch, note) => void editOne(m, patch, note)}
              busy={Boolean(editing[m.id])}
              failed={editFailed[m.id]}
            />
          ),
        }
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [chat.made, wire, editing, editFailed],
  )

  const messages: Said[] = chat.turns.map((t) => ({ id: t.id, role: t.role, content: t.text }))
  const busy = Boolean(working)
  const empty = !chat.turns.length

  const body = (said: Said) => {
    const t = chat.turns.find((x) => x.id === said.id)
    if (!t) return null
    if (t.role === 'user') {
      return (
        <YStack gap="$2">
          {t.attached?.length ? (
            <XStack gap="$2" flexWrap="wrap">
              {t.attached.map((f) => (
                <Thumb key={f.key} wire={wire} file={f} />
              ))}
            </XStack>
          ) : null}
          <SizableText size="$4" whiteSpace="pre-wrap">
            {t.text}
          </SizableText>
        </YStack>
      )
    }
    const made = (t.made ?? []).map((mid) => chat.made.find((m) => m.id === mid)).filter((m): m is Made => Boolean(m))
    return (
      <YStack gap="$2">
        {t.text ? (
          <SizableText size="$4" whiteSpace="pre-wrap">
            {t.text}
          </SizableText>
        ) : null}
        {made.length ? (
          <XStack gap="$2" flexWrap="wrap">
            {made.map((m) => (
              <Chip key={m.id} made={m} wire={wire} n={order.current.indexOf(m.id) + 1} onPress={() => show(m.id)} />
            ))}
          </XStack>
        ) : null}
        {t.doing ? (
          <XStack items="center" gap="$2" data-slot="doing">
            <LoaderCircle size={14} />
            <SizableText size="$2" color="$soft">
              {t.doing}…
            </SizableText>
          </XStack>
        ) : null}
        {t.failed ? <Failure onRetry={busy ? undefined : () => void retry(t.id)}>{t.failed}</Failure> : null}
        {t.text && !t.doing ? <Reply text={t.text} /> : null}
      </YStack>
    )
  }

  const retry = (assistant: string) => {
    const i = chat.turns.findIndex((t) => t.id === assistant)
    const q = chat.turns
      .slice(0, i)
      .reverse()
      .find((t) => t.role === 'user')
    if (q) void send(q.text)
  }

  const kindItems = [
    { key: 'auto', label: 'Auto', description: 'Studio picks from what you ask', onSelect: () => setPicked(null) },
    ...ALL.map((k) => {
      const why = k.needs(available)
      return { key: k.id, label: k.label, description: why ? 'Needs a GPU' : k.blurb, disabled: Boolean(why), onSelect: () => setPicked(k.id) }
    }),
  ]
  const brandName = brandOf(chat)
  const brandItems = [
    { type: 'label' as const, key: 'h', label: 'Draw in' },
    { key: 'none', label: 'Hanzo (default)', onSelect: () => changed({ ...latest.current, brand: undefined }) },
    ...chat.made
      .filter((m) => m.kind === 'type')
      .map((m) => ({ key: m.id, label: m.title, description: 'From this conversation', onSelect: () => changed({ ...latest.current, brand: m.id }) })),
    { type: 'separator' as const, key: 's' },
    ...PRESETS.map((p) => ({
      key: `preset-${p.name}`,
      label: p.name,
      description: p.blurb,
      onSelect: () => {
        const m: Made = { id: `type${Date.now().toString(36)}`, kind: 'type', title: p.name, at: 0, versions: [{ id: `v${Date.now().toString(36)}`, at: Date.now(), spec: p as unknown as Record<string, unknown>, files: [], note: `The ${p.name} preset`, by: 'preset' }] }
        changed({ ...latest.current, made: [...latest.current.made, m], brand: m.id })
        show(m.id)
      },
    })),
    { type: 'separator' as const, key: 's2' },
    { key: 'make', label: 'Make a type system…', onSelect: () => setDraft('A type system for ') },
  ]
  const zen = lit(available.fleet, 'image.zen')
  const optionItems = [
    { type: 'label' as const, key: 'm', label: 'Image model' },
    { key: 'auto', label: `${options.model === 'auto' ? '✓ ' : ''}Auto`, description: zen ? 'Zen Image on your GPU' : MODELS[0].label, onSelect: () => setOptions({ ...options, model: 'auto' }) },
    ...(zen ? [{ key: ZEN, label: `${options.model === ZEN ? '✓ ' : ''}Zen Image`, description: 'On your GPU', onSelect: () => setOptions({ ...options, model: ZEN }) }] : []),
    ...MODELS.map((m) => ({ key: m.id, label: `${options.model === m.id ? '✓ ' : ''}${m.label}`, description: m.id, onSelect: () => setOptions({ ...options, model: m.id }) })),
    { type: 'separator' as const, key: 's' },
    { type: 'label' as const, key: 'a', label: 'Aspect' },
    ...['', '1:1', '4:5', '9:16', '16:9', '3:2'].map((a) => ({ key: `a${a}`, label: `${options.aspect === a ? '✓ ' : ''}${a || 'As asked'}`, onSelect: () => setOptions({ ...options, aspect: a }) })),
    { type: 'separator' as const, key: 's2' },
    { type: 'label' as const, key: 'c', label: 'How many' },
    ...[1, 2, 4].map((n) => ({ key: `c${n}`, label: `${options.count === n ? '✓ ' : ''}${n}`, onSelect: () => setOptions({ ...options, count: n }) })),
  ]

  const tools = (
    <XStack items="center" gap={2}>
      <DropdownMenu trigger={<Pillish label="Kind" icon={<Sparkles size={13} />} value={picked ? kind(picked).label : 'Auto'} />} items={kindItems} />
      <DropdownMenu trigger={<Pillish label="Brand" icon={<Palette size={13} />} value={brandName} />} items={brandItems} />
      <DropdownMenu
        trigger={
          <XStack render="button" aria-label="More options: model, aspect, count" width={32} height={32} items="center" justify="center" rounded={999} cursor="pointer" hoverStyle={{ bg: '$hover' }}>
            <Ellipsis size={16} />
          </XStack>
        }
        items={optionItems}
      />
    </XStack>
  )

  const head = attached.length || uploading ? (
    <XStack gap="$2" flexWrap="wrap" data-slot="attached">
      {attached.map((f) => (
        <XStack key={f.key} position="relative">
          <Thumb wire={wire} file={f} />
          <XStack
            render="button"
            aria-label={`Remove ${f.name}`}
            onPress={() => setAttached((a) => a.filter((x) => x.key !== f.key))}
            position="absolute"
            t={-6}
            r={-6}
            width={20}
            height={20}
            rounded={99}
            bg="$background"
            borderWidth={1}
            borderColor="$borderColor"
            items="center"
            justify="center"
            cursor="pointer"
          >
            <X size={11} />
          </XStack>
        </XStack>
      ))}
      {uploading ? (
        <XStack width={56} height={56} rounded="$3" bg="$hover" items="center" justify="center">
          <LoaderCircle size={16} />
        </XStack>
      ) : null}
    </XStack>
  ) : undefined

  const composer = prompt({
    value: draft,
    onChange: setDraft,
    onSend: () => void send(draft),
    onStop: working ? () => working.abort() : undefined,
    busy,
    ready: Boolean(draft.trim() || attached.length),
    placeholder: empty ? 'What should we create?' : 'Ask for a change, or something new',
    label: 'What should we create?',
    head,
    onAttach: attach,
    tools,
    mind: null,
    narrow: !wide,
    note: imaging ? `Image models are not answering (${imaging.reason}). Posters and ads are set in type until they are back.` : shelf === false ? KEPT : undefined,
  })

  const deskPane = (sheet: boolean) => (
    <Panel side={deck} kinds={tabs} onHide={() => setDesk(false)} sheet={sheet} label="What this conversation made" />
  )

  return (
    <XStack data-slot="studio-split" flex={1} minH={0} minW={0} width="100%">
      <YStack data-slot="studio-talk" {...pane} flex={1} minH={0} minW={0} width="100%" overflow="hidden">
        <XStack px="$4" pt="$3" pb="$2" gap="$2" items="center" minH={44}>
          <SizableText render="h1" size="$5" color="$ink" numberOfLines={1} flex={1} minW={0}>
            {empty ? 'Studio' : chat.title}
          </SizableText>
          {chat.made.length ? <PanelToggle open={desk} onToggle={() => setDesk(!desk)} /> : null}
        </XStack>
        <Thread
          messages={messages}
          streaming={busy}
          send={(text) => void send(text)}
          stop={working ? () => working.abort() : undefined}
          body={body}
          center
          onFiles={(files) => void upload(files)}
          empty={<Opening />}
          composer={{ ...composer, foot: empty ? <Home available={available} onStart={begin} /> : undefined }}
        />
      </YStack>
      {desk && chat.made.length && !wide ? deskPane(true) : null}
      {desk && chat.made.length && wide ? (
        <YStack
          data-slot="studio-desk"
          ref={(el: unknown) => {
            deskBox.current = el instanceof HTMLElement ? el : null
          }}
          {...pane}
          ml={gap}
          position="relative"
          shrink={0}
          minH={0}
          overflow="hidden"
          style={{ width: span, maxWidth: `calc(100% - ${TALK_FLOOR}px)`, minWidth: DESK_FLOOR }}
        >
          <Grip
            side="right"
            span={span}
            floor={DESK_FLOOR}
            ceil={1400}
            reset={DESK}
            onSpan={(n) => deskBox.current?.style.setProperty('width', `${n}px`)}
            onKeep={setSpan}
            onShut={() => setDesk(false)}
            label="Resize the tabs"
            l={`calc(-1 * ${gap} - 1px)`}
            width={gap}
          />
          {deskPane(false)}
        </YStack>
      ) : null}
    </XStack>
  )
}

function brandOf(chat: Chat): string {
  const m = chat.brand ? chat.made.find((x) => x.id === chat.brand) : null
  return m ? m.title : 'Hanzo'
}

function Opening() {
  return (
    <YStack items="center" width="100%" pt="$6" pb="$2" gap="$2">
      <SizableText render="h2" size="$9" fontWeight="600" color="$ink" text="center">
        What should we create?
      </SizableText>
      <SizableText size="$3" color="$soft" text="center">
        Images, posters, documents, slides, type systems, 3D, video and sound. Everything opens in a tab.
      </SizableText>
    </YStack>
  )
}

/** A composer chip: what it sets, and its value. */
/** A menu's trigger: the menu hands it its state and handlers, and they land on the button itself. */
function Pillish({ label, icon, value, ...trigger }: { label: string; icon: ReactNode; value: string } & ComponentProps<typeof XStack>) {
  return (
    <XStack {...trigger} render="button" aria-label={`${label}: ${value}`} items="center" gap="$1.5" height={32} px="$2.5" rounded={999} borderWidth={1} borderColor="$borderColor" cursor="pointer" hoverStyle={{ bg: '$hover' }}>
      {icon}
      <SizableText size="$2" numberOfLines={1} maxW={120}>
        {value}
      </SizableText>
      <ChevronDown size={12} />
    </XStack>
  )
}

/** What a turn made, as a chip that opens its tab. */
function Chip({ made, wire, n, onPress }: { made: Made; wire: Wire; n: number; onPress: () => void }) {
  const k = kind(made.kind)
  const Icon = k.icon
  const pic = current(made).files.find((f) => f.mime.startsWith('image/'))
  const { src } = useUrl(wire, pic?.key)
  return (
    <XStack render="button" aria-label={`Open tab ${n}: ${made.title}`} onPress={onPress} items="center" gap="$2" p="$1.5" pr="$3" rounded="$4" borderWidth={1} borderColor="$borderColor" cursor="pointer" hoverStyle={{ bg: '$hover' }} maxW={280}>
      {src ? (
        <img src={src} alt="" width={40} height={40} style={{ borderRadius: 8, objectFit: 'cover' }} />
      ) : (
        <XStack width={40} height={40} rounded="$3" bg="$hover" items="center" justify="center">
          <Icon size={16} />
        </XStack>
      )}
      <YStack minW={0}>
        <SizableText size="$2" numberOfLines={1}>
          {made.title}
        </SizableText>
        <SizableText size="$1" color="$soft">
          {`Tab ${n} · ${k.label} · v${made.at + 1}`}
        </SizableText>
      </YStack>
      <Check size={13} />
    </XStack>
  )
}

function Thumb({ wire, file }: { wire: Wire; file: Stored }) {
  const { src } = useUrl(wire, file.key)
  return src ? (
    <img src={src} alt={file.name} width={56} height={56} style={{ borderRadius: 8, objectFit: 'cover' }} />
  ) : (
    <XStack width={56} height={56} rounded="$3" bg="$hover" items="center" justify="center">
      <ImageIcon size={16} />
    </XStack>
  )
}
