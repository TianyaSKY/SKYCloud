import { expect, it, vi } from 'vitest'
import { loginSchema, registerSchema } from '@/schemas/auth'

it('validates login and registration without probing or compiling dynamic JavaScript', () => {
  const compile = vi.spyOn(globalThis, 'Function').mockImplementation(() => { throw new Error('CSP denied') })
  try {
    expect(loginSchema.safeParse({ username: 'user', password: 'password' }).success).toBe(true)
    expect(loginSchema.safeParse({ username: '', password: '' }).success).toBe(false)
    expect(registerSchema.safeParse({ username: 'user', password: 'password', confirmPassword: 'different' }).success).toBe(false)
    expect(compile).not.toHaveBeenCalled()
  } finally { compile.mockRestore() }
})
