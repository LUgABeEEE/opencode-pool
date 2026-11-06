/**
 * dsh-plugin-opencode-pool — 宿主侧。
 *
 * 只做三件事：
 *   1. 保证中转进程活着（按需拉起 `python3 -m opencode_pool serve`，卸载时收掉）
 *   2. 把 `/api/opencode-pool/*` 反向代理到中转的 HTTP 面
 *   3. 代理时注入中转 token —— 浏览器永远看不到它
 *
 * 为什么要在 dsh-web 上代理而不是让浏览器直连 127.0.0.1:8790：
 * 面板可能从局域网打开（http://<lan-host>:3080），那时浏览器里的 127.0.0.1
 * 是**用户自己的电脑**，不是 dsh-web 所在的那台机器。经 dsh-web 转一次，局域网也能用。
 *
 * 路由形态取自 dsh-host-service-panel 的真实契约（`WebRoute` 只有 kind/path/handler，
 * 没有 method，动词检查写在 handler 第一行）。
 */
import { spawn } from 'node:child_process'
import { createWriteStream, existsSync, mkdirSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROUTE_BASE = '/api/opencode-pool'
const SLOT_ID = 'opencode-pool'

/** 允许透传的请求头（其余一律不转发，避免把浏览器的 cookie 带给中转）。 */
const FORWARD_HEADERS = ['content-type', 'accept', 'user-agent']
const PASS_RESPONSE_HEADERS = ['content-type', 'cache-control', 'x-request-id']

function expandHome(path) {
  if (!path) return ''
  return path.startsWith('~') ? join(homedir(), path.slice(1)) : path
}

/** 插件包自己所在的仓库根：pnpm 本地 link 时 ../.. 就是 opencode-pool/。 */
function inferProjectDir() {
  try {
    const here = dirname(fileURLToPath(import.meta.url))          // …/dsh-plugin/lib
    const root = join(here, '..', '..')                            // …/opencode-pool
    return existsSync(join(root, 'opencode_pool', '__main__.py')) ? root : ''
  } catch {
    return ''
  }
}

export function resolveConfig(raw = {}) {
  const projectDir = expandHome(raw.projectDir || process.env.OPENCODE_POOL_HOME || inferProjectDir())
  return {
    host: raw.host || '127.0.0.1',
    port: Number(raw.port || 8790),
    projectDir,
    python: raw.python || 'python3',
    autostart: raw.autostart !== false,
    configPath: expandHome(raw.configPath || '~/.dsh/opencode-pool/pool.config.json'),
    logPath: expandHome(raw.logPath || '~/.dsh/opencode-pool/relay.log'),
  }
}

/** 中转的 base url（宿主侧永远走 loopback）。 */
export function relayBase(cfg) {
  return `http://${cfg.host === '0.0.0.0' ? '127.0.0.1' : cfg.host}:${cfg.port}`
}

/**
 * 读中转配置里的 relay_token。
 *
 * 不做缓存：配置文件 1KB 上下，一次请求读一遍可以忽略不计，
 * 而任何缓存都会踩"同一毫秒重写、时间戳不变"的坑（面板连点换 key 就会）。
 */
export function createTokenReader(cfg) {
  return () => {
    try {
      return JSON.parse(readFileSync(cfg.configPath, 'utf8')).relay_token || ''
    } catch {
      return '' // 配置还没生成：按无 token 处理，中转自己会拒绝
    }
  }
}

async function healthy(cfg, timeoutMs = 1500) {
  try {
    const resp = await fetch(`${relayBase(cfg)}/healthz`, { signal: AbortSignal.timeout(timeoutMs) })
    return resp.ok
  } catch {
    return false
  }
}

/** 拉起中转；已活着就什么都不做。返回子进程或 null。 */
export async function ensureRelay(cfg, log = console) {
  if (await healthy(cfg)) return null
  if (!cfg.autostart) {
    log.warn?.('[opencode-pool] 中转没在跑，且 autostart 关闭')
    return null
  }
  if (!cfg.projectDir) {
    log.warn?.('[opencode-pool] 没配置 projectDir 且 OPENCODE_POOL_HOME 为空，无法自动拉起中转')
    return null
  }
  const entry = join(cfg.projectDir, 'opencode_pool', '__main__.py')
  if (!existsSync(entry)) {
    log.warn?.(`[opencode-pool] ${entry} 不存在 —— projectDir 指错了？`)
    return null
  }
  try {
    mkdirSync(dirname(cfg.logPath), { recursive: true, mode: 0o700 })
  } catch {
    /* 目录已存在 */
  }
  const out = createWriteStream(cfg.logPath, { flags: 'a' })
  const child = spawn(
    cfg.python,
    ['-m', 'opencode_pool', 'serve', '--config', cfg.configPath],
    { cwd: cfg.projectDir, env: process.env, stdio: ['ignore', 'pipe', 'pipe'], detached: false },
  )
  child.stdout.pipe(out)
  child.stderr.pipe(out)
  child.on('exit', (code, signal) => {
    log.warn?.(`[opencode-pool] 中转退出 code=${code} signal=${signal}`)
    try { out.end() } catch { /* 已关 */ }
  })
  for (let i = 0; i < 40; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 250))
    if (await healthy(cfg)) return child
    if (child.exitCode !== null) break
  }
  log.warn?.('[opencode-pool] 中转 10 秒内没起来，看看 ' + cfg.logPath)
  return child
}

// ── HTTP ────────────────────────────────────────────────────────────────
function sendJson(res, status, payload) {
  if (res.writableEnded) return
  const body = JSON.stringify(payload)
  res.statusCode = status
  res.setHeader('content-type', 'application/json; charset=utf-8')
  res.setHeader('cache-control', 'no-store')
  res.setHeader('x-content-type-options', 'nosniff')
  res.end(body)
}

async function readBody(req, limit = 1024 * 1024) {
  const chunks = []
  let size = 0
  for await (const chunk of req) {
    size += chunk.length
    if (size > limit) throw new Error('body too large')
    chunks.push(chunk)
  }
  return Buffer.concat(chunks)
}

function buildHeaders(req, token) {
  const headers = {}
  for (const name of FORWARD_HEADERS) {
    const value = req.headers[name]
    if (value) headers[name] = value
  }
  if (token) headers.authorization = `Bearer ${token}`
  return headers
}

/**
 * 造路由。返回 [{kind, path, handler}]，由调用方用 webServer.register 注册。
 * 一个 prefix 路由吃掉全部子路径：索引、面板 HTML、以及中转的每个 API。
 */
export function createRoutes({ cfg, getToken, log = console, ensure = ensureRelay }) {
  const base = relayBase(cfg)

  async function proxy(req, res, rest, body) {
    await ensure(cfg, log)
    const url = `${base}${rest}`
    let upstream
    try {
      upstream = await fetch(url, {
        method: req.method,
        headers: buildHeaders(req, getToken()),
        body: req.method === 'GET' || req.method === 'HEAD' ? undefined : body,
        redirect: 'manual',
      })
    } catch (error) {
      return sendJson(res, 502, {
        error: `中转连不上（${url}）：${error.message}`,
        hint: '确认 python3 -m opencode_pool serve 能起来，或把 autostart 打开',
      })
    }
    const buf = Buffer.from(await upstream.arrayBuffer())
    res.statusCode = upstream.status
    for (const name of PASS_RESPONSE_HEADERS) {
      const value = upstream.headers.get(name)
      if (value) res.setHeader(name, value)
    }
    if (!res.getHeader('content-type')) res.setHeader('content-type', 'application/octet-stream')
    res.setHeader('content-length', String(buf.length))
    res.end(buf)
  }

  return [
    {
      kind: 'prefix',
      path: ROUTE_BASE,
      handler: async (req, res) => {
        const url = new URL(req.url || '/', 'http://localhost')
        const rest = url.pathname.slice(ROUTE_BASE.length) || '/'

        // 索引：让面板/调试能看见插件状态，不必先碰中转
        if (rest === '/' || rest === '') {
          if (req.method !== 'GET') return sendJson(res, 405, { error: 'method not allowed' })
          return sendJson(res, 200, {
            plugin: 'dsh-plugin-opencode-pool',
            version: cfg.version || '0.1.0',
            relay: base,
            projectDir: cfg.projectDir || null,
            healthy: await healthy(cfg),
            routes: [`${ROUTE_BASE}/panel`, `${ROUTE_BASE}/v1/usage`, `${ROUTE_BASE}/v1/pool/schema`],
          })
        }
        const body = req.method === 'GET' || req.method === 'HEAD' ? undefined : await readBody(req)
        return proxy(req, res, rest + url.search, body)
      },
    },
  ]
}

/**
 * Cordis 挂载点。生命周期规则同 dsh-host-service-panel：
 * 子进程与路由都必须建在 `ctx.effect` 里，卸载时一起收掉。
 */
export default function mount(ctx, raw = {}, deps = {}) {
  const cfg = { ...resolveConfig(raw), version: deps.version }
  const log = ctx.logger || console
  const getToken = deps.getToken || createTokenReader(cfg)
  let child = null

  if (typeof ctx.effect === 'function') {
    ctx.effect(
      () => {
        // 不 await：挂载不该卡在健康检查上；第一次请求会再兜一次
        void (deps.ensure || ensureRelay)(cfg, log).then((proc) => { child = proc })
        return () => {
          try { child?.kill('SIGTERM') } catch { /* 已经没了 */ }
          child = null
        }
      },
      'opencode-pool: relay process',
    )
  }

  const routes = createRoutes({ cfg, getToken, log, ensure: deps.ensure })
  if (typeof ctx.inject === 'function') {
    ctx.inject(['webServer'], (webCtx) => {
      webCtx.effect(
        () => {
          const disposers = routes.map((route) => webCtx.webServer.register(route))
          log.info?.(`[opencode-pool] 注册了 ${routes.length} 条路由（${ROUTE_BASE}）`)
          return () => {
            for (const dispose of disposers) {
              try { dispose?.() } catch { /* 卸载不能抛 */ }
            }
          }
        },
        'opencode-pool: web routes',
      )
    })
  }

  return { routes, config: cfg, relayBase: relayBase(cfg) }
}

export { mount }
