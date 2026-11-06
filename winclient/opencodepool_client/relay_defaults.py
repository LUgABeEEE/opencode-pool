"""中转配置里"客户端会显示/会改"的那几个字段的上游默认值。

单独一个文件是为了让默认值与 `opencode_pool/config.py::Config` 保持一眼可对照，
而不是散在设置页的控件初值里（那种地方永远会漂）。
上游 `Config` 改了默认值，只改这里一处。
"""
from __future__ import annotations

# 只列客户端关心的字段；没列出的键原样保留，不碰
CFG_DEFAULTS = {
    "min_headroom": 2.0,             # 余量水位线：低于它就不接单
    "max_inflight": 1,               # 每号并发
    "min_interval_seconds": 1.2,     # 每号最小请求间隔
    "queue_wait_seconds": 60.0,      # 全池限流时的排队上限
    "usage_refresh_seconds": 90,     # 额度采样周期
    "sticky_ttl_seconds": 43200,     # 会话粘性 TTL（12h），只是安全上界
    "upstream_timeout": 900,         # 上游超时
    "request_log_path": "~/.dsh/opencode-pool/requests.jsonl",
    "request_log_max_bytes": 20971520,
    "request_log_keep": 3,
    "version": 2,
}

# 设置页用的中文标签与范围（min/max/step 用于校验，不是装饰）
FIELD_META = {
    "min_headroom": ("余量水位线", "余量（100−各窗口占用百分比的较小值）低于它就不给这个号派单。0 = 只要没打满就可用。", 0.0, 100.0, 0.5),
    "max_inflight": ("每号并发", "同一账号同时最多几个请求在飞。", 1, 32, 1),
    "min_interval_seconds": ("每号最小间隔", "同一账号两次请求之间的最小间隔（秒）。", 0.0, 120.0, 0.1),
    "queue_wait_seconds": ("排队等待上限", "全池都没空位时先排队多久，超时才回 503。", 0.0, 600.0, 1.0),
    "usage_refresh_seconds": ("额度采样周期", "多久去上游 /v1/usage 刷一次三档额度（秒）。", 15, 3600, 5),
    "sticky_ttl_seconds": ("会话粘性 TTL", "同一对话钉在同一账号多久（秒）；只是安全上界，超容量按 LRU 淘汰。", 0, 604800, 600),
    "upstream_timeout": ("上游超时", "单次上游请求的超时（秒），深度思考别调太小。", 30, 3600, 30),
    "request_log_max_bytes": ("请求日志单文件上限", "超过就轮转（字节）。", 1048576, 268435456, 1048576),
    "request_log_keep": ("请求日志保留份数", "轮转后保留几个历史文件。", 0, 20, 1),
}
