import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import { AccountGate } from '../app/AccountGate'
import { ApiError, parseError } from '../services/api'
import { auth } from '../services/auth'

describe('error bodies', () => {
  it('reads the Phase 9 format and the earlier ones', () => {
    const e = parseError(403, 'Forbidden', { detail: { code: 'PERMISSION_DENIED', message: 'Permission user.manage required' }, code: 'PERMISSION_DENIED', message: 'Permission user.manage required', request_id: 'r1' })
    expect([e.status, e.code, e.message, e.requestId]).toEqual([403, 'PERMISSION_DENIED', 'Permission user.manage required', 'r1'])
    expect(parseError(404, 'Not Found', { detail: 'Unknown device' }).message).toBe('Unknown device')
    expect(parseError(422, 'Unprocessable', { detail: [{ loc: ['body'], msg: 'x' }] }).message).toBe('The request was not valid')
    expect(parseError(500, 'Server Error', null).message).toBe('Server Error')
  })
})

describe('OIDC redirect token', () => {
  it('stores the fragment token and removes it from the address bar', () => {
    const exp = Math.floor(Date.now() / 1000) + 600
    const payload = btoa(JSON.stringify({ exp })).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_')
    history.replaceState(null, '', `/#access_token=h.${payload}.s`)
    expect(auth.pickUpRedirectToken()).toBe(true)
    expect(auth.jwt()).toBe(`h.${payload}.s`)
    expect(location.hash).toBe('#/overview')
    auth.clear()
  })

  it('ignores anything that is not exactly a token fragment', () => {
    history.replaceState(null, '', '/#/fleet')
    expect(auth.pickUpRedirectToken()).toBe(false)
    history.replaceState(null, '', '/#access_token=a b<script>')
    expect(auth.pickUpRedirectToken()).toBe(false)
    expect(auth.jwt()).toBeNull()
  })
})

vi.mock('../services/api', async (orig) => {
  const real = await orig<typeof import('../services/api')>()
  return { ...real, api: { login: vi.fn(), setup: vi.fn(), signInProviders: vi.fn() } }
})

describe('sign-in with an authenticator', () => {
  it('asks for a code when the account has MFA, then signs in with it', async () => {
    const { api } = await import('../services/api')
    vi.mocked(api.login)
      .mockRejectedValueOnce(new ApiError(401, 'Enter the code from your authenticator app', 'MFA_CODE_REQUIRED'))
      .mockResolvedValueOnce({ access_token: 't', expires_at: '2030-01-01T00:00:00Z', user: {} as never })
    const onSession = vi.fn()
    render(<AccountGate setup={false} tokenRequired={false} onSession={onSession} />)
    fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'alice' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'pw' } })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    const code = await screen.findByLabelText('Authenticator code')
    fireEvent.change(code, { target: { value: '12a3456' } })
    expect((code as HTMLInputElement).value).toBe('123456') // digits only
    fireEvent.click(screen.getByRole('button', { name: 'Verify code' }))
    await waitFor(() => expect(onSession).toHaveBeenCalledWith('t', '2030-01-01T00:00:00Z'))
    expect(api.login).toHaveBeenLastCalledWith('alice', 'pw', '123456')
  })
})
