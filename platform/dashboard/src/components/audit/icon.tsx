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
  Search,
  ShieldX,
  X,
  type LucideIcon,
} from 'lucide-react'

// Maps the design's kebab-case `data-lucide` names to lucide-react components.
// Only the icons the Audit page uses are registered.
const ICONS: Record<string, LucideIcon> = {
  activity: Activity,
  check: Check,
  'chevron-right': ChevronRight,
  'circle-dashed': CircleDashed,
  download: Download,
  filter: Filter,
  lightbulb: Lightbulb,
  list: List,
  search: Search,
  'shield-x': ShieldX,
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
