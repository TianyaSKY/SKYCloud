/** Actual frontend utility against the isolated API/PostgreSQL/MinIO browser fixture. */
import { build } from 'esbuild'
import { mkdtemp, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawn } from 'node:child_process'
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const temporary = await mkdtemp(resolve(tmpdir(), 'skycloud-upload-http-'))
try {
  const stub = resolve(temporary, 'ui.mjs')
  await writeFile(stub, 'export const Message={error(){}};export default {currentRoute:{value:{name:"home"}},push(){}};')
  const output = resolve(temporary, 'check.cjs')
  await build({ stdin: { resolveDir: root, loader: 'ts', contents: `
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { File } from 'node:buffer'
import { createHash } from 'node:crypto'
import { createPinia, setActivePinia } from 'pinia'
import { useAuthStore } from './src/stores/auth'
import { useWorkspaceStore } from './src/stores/workspace'
import request from './src/api/request'
import { apiSuccessData } from './src/utils/apiSuccess'
import { uploadFileOptimized } from './src/utils/upload'
async function main() {
  const fixture = JSON.parse(readFileSync(process.argv[2], 'utf8'))
  const base = 'http://127.0.0.1:' + new URL(fixture.url).port + '/api'
  const login = await fetch(base + '/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json', Origin: fixture.url }, body: JSON.stringify({username: fixture.username, password: fixture.password}) })
  assert.equal(login.status, 200, 'Synthetic fixture login failed')
  const session = apiSuccessData(await login.json())
  setActivePinia(createPinia())
  useAuthStore().setAuth(session.token, session.user)
  useWorkspaceStore().currentWorkspace = {id: fixture.workspace_id, name: 'fixture', owner_id: session.user.id, my_role: 'admin'}
  request.defaults.baseURL = base
  request.defaults.headers.common.Origin = fixture.url
  const parent = (await request.get('/folder/root_id')).root_folder_id
  const runTag = Date.now()
  const large = Buffer.alloc(21 * 1024 * 1024 + 17)
  large.write(String(runTag), large.length - 17)
  for (const [name, bytes] of [ ['http-small-' + runTag + '.txt', Buffer.from('Actual frontend upload via HTTP ' + runTag + '\\n')], ['http-large-' + runTag + '.bin', large] ]) {
    const file = new File([bytes], name, {lastModified: 123})
    Object.defineProperty(file, 'arrayBuffer', {value: () => {throw new Error('Whole-file read refused')}})
    const progress = []
    assert.equal((await uploadFileOptimized(file, parent, value => progress.push(value))).instantUpload, false)
    assert.equal(progress.at(-1), 100)
    assert.ok(progress.slice(0,-1).every(value => value < 100))
    const listed = await request.get('/files/list', {params: {parent_id: parent}})
    const records = Array.isArray(listed.files) ? listed.files : listed.files?.items || listed.items || []
    const record = records.find(item => item.name === name)
    assert.ok(record, 'Uploaded file missing from SQL listing')
    const downloaded = await request.get('/files/' + record.id + '/download', {responseType: 'arraybuffer'})
    assert.equal(createHash('sha256').update(downloaded).digest('hex'), createHash('sha256').update(bytes).digest('hex'))
    assert.equal((await uploadFileOptimized(file, parent)).instantUpload, true, 'Committed content should support instant reuse')
    console.log(JSON.stringify({name, size: bytes.length, upload: 'passed', download_sha256: 'matched', instant_reuse: 'passed'}))
  }
}
main().catch(error => {console.error(error.message); process.exitCode = 1})
` }, bundle: true, platform: 'node', format: 'cjs', outfile: output, alias: { '@': resolve(root, 'src') },
    define: { 'import.meta.env.DEV': 'false', 'import.meta.env.PROD': 'true' },
    plugins: [{ name: 'headless-ui-only', setup(builder) {
      builder.onResolve({ filter: /^@arco-design\/web-vue$/ }, () => ({ path: stub }))
      builder.onResolve({ filter: /^\.\.\/router$/ }, () => ({ path: stub }))
    } }],
  })
  if (process.argv.includes('--build-only')) {
    console.log('Upload HTTP harness compiled')
  } else {
  const code = await new Promise((accept, reject) => {
    const child = spawn(process.execPath, [output, process.argv[2] || '/private/tmp/skycloud-full-browser.json'], { stdio: 'inherit' })
    const timer = setTimeout(() => child.kill('SIGTERM'), 60000)
    child.on('error', error => { clearTimeout(timer); reject(error) })
    child.on('close', code => { clearTimeout(timer); accept(code) })
  })
  process.exitCode = code ?? 1
  }
} finally { await rm(temporary, { recursive: true, force: true }) }
