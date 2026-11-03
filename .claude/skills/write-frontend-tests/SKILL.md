---
name: write-frontend-tests
description: Use when adding or updating Vitest and React Testing Library tests under frontend/src. Gives this repository's test setup, design-system mock, fixture and patterns for components, query hooks, fetch and timers.
---

# Write frontend tests

Before writing, read the nearest existing test and match it. Good references: `src/features/todos/components/TodoItem.test.tsx`, `src/features/todos/api/todos.test.ts`, `src/components/SuccessNotice.test.tsx`.

## Conventions

- Import explicitly; there are no globals: `import { beforeEach, expect, it, vi } from 'vitest'`.
- Colocate as `X.test.tsx` beside `X.tsx`, and `x.test.ts` beside `x.ts`. Name tests with `it('…')` and a behaviour sentence, usually without `describe`.
- `src/test/setup.ts` already loads jest-dom matchers and runs `cleanup` after each test. Do not repeat that.
- Style follows Prettier in `.prettierrc.json`: no semicolons, single quotes, two spaces, trailing commas, 80 columns. CI runs `format:check` over `src`, so match it by hand.
- Tests are type-checked in CI with `tsc --noEmit`. Mock returns use the existing cast, as in `as unknown as ReturnType<typeof useTodosQuery>`.

## Rendering components

Existing component tests use **no theme provider and no `AppProviders`**. They mock the design system instead, so `styled-components` never needs a theme:

```tsx
vi.mock('../../../design-system', () => import('../../../design-system/mocks'))
```

The path is relative to the test, and the factory must be this async `import`, because `vi.mock` is hoisted. `design-system/mocks.tsx` gives each component accessible native markup (`Button` is a `button`, `Input` a labelled `input`, `Drawer` a `role="dialog"`, `Alert` has `data-testid="alert"`) and exposes each as a `vi.fn()`.

When a prop is the behaviour and is not visible in the DOM, assert it the way `SuccessNotice.test.tsx` does:

```tsx
expect(vi.mocked(Alert).mock.calls[0][0]).toEqual(
  expect.objectContaining({ variant: 'success' }),
)
```

Add `beforeEach(() => vi.clearAllMocks())` whenever a test inspects those mock calls.

Never test `design-system/` components themselves; they are excluded from coverage. Mock child components only for page-level composition, as `TodosPage.test.tsx` and `App.test.tsx` do.

## The patterns below show how, not what

The sections below cover setups that are awkward to get right. They do not decide what to test: that was decided by `analyze-test-impact` in pr mode or `find-coverage-gaps` in backfill mode, and this skill does not widen it. A target of a kind not mentioned here is still tested, by reading it and its nearest existing test. Cover the target's branches.

## Queries and interaction

- Query by what a user perceives: `getByRole` with `name` first, then `getByLabelText`, then `getByText`. Use `getByTestId` only for the `alert` the mock provides. Use `within(...)` to scope.
- Use user-event: `const user = userEvent.setup()` at the start of the test, then `await user.click(...)` or `await user.type(...)`. Do not use `fireEvent`.
- For async UI use `await screen.findBy…` or `await waitFor(...)`, and `queryBy…` to assert absence.
- Use `todoFixture` from `src/test/todoFixture.ts`, and vary it with a spread: `{ ...todoFixture, completed: true }`. Do not invent task shapes. Add new shared fixtures under `src/test/`.

## Mocking

- Mock at the module boundary below the unit, with paths relative to the test: `vi.mock('../../../lib/http', () => ({ request: vi.fn() }))` then `vi.mocked(request).mockResolvedValue(...)`.
- For `lib/http.ts` itself, stub fetch and clean up:

```ts
beforeEach(() => vi.stubGlobal('fetch', vi.fn()))
afterEach(() => vi.unstubAllGlobals())
```

Build responses with `new Response(JSON.stringify(body), { status })`. Behaviours in `request`: a network failure becomes the friendly message, an aborted signal rethrows the original error, `422` gives the fixed validation message, a string `detail` is surfaced, a non-JSON error body falls back, and `204` resolves to `undefined`.

- Environment values go through `vi.stubEnv`; never touch `process.env`.

## Query hooks

Use `renderHook` with a fresh client per test and retries off, so errors surface immediately. Do not import the shared `queryClient` from `lib/queryClient.ts`:

```tsx
function setup() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  return { client, wrapper }
}
```

The file needs the `.tsx` extension for this wrapper. Mock `../api/todos` with `vi.mock`, seed with `client.setQueryData(todoKeys.list, [...])`, run `await act(() => result.current.mutateAsync(input))` or `await waitFor(() => expect(result.current.isSuccess).toBe(true))`, and assert the observable result with `client.getQueryData(todoKeys.list)`. For mutations that means the cache contents after success, such as the saved task first with no duplicate id, and that a failure leaves the cache unchanged.

## Timers

`useNotice` clears its message after 4000 ms, and each new notice restarts the timer. Use fake timers and wrap state changes in `act`:

```ts
beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

act(() => result.current.notify('Saved'))
act(() => vi.advanceTimersByTime(3999))
expect(result.current.message).toBe('Saved')
act(() => vi.advanceTimersByTime(1))
expect(result.current.message).toBe('')
```

Combined with user-event, use `userEvent.setup({ advanceTimers: vi.advanceTimersByTime })`, or `findBy` and `waitFor` will hang.

## Do not

- No snapshot tests: no `toMatchSnapshot` or `toMatchInlineSnapshot`.
- No assertions on implementation details: internal state, class names, generated styled-components CSS, hook call counts, or `container.querySelector`. `toBeEmptyDOMElement` on the container is fine for "renders nothing".

## The patch guard rejects these

`check_patch.py` fails the whole patch on these, so avoid them from the start:

- Files other than `*.test.ts`, `*.test.tsx` and `src/test/**`.
- `.only(`, `.skip(`, `.skipIf(`, `.runIf(`, `.todo(`, `.fails(`, and `xit`, `fit`, `xdescribe`, `fdescribe`, including chained and bracket forms.
- `@ts-ignore`, `@ts-expect-error`, `@ts-nocheck`.
- `process.env`, `child_process`, `XMLHttpRequest`, `eval(`, and `fetch('http…')` with a real URL.
- Assertions on constants such as `expect(true).toBe(true)`, and new test files with no assertions.
- Removing more assertions than you add.

## Before you finish a test

Ask how this test fails if the behaviour breaks. A test that only proves a mock returned its mocked value proves nothing.

Do not assert on behaviour you believe is a bug; report it instead.

Then run it with `run-sandboxed-tests`.
