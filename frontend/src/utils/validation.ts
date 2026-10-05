import { z } from 'zod'

// The application forbids dynamic JavaScript evaluation. Select Zod's
// interpreter before schemas are constructed instead of probing JIT support.
z.config({ jitless: true })

export { z }
