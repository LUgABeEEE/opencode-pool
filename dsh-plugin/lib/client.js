/**
 * dsh-plugin-opencode-pool — 浏览器侧。
 *
 * 只做一件事：把中转自带的控制台装进 DSH 的侧边栏 + 主面板。
 * 面板本身仍是 `opencode_pool/panel/panel.html`（Python 侧一份、两个入口共用），
 * 这里用 iframe 指向经 dsh-web 代理的地址，所以：
 *   - 局域网打开也能用（不会被用户自己电脑的 127.0.0.1 坑到）
 *   - token 在宿主侧注入，浏览器拿不到
 *
 * 约束：只用 platform 模块（react），无副作用，注册全部走 ctx.effect。
 */
window.__ModuleLoader__.load({
  id: 'dsh-plugin-opencode-pool',

  factory(require) {
    const React = require('react')
    const h = React.createElement

    /** 侧边栏 id 与主面板 key 必须一致：外壳靠它把点击和面板连起来。 */
    const SLOT_ID = 'opencode-pool'
    /** 文档相对路径：DSH 可能部署在子路径下，根绝对路径会逃出前缀。 */
    const PANEL_SRC = 'api/opencode-pool/panel'
    const LABEL = 'OpenCode 号池'

    function Glyph(props) {
      return h(
        'svg',
        {
          viewBox: '0 0 24 24',
          width: 18,
          height: 18,
          fill: 'none',
          stroke: 'currentColor',
          strokeWidth: 1.8,
          strokeLinecap: 'round',
          'aria-hidden': true,
          ...props,
        },
        h('circle', { cx: 12, cy: 12, r: 3.2 }),
        h('path', { d: 'M12 3.2a8.8 8.8 0 0 1 8.5 6.6' }),
        h('path', { d: 'M20.5 12a8.8 8.8 0 0 1-6.1 8.4' }),
        h('path', { d: 'M12 20.8A8.8 8.8 0 0 1 3.5 14.2' }),
        h('path', { d: 'M3.5 12a8.8 8.8 0 0 1 6.1-8.4' }),
      )
    }

    function Panel() {
      return h('iframe', {
        src: PANEL_SRC,
        title: LABEL,
        style: { width: '100%', height: '100%', border: '0', display: 'block', background: 'transparent' },
        // 面板只读同源 API，不需要任何额外权限
        sandbox: undefined,
      })
    }

    return {
      inject: ['slots'],

      apply(ctx) {
        ctx.slots.inject('sidebar.panellist', () =>
          ctx.slots.register(
            { name: 'sidebar.panellist', id: SLOT_ID, order: 93, label: () => LABEL },
            Glyph,
          ),
        )
        ctx.slots.inject('main', () =>
          ctx.slots.register({ name: 'main', key: SLOT_ID }, Panel),
        )
      },
    }
  },
})
