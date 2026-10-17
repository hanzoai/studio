/**
 * @hanzo/studio: Studio as a component.
 *
 * `Studio({host, rail})` under a gui root in a box with a height; `nav(host,
 * go)` for a host that draws Studio's places in a rail of its own (the Hanzo
 * app at hanzo.ai/studio). `host` is @hanzo/build's `Host`, the value Dev
 * takes, and `host.path` is Studio's address under its mount (`route`).
 */
export { Studio } from './studio.tsx'
export { nav, NEW, type Group } from './nav.tsx'
export { path, route, type Route } from './route.ts'
export type { Host } from '@hanzo/build'
