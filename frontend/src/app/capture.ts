/**
 * Capture the twin viewport as a PNG: the WebGL frame (or illustrative image), the leader lines and
 * the live annotation labels as rendered, plus a provenance footer. Runs entirely in the browser.
 */
export async function captureViewport(root: HTMLElement, footer: string, fileName: string): Promise<void> {
  const box = root.getBoundingClientRect()
  const scale = Math.min(2, window.devicePixelRatio || 1)
  const footerH = 28
  const canvas = document.createElement('canvas')
  canvas.width = Math.round(box.width * scale)
  canvas.height = Math.round((box.height + footerH) * scale)
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('Canvas 2D context unavailable')
  ctx.scale(scale, scale)
  ctx.fillStyle = '#0d1116'
  ctx.fillRect(0, 0, box.width, box.height + footerH)

  const place = (el: Element) => {
    const r = el.getBoundingClientRect()
    return { x: r.left - box.left, y: r.top - box.top, w: r.width, h: r.height }
  }
  for (const el of root.querySelectorAll('canvas, .viewport__image img')) {
    const r = place(el)
    if (r.w < 2 || r.h < 2) continue
    try {
      ctx.drawImage(el as CanvasImageSource, r.x, r.y, r.w, r.h)
    } catch {
      /* tainted or not ready: skip that layer */
    }
  }
  ctx.strokeStyle = '#69cfe5'
  ctx.lineWidth = 1
  for (const line of root.querySelectorAll('.model-overlay__lines line')) {
    if ((line as SVGLineElement).style.visibility === 'hidden') continue
    const [x1, y1, x2, y2] = ['x1', 'y1', 'x2', 'y2'].map((a) => Number(line.getAttribute(a) ?? 0))
    ctx.beginPath()
    ctx.moveTo(x1, y1)
    ctx.lineTo(x2, y2)
    ctx.stroke()
  }
  for (const dot of root.querySelectorAll('.model-overlay__dot, .sensor-anchor')) {
    const r = place(dot)
    if (r.w === 0 || (dot as HTMLElement).style.visibility === 'hidden') continue
    ctx.beginPath()
    ctx.arc(r.x + r.w / 2, r.y + r.h / 2, r.w / 2, 0, Math.PI * 2)
    ctx.fillStyle = dot.classList.contains('is-selected') ? '#69cfe5' : '#0d1116'
    ctx.fill()
    ctx.stroke()
  }
  // Text overlays: annotation labels, toolbars and panels inside the viewport, drawn line by line.
  ctx.textBaseline = 'top'
  for (const el of root.querySelectorAll('.model-overlay__label, .telemetry-anchor, .annotation, .viewport__identity, .inspector, .component-selector')) {
    const r = place(el)
    if (r.w === 0 || (el as HTMLElement).style.visibility === 'hidden') continue
    ctx.fillStyle = 'rgba(13,17,22,0.85)'
    ctx.fillRect(r.x, r.y, r.w, r.h)
    let y = r.y + 6
    for (const node of el.querySelectorAll('p, span.telemetry-anchor__name, span.telemetry-anchor__value, span.selectable__title > span:first-child, span.selectable__value')) {
      const text = (node.textContent ?? '').trim()
      if (!text || node.querySelector('p, span')) continue
      const style = getComputedStyle(node)
      ctx.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`
      ctx.fillStyle = style.color
      ctx.fillText(text, r.x + 6, y, r.w - 12)
      y += parseFloat(style.fontSize) * 1.45 + 2
      if (y > r.y + r.h - 4) break
    }
  }
  ctx.fillStyle = '#161b21'
  ctx.fillRect(0, box.height, box.width, footerH)
  ctx.font = '10px "IBM Plex Mono", monospace'
  ctx.fillStyle = '#a1acb9'
  ctx.fillText(footer, 12, box.height + 9)

  const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/png'))
  if (!blob) throw new Error('Could not encode the image')
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = fileName
  a.click()
  setTimeout(() => URL.revokeObjectURL(url), 2000)
}
