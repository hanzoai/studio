/**
 * Studio's places, once, for every rail that lists them: Studio's own rail on
 * studio.hanzo.ai, and the Hanzo app's column at hanzo.ai/studio (the shape
 * @hanzo/build's `nav` gives Dev, so a host draws both one way). Every row
 * moves Studio through `go` with an address `path` writes.
 */
import type { Host } from '@hanzo/build'
import { Activity, LayoutTemplate, Library, SquarePen } from '@hanzogui/lucide-icons-2'
import type { RailLink } from '@hanzo/ui/chat'

import { path, route } from './route.ts'

/** A run of places under one quiet label. */
export interface Group {
  id: 'make' | 'made' | 'run'
  label: string
  links: RailLink[]
}

/** The id of the row that starts a new conversation: a rail with a New control of its own draws that instead. */
export const NEW = 'new'

export function nav(host: Host, go: (path: string) => void): Group[] {
  const r = route(host.path)
  const to = (p: string) => () => go(p)
  return [
    {
      id: 'make',
      label: 'Create',
      links: [
        { id: NEW, label: 'New creation', icon: <SquarePen size={16} />, onPress: to(''), active: r.kind === 'new' },
        { id: 'explore', label: 'Explore', icon: <LayoutTemplate size={16} />, onPress: to(path({ kind: 'explore' })), active: r.kind === 'explore' },
      ],
    },
    {
      id: 'made',
      label: 'Made',
      links: [{ id: 'library', label: 'Library', icon: <Library size={16} />, onPress: to(path({ kind: 'library' })), active: r.kind === 'library' }],
    },
    {
      id: 'run',
      label: 'Run',
      links: [{ id: 'activity', label: 'Activity', icon: <Activity size={16} />, onPress: to(path({ kind: 'activity' })), active: r.kind === 'activity' }],
    },
  ]
}
