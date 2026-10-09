/** Browser (OS) notifications for the browser channel: shown only with the user's permission. */

export type PermissionState = 'granted' | 'denied' | 'default' | 'unsupported'

export function permission(): PermissionState {
  if (typeof window === 'undefined' || !('Notification' in window)) return 'unsupported'
  return Notification.permission as PermissionState
}

export async function requestPermission(): Promise<PermissionState> {
  if (permission() === 'unsupported') return 'unsupported'
  try {
    return (await Notification.requestPermission()) as PermissionState
  } catch {
    return permission()
  }
}

/** ``tag`` = notification id: a repeated delivery replaces instead of duplicating. */
export function showBrowserNotification(id: string, title: string, body: string, onClick: () => void): boolean {
  if (permission() !== 'granted') return false
  try {
    const n = new Notification(title, { body, tag: id })
    n.onclick = () => {
      window.focus()
      onClick()
      n.close()
    }
    return true
  } catch {
    return false
  }
}
