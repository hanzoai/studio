import { describe, expect, it } from 'vitest'

import { fresh, path, route } from './route.ts'

describe('route', () => {
  it('reads every place and writes it back', () => {
    for (const at of ['', 'library', 'explore', 'activity', 'c/0mvbqspbsawacqm']) expect(path(route(at))).toBe(at)
  })
  it('reads an address as New when it is not one of Studio’s', () => {
    for (const at of ['templates', 'settings', 'c/', 'c/../x', 'c/A B', 'library/x', 'c/a/b']) expect(route(at)).toEqual({ kind: 'new' })
  })
  it('ignores slashes, a query and a fragment', () => {
    expect(route('/library/')).toEqual({ kind: 'library' })
    expect(route('c/abc?x=1#y')).toEqual({ kind: 'chat', id: 'abc' })
  })
  it('mints ids the grammar reads and a listing sorts by time', () => {
    const a = fresh()
    expect(route(`c/${a}`)).toEqual({ kind: 'chat', id: a })
    expect(a).toMatch(/^[a-z0-9]{15}$/)
  })
})
