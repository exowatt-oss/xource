import { afterEach, expect, test, vi } from 'vitest'
import { api } from './App'

afterEach(() => vi.unstubAllGlobals())

test('API fails closed on missing identity', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 401 }))
  await expect(api('/spaces')).rejects.toThrow('Sign in through your configured identity provider')
})

test('API requests same-origin authentication, not a fixed private endpoint', async () => {
  const fetcher = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => [] })
  vi.stubGlobal('fetch', fetcher)
  await expect(api('/spaces')).resolves.toEqual([])
  expect(fetcher).toHaveBeenCalledWith('/api/spaces', { credentials: 'same-origin' })
})
