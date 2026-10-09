/**
 * Icon assets exported from the Figma file (lucide glyphs). They are rendered as CSS masks so one
 * asset can take the active (cyan) or idle (grey) colour from `currentColor` without editing it.
 */
import activity from '../assets/figma/icons/activity.svg'
import arrowRight from '../assets/figma/icons/arrow-right.svg'
import arrowUpRight from '../assets/figma/icons/arrow-up-right.svg'
import bell from '../assets/figma/icons/bell.svg'
import box from '../assets/figma/icons/box.svg'
import brandBox from '../assets/figma/icons/brand-box.svg'
import calendarDays from '../assets/figma/icons/calendar-days-cyan.svg'
import chartNoAxesCombined from '../assets/figma/icons/chart-no-axes-combined.svg'
import check from '../assets/figma/icons/check.svg'
import chevronDown from '../assets/figma/icons/chevron-down.svg'
import chevronsUpDown from '../assets/figma/icons/chevrons-up-down.svg'
import columns2 from '../assets/figma/icons/columns-2.svg'
import cpu from '../assets/figma/icons/cpu.svg'
import crosshair from '../assets/figma/icons/crosshair.svg'
import download from '../assets/figma/icons/download.svg'
import flaskConical from '../assets/figma/icons/flask-conical.svg'
import history from '../assets/figma/icons/history.svg'
import laptop from '../assets/figma/icons/laptop.svg'
import layers from '../assets/figma/icons/layers.svg'
import layoutDashboard from '../assets/figma/icons/layout-dashboard.svg'
import listTree from '../assets/figma/icons/list-tree.svg'
import pause from '../assets/figma/icons/pause.svg'
import play from '../assets/figma/icons/play-cyan.svg'
import plug from '../assets/figma/icons/plug-cyan.svg'
import radio from '../assets/figma/icons/radio.svg'
import rotate3d from '../assets/figma/icons/rotate-3d.svg'
import rotateCcw from '../assets/figma/icons/rotate-ccw.svg'
import scan from '../assets/figma/icons/scan.svg'
import search from '../assets/figma/icons/search.svg'
import settings2 from '../assets/figma/icons/settings-2.svg'
import shieldCheck from '../assets/figma/icons/shield-check.svg'
import timer from '../assets/figma/icons/timer.svg'
import zoomIn from '../assets/figma/icons/zoom-in.svg'

export const icons = {
  activity,
  arrowRight,
  arrowUpRight,
  bell,
  box,
  brandBox,
  calendarDays,
  chartNoAxesCombined,
  check,
  chevronDown,
  chevronsUpDown,
  columns2,
  cpu,
  crosshair,
  download,
  flaskConical,
  history,
  laptop,
  layers,
  layoutDashboard,
  listTree,
  pause,
  play,
  plug,
  radio,
  rotate3d,
  rotateCcw,
  scan,
  search,
  settings2,
  shieldCheck,
  timer,
  zoomIn,
} as const

export type IconName = keyof typeof icons
