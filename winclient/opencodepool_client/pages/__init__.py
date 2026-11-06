"""五个页面模块。每个页面只依赖 `app` 暴露的几个接口：

    app.snap / app.schema / app.healthy / app.client / app.settings
    app.toast(text, kind) / app.run_async(label, fn, on_done) / app.confirm(title, msg)

页面实现（可选）`on_show()` / `on_snapshot(snap)` / `on_status(healthy, text)` / `on_tick()`。
"""
