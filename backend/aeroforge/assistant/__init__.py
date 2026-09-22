"""AI 助手域（规格 §10.2 / OI-10 / §11 ④）。

三个职责各归一个模块：

- :mod:`aeroforge.assistant.config` —— ``base_url / api_key / model`` 三项的
  读写与脱敏（``data/assistant.json``）。key **只进不出**：任何响应、任何日志
  都不得出现明文（出边界只允许"作为 Authorization 头发给用户自己配置的
  base_url"——那正是 OI-10 的唯一联网用途）。
- :mod:`aeroforge.assistant.upstream` —— OpenAI 兼容 ``chat/completions``（SSE
  流式）的**代理客户端**。后端持有 key 直连用户配置的 ``base_url``，前端与
  key 之间隔着整个后端边界。
- ``aeroforge.api.assistant`` —— REST 端点 + ``/ws/assistant`` 对话流（协议
  形状见该模块 docstring）。

降级三态（§10.2 / R-35：三条**任一**成立即降级，不得只处理其中一条）由
API 层收口：未配置 / 断网超时 / 上游错误各自下发明确的 ``degraded`` 事件。
"""
