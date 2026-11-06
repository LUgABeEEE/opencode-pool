# 私有适配器扩展点

把实现 `Adapter` 的 `.py` 丢进这个目录，启动时自动加载（文件名不以 `_` 开头）。

两种写法都行：

```python
# 写法一：暴露 ADAPTER
from ..base import Adapter, Field, WindowSpec, ErrorDecision
ADAPTER = MyAdapter()

# 写法二：暴露 register()
def register(register):
    register(MyAdapter())
```

约定：
- `id` 必须唯一；配置里 `providers.<pid>.adapter` 写它
- 有余量查询就实现 `probe()` 并置 `supports_quota = True`，否则调度退化为轮询 + 冷却
- 失败处置写在 `classify()` 里，调度器只执行不判断
- 字段与窗口用 `Field` / `WindowSpec` 声明，面板与 `/v1/pool/schema` 会自动读
