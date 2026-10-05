/** Opt-in manual browser verification of production preview components/CSP.
 * Usage: node scripts/preview-browser.mjs [optional fixture directory]
 * Keep it running while checking each preview; terminate to clean artifacts.
 */
import { mkdtempSync, readFileSync, writeFileSync, rmSync, existsSync, realpathSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { resolve, dirname, extname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'node:http'
import { build, loadConfigFromFile } from 'vite'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const temporary = realpathSync(mkdtempSync(join(tmpdir(), 'skycloud-preview-browser-')))
const fixtures = process.argv[2] ? resolve(process.argv[2]) : join(temporary, 'fixtures')
if (!process.argv[2]) {
  mkdirSync(fixtures)
  const samples = JSON.parse(readFileSync(join(root, 'scripts/preview-fixtures.json'), 'utf8'))
  for (const [name, bytes] of Object.entries(samples)) writeFileSync(join(fixtures, name), Buffer.from(bytes, 'base64'))
}
for (const file of ['sample.pdf', 'sample.docx', 'sample.xlsx', 'sample.md']) {
  if (!existsSync(join(fixtures, file))) throw new Error(`Missing ${file}`)
}
const { config } = await loadConfigFromFile({ command: 'build', mode: 'production' }, join(root, 'vite.config.ts'))
const entry = join(temporary, 'entry.js')
writeFileSync(entry, `
import { createApp, ref, h } from ${JSON.stringify(root + '/node_modules/vue/dist/vue.runtime.esm-bundler.js')};
import Arco from ${JSON.stringify(root + '/node_modules/@arco-design/web-vue/es/index.js')};
import ${JSON.stringify(root + '/node_modules/@arco-design/web-vue/dist/arco.css')};
import Preview from ${JSON.stringify(root + '/src/components/FilePreview.vue')};
const types = {pdf:'pdf',docx:'docx',xlsx:'excel',md:'markdown'};
createApp({setup(){const visible=ref(false),url=ref(''),type=ref(''),title=ref(''),text=ref('');
  const violations=ref([]);document.addEventListener('securitypolicyviolation',e=>violations.value.push(e.violatedDirective+' '+e.blockedURI+' '+e.sourceFile+':'+e.lineNumber+':'+e.columnNumber));
  async function open(ext){visible.value=false;if(url.value)URL.revokeObjectURL(url.value);
    const blob=await fetch('/fixtures/sample.'+ext).then(r=>r.blob());url.value=URL.createObjectURL(blob);
    type.value=types[ext];title.value='sample.'+ext;text.value=ext==='md'?await blob.text():'';visible.value=true;}
  return()=>h('main',[h('h1','SKYCloud preview / production CSP'),...Object.keys(types).map(ext=>h('button',{onClick:()=>open(ext)},'Open '+ext)),
    h('p',{id:'violations'},'CSP violations: '+violations.value.length),h('pre',violations.value.join('\\n')),
    h(Preview,{visible:visible.value,title:title.value,url:url.value,type:type.value,textContent:text.value,
      'onUpdate:visible':value=>visible.value=value,onClose:()=>{if(url.value)URL.revokeObjectURL(url.value);url.value='';}})]);
}}).use(Arco).mount('#app');`)
writeFileSync(join(temporary, 'index.html'), '<!doctype html><meta charset="utf-8"><title>SKYCloud preview verification</title><div id="app"></div><script type="module" src="./entry.js"></script>')
try {
  await build({ ...config, configFile: false, root: temporary,
    resolve: { alias: { '@': root + '/src', vue: root + '/node_modules/vue/dist/vue.runtime.esm-bundler.js' } },
    // The harness uses actual Vue preview components and asset plugin.
    plugins: config.plugins.filter(plugin => !['unplugin-auto-import', 'unplugin-vue-components'].includes(plugin?.name)),
    build: { outDir: join(temporary, 'dist'), emptyOutDir: true } })
  const nginx = readFileSync(join(root, 'nginx.conf'), 'utf8')
  const policies = [...nginx.matchAll(/add_header Content-Security-Policy "([^"]+)"/g)].map(match=>match[1])
  const parentPolicy = policies.find(policy=>policy.includes('frame-ancestors')).replace('${RUNTIME_UI_CSP_SOURCE}', 'http://*.skycloud.localhost:8081')
  const workerPolicy = policies.find(policy=>policy.includes('wasm-unsafe-eval'))
  const server = createServer((request,response)=>{
    const path = new URL(request.url, 'http://localhost').pathname
    const base = path.startsWith('/fixtures/') ? fixtures : join(temporary, 'dist')
    const relative = path.startsWith('/fixtures/') ? path.slice(10) : path === '/' ? 'index.html' : path.slice(1)
    const file = resolve(base, relative)
    if (!file.startsWith(base + '/') || !existsSync(file)) { response.writeHead(404);response.end();return }
    const mime={'.html':'text/html','.js':'application/javascript','.mjs':'application/javascript','.css':'text/css','.wasm':'application/wasm','.pdf':'application/pdf'}[extname(file)] || 'application/octet-stream'
    response.setHeader('Content-Type',mime)
    response.setHeader('Content-Security-Policy', /^\/assets\/pdf\.worker-[A-Za-z0-9_-]+\.mjs$/.test(path)?workerPolicy:parentPolicy)
    response.setHeader('X-Content-Type-Options','nosniff')
    response.end(readFileSync(file))
  })
  await new Promise(done=>server.listen(0,'127.0.0.1',done))
  console.log('BROWSER_URL=http://127.0.0.1:'+server.address().port)
  for(const signal of ['SIGINT','SIGTERM']) process.once(signal,()=>{server.close();rmSync(temporary,{recursive:true,force:true});process.exit(0)})
} catch(error) { rmSync(temporary,{recursive:true,force:true});throw error }
