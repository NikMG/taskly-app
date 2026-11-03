import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { request } from './http'

beforeEach(() => vi.stubGlobal('fetch', vi.fn()))
afterEach(() => vi.unstubAllGlobals())

function respond(body: unknown, status: number) {
  vi.mocked(fetch).mockResolvedValue(
    new Response(typeof body === 'string' ? body : JSON.stringify(body), {
      status,
    }),
  )
}

it('prefixes the path, sends JSON headers and parses the body', async () => {
  respond({ ok: 1 }, 200)
  await expect(
    request('/todos', { headers: { 'X-Test': 'a' }, method: 'POST' }),
  ).resolves.toEqual({ ok: 1 })
  expect(fetch).toHaveBeenCalledWith('/api/todos', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Test': 'a' },
  })
})

it('resolves to undefined for 204', async () => {
  vi.mocked(fetch).mockResolvedValue(new Response(null, { status: 204 }))
  await expect(request('/todos/1')).resolves.toBeUndefined()
})

it('turns a network failure into a friendly message', async () => {
  vi.mocked(fetch).mockRejectedValue(new TypeError('boom'))
  await expect(request('/todos')).rejects.toThrow(
    'Cannot connect to the server. Please try again.',
  )
})

it('rethrows the original error when the request was aborted', async () => {
  const controller = new AbortController()
  controller.abort()
  const abortError = new DOMException('aborted', 'AbortError')
  vi.mocked(fetch).mockRejectedValue(abortError)
  await expect(request('/todos', { signal: controller.signal })).rejects.toBe(
    abortError,
  )
})

it('gives a fixed validation message for 422', async () => {
  respond({ detail: 'ignored' }, 422)
  await expect(request('/todos')).rejects.toThrow(/Check the task details/)
})

it('surfaces a string detail from an error response', async () => {
  respond({ detail: 'Task not found' }, 404)
  await expect(request('/todos/9')).rejects.toThrow('Task not found')
})

it('falls back when the error detail is not a string', async () => {
  respond({ detail: [{ msg: 'x' }] }, 400)
  await expect(request('/todos')).rejects.toThrow(
    'The operation failed. Please try again.',
  )
})

it('falls back when the error body is not JSON', async () => {
  respond('<html>', 500)
  await expect(request('/todos')).rejects.toThrow(
    'The operation failed. Please try again.',
  )
})
