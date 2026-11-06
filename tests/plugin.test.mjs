/** DSH 插件的纯逻辑测试（不联网、不起服务）：node --test tests/ */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { createRoutes, createTokenReader, resolveConfig, mount } from '../dsh-plugin/lib/index.js'

const cfg = resolveConfig({ port: 1, autostart: false, projectDir: '/tmp/nowhere' })

test('配置默认值与会展开 ~', () => {
  assert.equal(cfg.port, 1)
  assert.equal(cfg.autostart, false)
  assert.ok(cfg.configPath.startsWith('/'), 'configPath 应展开成绝对路径')
  assert.ok(!cfg.configPath.startsWith('~'))
  assert.equal(resolveConfig({}).port, 8790)
  assert.equal(resolveConfig({}).autostart, true)
})

test('token 从配置文件读，改过之后能重新读到', () => {
  const dir = mkdtempSync(join(tmpdir(), 'pool-plugin-'))
  const path = join(dir, 'pool.config.json')
  writeFileSync(path, JSON.stringify({ relay_token: 'a' }))
  const read = createTokenReader({ configPath: path })
  assert.equal(read(), 'a')
  writeFileSync(path, JSON.stringify({ relay_token: 'b' }))
  assert.equal(read(), 'b', 'mtime 变了就该重读')
})

test('只注册一条 prefix 路由，且索引不吃 POST', async () => {
  const routes = createRoutes({ cfg, getToken: () => '', log: { info() {}, warn() {} } })
  assert.equal(routes.length, 1)
  assert.equal(routes[0].kind, 'prefix')
  assert.equal(routes[0].path, '/api/opencode-pool')

  const res = fakeRes()
  await routes[0].handler({ method: 'POST', url: '/api/opencode-pool', headers: {} }, res)
  assert.equal(res.statusCode, 405)

  const ok = fakeRes()
  await routes[0].handler({ method: 'GET', url: '/api/opencode-pool', headers: {} }, ok)
  assert.equal(ok.statusCode, 200)
  const body = JSON.parse(ok.body)
  assert.equal(body.plugin, 'dsh-plugin-opencode-pool')
  assert.equal(body.healthy, false, '端口 1 上没有中转，应为 false')
})

test('mount 把路由注册进 webServer，卸载可回滚', () => {
  const registered = []
  const disposers = []
  const ctx = {
    logger: { info() {}, warn() {} },
    effect: (fn) => { const d = fn(); if (typeof d === 'function') disposers.push(d) },
    inject: (deps, cb) => cb({
      effect: (fn) => { const d = fn(); if (typeof d === 'function') disposers.push(d) },
      webServer: { register: (r) => { registered.push(r); return () => {} } },
    }),
  }
  const api = mount(ctx, { port: 1, autostart: false }, {})
  assert.equal(registered.length, 1)
  assert.equal(api.routes.length, 1)
  for (const dispose of disposers) dispose()
})

test('客户端模块 id 必须是 npm 包名（2026-11-04 踩过：写成 cordis 条目 id 会让整页 boot 卡死）', () => {
  const pkg = JSON.parse(readFileSync(new URL('../dsh-plugin/package.json', import.meta.url), 'utf8'))
  const client = readFileSync(new URL('../dsh-plugin/lib/client.js', import.meta.url), 'utf8')
  const id = client.match(/__ModuleLoader__\.load\(\{\s*id:\s*'([^']+)'/)?.[1]
  assert.ok(id, 'client.js 里找不到 __ModuleLoader__.load({ id })')
  assert.equal(
    id,
    pkg.name,
    'loader 用这个 id 在注册表里找回包；它必须是包名，不是 cordis 条目 id / slot id',
  )
  assert.ok(client.includes('__ModuleLoader__.load('), '必须通过 __ModuleLoader__.load 自注册')
})

function fakeRes() {
  const out = { statusCode: 0, headers: {}, body: '', done: false }
  out.setHeader = (k, v) => { out.headers[k.toLowerCase()] = v }
  out.getHeader = (k) => out.headers[k.toLowerCase()]
  out.end = (b) => { out.body = b ? b.toString() : ''; out.done = true }
  Object.defineProperty(out, 'writableEnded', { get: () => out.done })
  return out
}
