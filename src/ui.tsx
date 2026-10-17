/**
 * The few controls Studio draws itself, on gui tokens: a choice from a list
 * (the browser's own <select>), a colour, a line or a paragraph of text, a pill
 * that is on or off, and a quiet labelled row. Everything larger is @hanzo/ui's
 * or @hanzo/build's.
 */
import { SizableText, styled, XStack, YStack } from '@hanzo/gui'
import type { ChangeEvent, ReactNode } from 'react'

const field = {
  size: '$2',
  minW: 0,
  rounded: '$3',
  borderWidth: 1,
  borderColor: '$borderColor',
  bg: 'transparent',
  color: '$ink',
  focusVisibleStyle: { outlineWidth: 2, outlineStyle: 'solid', outlineColor: '$outlineColor' },
} as const

const Select = styled(SizableText, { name: 'StudioChoice', render: 'select', height: 32, px: '$2', cursor: 'pointer', ...field })
const Line = styled(SizableText, { name: 'StudioLine', render: 'input', height: 32, px: '$2.5', ...field })
const Lines = styled(SizableText, { name: 'StudioLines', render: 'textarea', p: '$2.5', minH: 72, ...field })

export interface Option {
  value: string
  label?: string
}

export function Choice({ value, options, onChange, label }: { value: string; options: readonly Option[]; onChange: (v: string) => void; label: string }) {
  const all = value === '' || options.some((o) => o.value === value) ? options : [{ value }, ...options]
  return (
    <Select {...({ value } as object)} onChange={(e: ChangeEvent<HTMLElement>) => onChange((e.currentTarget as HTMLSelectElement).value)} aria-label={label}>
      {all.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label ?? o.value}
        </option>
      ))}
    </Select>
  )
}

export function Text1({ value, onChange, label, placeholder }: { value: string; onChange: (v: string) => void; label: string; placeholder?: string }) {
  return (
    <Line
      {...({ value, placeholder } as object)}
      onChange={(e: ChangeEvent<HTMLElement>) => onChange((e.currentTarget as HTMLInputElement).value)}
      aria-label={label}
    />
  )
}

export function TextN({ value, onChange, label, rows = 3 }: { value: string; onChange: (v: string) => void; label: string; rows?: number }) {
  return <Lines {...({ value, rows } as object)} onChange={(e: ChangeEvent<HTMLElement>) => onChange((e.currentTarget as HTMLTextAreaElement).value)} aria-label={label} />
}

export function Color({ value, onChange, label }: { value: string; onChange: (v: string) => void; label: string }) {
  return (
    <input
      type="color"
      value={value}
      aria-label={label}
      onChange={(e) => onChange(e.currentTarget.value)}
      style={{ width: 32, height: 32, padding: 0, border: '1px solid var(--borderColor)', borderRadius: 8, background: 'transparent', cursor: 'pointer' }}
    />
  )
}

/** A label above a control. */
export function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <YStack gap="$1" minW={0}>
      <SizableText size="$1" color="$soft">
        {label}
      </SizableText>
      {children}
    </YStack>
  )
}

/** A pill that is on or off: a category, a filter. */
export function Pill({ on, onPress, children, label }: { on: boolean; onPress: () => void; children: ReactNode; label?: string }) {
  return (
    <XStack
      render="button"
      role="button"
      aria-pressed={on}
      aria-label={label}
      onPress={onPress}
      items="center"
      gap="$1.5"
      height={32}
      px="$3"
      rounded={999}
      borderWidth={1}
      borderColor={on ? '$ink' : '$borderColor'}
      bg={on ? '$ink' : 'transparent'}
      cursor="pointer"
      hoverStyle={{ bg: on ? '$ink' : '$hover' }}
      focusVisibleStyle={{ outlineWidth: 2, outlineStyle: 'solid', outlineColor: '$outlineColor' }}
      shrink={0}
    >
      <SizableText size="$2" color={on ? '$background' : '$ink'}>
        {children}
      </SizableText>
    </XStack>
  )
}

/** A small press with an icon and a word: the tab bar's controls. */
export function Act({ onPress, children, label, on = false, disabled = false }: { onPress: () => void; children: ReactNode; label: string; on?: boolean; disabled?: boolean }) {
  return (
    <XStack
      render="button"
      role="button"
      aria-label={label}
      aria-pressed={on || undefined}
      disabled={disabled}
      onPress={disabled ? undefined : onPress}
      items="center"
      gap="$1.5"
      height={30}
      px="$2.5"
      rounded="$3"
      bg={on ? '$hover' : 'transparent'}
      opacity={disabled ? 0.4 : 1}
      cursor={disabled ? 'default' : 'pointer'}
      hoverStyle={disabled ? undefined : { bg: '$hover' }}
      focusVisibleStyle={{ outlineWidth: 2, outlineStyle: 'solid', outlineColor: '$outlineColor' }}
      shrink={0}
    >
      {children}
    </XStack>
  )
}

/** Save bytes as a file the person keeps. */
export function download(name: string, blob: Blob): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 10_000)
}
