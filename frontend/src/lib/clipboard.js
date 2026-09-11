/**
 * Copy text to the clipboard, returning whether it actually worked.
 *
 * `navigator.clipboard` only exists in a secure context (HTTPS, or
 * localhost) - on a plain-HTTP deployment it is simply undefined, so
 * `navigator.clipboard?.writeText(...)` silently does nothing while the
 * caller still shows a "copied" checkmark. Falls back to the legacy
 * execCommand approach, and only reports success when a copy actually
 * happened.
 */
export async function copyToClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // Fall through to the legacy path below.
    }
  }

  try {
    const textarea = document.createElement('textarea')
    textarea.value = text
    textarea.style.position = 'fixed'
    textarea.style.top = '0'
    textarea.style.left = '0'
    textarea.style.opacity = '0'
    document.body.appendChild(textarea)
    textarea.focus()
    textarea.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(textarea)
    return ok
  } catch {
    return false
  }
}
