import type { CSSProperties } from 'react'
import {
  Activity,
  Check,
  ChevronRight,
  CircleDashed,
  Download,
  Filter,
  Lightbulb,
  List,
  X,
  type LucideIcon,
} from 'lucide-react'

// kebab `data-lucide` name → lucide-react component (only icons the page uses).
const ICONS: Record<string, LucideIcon> = {
  activity: Activity,
  check: Check,
  'chevron-right': ChevronRight,
  'circle-dashed': CircleDashed,
  download: Download,
  filter: Filter,
  lightbulb: Lightbulb,
  list: List,
  x: X,
}

export function Icon({
  name,
  size = 16,
  strokeWidth = 1.5,
  color = 'currentColor',
  style,
}: {
  name: string
  size?: number
  strokeWidth?: number
  color?: string
  style?: CSSProperties
}) {
  const Cmp = ICONS[name]
  if (!Cmp) return null
  return (
    <Cmp
      size={size}
      strokeWidth={strokeWidth}
      color={color}
      style={{ flexShrink: 0, ...style }}
    />
  )
}
