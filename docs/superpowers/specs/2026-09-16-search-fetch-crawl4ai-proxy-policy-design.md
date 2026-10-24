# Search and Fetch 两阶段网络出口与 Crawl4AI 运行时配置 PRD

> 状态：Draft，等待用户评审
>
> 日期：2026-09-16
>
> 适用项目：`/home/zk/Project/LLM-deployment`

## 1. 摘要

本 PRD 规范 `search_and_fetch` 的两阶段网页获取链路：

1. Stage 1：使用独立 curl 抓取网页。
2. Stage 2：Stage 1 失败、被拦截、正文为空或正文质量不足时，使用 Crawl4AI 浏览器抓取。

本次目标是统一搜索引擎出站代理策略、让 Crawl4AI 正确继承可选的抓取代理、升级并固定 Crawl4AI 依赖版本约束，以及把 Stage 1 的正文质量阈值从代码常量改成 YAML 配置。

本次不切换到 curl-cffi，不调整任何并发上限，不把 SearXNG 从 `latest` 改为固定版本，也不引入 CAPTCHA solver 或新的前端协议。

## 2. 背景与问题判断

### 2.1 当前仓库事实

- [config/config-agent-operation.yaml](/home/zk/Project/LLM-deployment/config/config-agent-operation.yaml) 当前配置 `fetch_transport: curl`，`fetch_fallback_transport: crawl4ai`。
- 当前 `fetch_proxy: null`，表示 backend 的直接网页抓取默认不使用代理。
- SearXNG 的国外引擎在 [deploy/searxng/core-config/settings.yml](/home/zk/Project/LLM-deployment/deploy/searxng/core-config/settings.yml) 中配置了代理；国内引擎使用空代理字符串表达直连。
- Crawl4AI runtime 在 [backend/tools/fetch_worker_runtime.py](/home/zk/Project/LLM-deployment/backend/tools/fetch_worker_runtime.py) 创建 `BrowserConfig` 时，目前只传入浏览器类型、headless 和日志选项，没有传入 `proxy_config`。
- Stage 1 的正文质量检查位于 [backend/tools/web_tools.py](/home/zk/Project/LLM-deployment/backend/tools/web_tools.py) 的 `_looks_like_low_quality_text()`，当前硬编码了 `1000` 字符和 `5` 行边界。
- 现有 `fetch_min_text_chars: 200` 是正文候选提取阶段的阈值，不是 Stage 1 最终成功判定的 1000 字阈值，不能直接复用来表达两个不同语义。
- 当前本地环境检测到 `crawl4ai 0.9.0`、`playwright 1.61.0` 和 `curl-cffi 0.15.0`。curl-cffi 不在本次研究和实现范围内。
- SearXNG Compose 使用服务名 `valkey`，因此 `redis://valkey:6379/0` 与当前 Compose 网络拓扑一致；Valkey 地址不是本次成功率问题的默认根因。
- SearXNG 使用 `SEARXNG_VERSION=latest`，这是本次明确保留的策略。运行版本、镜像 digest 和实际引擎列表需要进入运行时观测，以便解释动态变化。

### 2.2 独立研究结论

#### 代理键格式

SearXNG 官方文档推荐 `all://` 或规范化的 `http` / `https` 键。当前上游源码仍能处理 `http://` / `https://` 形式，因此当前写法不能被判定为“代理必然失效”，但应改成文档风格，降低版本敏感性。

#### 空代理字符串

当前 SearXNG/curl 组合中，空代理字符串通常表示禁用代理；不能把它断言为必然的 Invalid URL。但是空字符串不是清晰的策略表达，且跨客户端、跨版本的含义不够稳定。本 PRD 将改为“全局不设置代理，国外引擎按引擎显式设置代理，国内引擎不设置代理”。

#### 引擎稳定性

Google、DuckDuckGo、Brave 等公共引擎存在持续的 403、CAPTCHA、限流和解析器变化风险。上游社区的 [Google 403 issue #5867](https://github.com/searxng/searxng/issues/5867) 和 [major engines discussion #5651](https://github.com/searxng/searxng/discussions/5651) 支持“不要把单一公共引擎当作稳定依赖”的判断，但不支持简单地永久关闭所有国外引擎。

#### Crawl4AI 版本与 stream 生命周期

Crawl4AI 0.9.3 是当前应升级到的目标版本，上游 release note 声明包含安全修复和 33 项修复：[Crawl4AI v0.9.3 release note](https://github.com/unclecode/crawl4ai/blob/main/docs/blog/release-v0.9.3.md)。但 0.9.2/0.9.3 的 `arun_many(..., stream=True)` 外层 generator 生命周期仍有公开问题：[issue #2083](https://github.com/unclecode/crawl4ai/issues/2083)。因此升级是必要条件，不是“升级后无需验证”的充分条件。

## 3. 用户确认的硬约束

以下约束优先于此前报告或此前方案中的相反建议：

1. 不切换到 curl-cffi；Stage 1 保持 curl，Stage 2 保持 Crawl4AI。
2. 旧测试和测试期望需要甄别，但不是本次重点；不能为了迎合明显过时的 SearXNG 测试而反向设计运行配置。
3. Crawl4AI 创建 `BrowserConfig` 时，如果配置了抓取代理，必须传入代理配置。SearXNG 中国大陆境内引擎直连，中国大陆以外引擎使用系统 VPN 代理出口。
4. 升级 Crawl4AI，并更新项目依赖 pin；Playwright 是否升级由目标 Crawl4AI 版本兼容性、doctor 和 smoke test 决定。
5. 保留 SearXNG `latest`。动态更新是本项目应对搜索引擎反爬变化的策略，不得在本次工作中改成固定 SearXNG 版本。
6. 不降低、不重新设计任何并发参数；`fetch_max_workers`、Crawl4AI 页面并发和 worker 数不属于本次优化目标。
7. 把 Stage 1 正文质量判定中的 1000 字符和 6 行阈值放进 `config/config-agent-operation.yaml` 的 `web_tools` 段。

## 4. 目标与非目标

### 4.1 目标

- 保证 SearXNG 的国内/国外引擎代理策略明确、可读、可验证。
- 保证 Stage 2 Crawl4AI 与 Stage 1 在需要代理时使用同一抓取出口。
- 保证代理为空时保持直连，不把未配置代理错误地转换成环境代理或随机代理。
- 将 Stage 1 正文质量阈值配置化，同时保留当前默认行为。
- 将 Crawl4AI 升级到项目验收过的目标版本，并根据该版本更新依赖 pin。
- 保留现有的失败、超时和 SearXNG 摘要兜底语义。
- 增加足够的运行时观测，能区分搜索引擎失败、代理失败、curl 失败、Crawl4AI 失败和正文质量失败。

### 4.2 非目标

- 不研究、不修复、不替换 curl-cffi。
- 不把主传输改成 curl-cffi。
- 不降低 `fetch_max_workers`、`fetch_crawl4ai_max_concurrency` 或 `fetch_worker_processes`。
- 不将 SearXNG `latest` 改成版本号或 digest。
- 不实现 CAPTCHA 自动求解、第三方打码服务或代理池。
- 不把 frontend UI、SSE、Agent prompt、tool schema 或 prefix-cache 作为本次改动范围。
- 不通过删除失败结果来提高表面成功率；摘要兜底必须继续标记为降级结果。

## 5. 产品与技术方案

### 5.1 两阶段请求流程

```text
SearXNG 搜索
    |
    | 结构化结果：title / url / description / 可观测元数据
    v
Stage 1: curl
    |
    | HTTP 错误、challenge、空正文、质量失败、超时
    v
Stage 2: Crawl4AI
    | 共享可选 fetch_proxy，使用 BrowserConfig.proxy_config
    |
    | 仍然失败
    v
SearXNG snippet fallback
    |
    v
明确标记为“摘要兜底”，不伪装成全文抓取成功
```

Stage 1 和 Stage 2 继续使用同一个外层 deadline。Stage 2 不得重新获得一个新的完整 deadline，也不得因为 Stage 1 失败而绕过用户取消和全局截止时间。

### 5.2 代理拓扑

本 PRD 区分两个代理边界：

#### SearXNG 到上游搜索引擎

- 不设置全局 `outgoing.proxies`，避免国内引擎依赖空字符串覆盖全局代理。
- 国外引擎在各自的 engine stanza 中设置：

```yaml
proxies:
  all://:
    - http://host.docker.internal:10808
```

- 国内引擎不设置 `proxies`，保持直连。
- 具体启用哪些引擎由 active `settings.yml` 决定，并通过 SearXNG `/config` 和逐引擎请求验证。
- Compose 增加 `host.docker.internal:host-gateway` 映射。该名称只是 Docker 到 host 的稳定寻址方式，不保证代理程序已监听；代理程序必须监听容器可达的 host 地址。

#### backend/Crawl4AI 到目标网页

- `web_tools.fetch_proxy` 继续作为可选的直接抓取代理配置。
- 当 `fetch_proxy` 为非空字符串时：
  - Stage 1 curl 继续使用该代理；
  - Stage 2 Crawl4AI 的 `BrowserConfig` 必须使用对应的 `ProxyConfig` / `proxy_config`；
  - 不允许只有 curl 走代理而 Crawl4AI fallback 直连。
- 当 `fetch_proxy` 为 `null` 或空值时：
  - Stage 1 和 Stage 2 都不设置代理；
  - 不读取 `HTTP_PROXY`、`HTTPS_PROXY` 等环境变量来隐式改变行为。

backend 当前只收到 URL，不可靠地知道该 URL 是哪个 SearXNG engine 产生的。因此本次将“国内/国外 engine 的直连/代理路由”落实在 SearXNG engine 配置；全文网页抓取使用 `fetch_proxy` 作为明确的可选默认出口，不在本次引入 URL 地理分类器。

### 5.3 Crawl4AI BrowserConfig

`fetch_worker_runtime.py` 创建每一种 `(browser_type, headless)` runtime 时，应从 batch config 读取：

- `fetch_proxy`；
- `crawl4ai_enable_stealth`；
- 现有 browser type/headless 配置。

当 `fetch_proxy` 非空时，使用目标 Crawl4AI 版本支持的 `ProxyConfig(server=fetch_proxy)` 或等价 `proxy_config` 传给 `BrowserConfig`。不得把代理 URL 拼进不受支持的浏览器启动参数，也不得修改 Crawl4AI 依赖源码。

当 `fetch_proxy` 为空时，显式传 `proxy_config=None` 或省略该字段，确保行为是直连。

`enable_stealth` 作为配置化能力接入，但不能声称它能绕过所有 CAPTCHA。默认是否开启应由目标版本 smoke test 和实际资源观测决定；本次不通过并发降级来解决资源问题。

### 5.4 Crawl4AI RunConfig

以下已有行为保持：

- `wait_until`、`page_timeout`、`word_count_threshold` 和 cache bypass 继续由配置控制。
- 外层 Stage 2 timeout 继续约束 `page_timeout`。
- Stage 2 失败仍回到统一的 blocked/empty/low-quality 处理。

本次允许新增并配置化：

- `crawl4ai_enable_stealth`：是否传入 `BrowserConfig(enable_stealth=...)`；
- `crawl4ai_magic`：是否传入目标版本支持的 `CrawlerRunConfig(magic=...)`；
- `crawl4ai_retry_count`：保留现有配置，只有在 deadline 尚可用时进行有限重试。

不强制把全局 `wait_until` 改成 `load`。当前 `domcontentloaded` 是延迟较低的默认策略；对明确的 challenge/anti-bot 页面是否使用更晚的 readiness 条件，应通过可配置策略和固定 URL 测试决定。

### 5.5 Stage 1 正文质量配置化

在 `config/config-agent-operation.yaml` 的 `web_tools` 下新增两个独立键：

```yaml
  # Stage 1 最终正文质量判定；不是正文候选解析阈值
  fetch_quality_min_text_chars: 1000
  fetch_quality_min_text_lines: 6
```

对应 `WebToolsConfig` 字段：

```python
fetch_quality_min_text_chars: int = 1000
fetch_quality_min_text_lines: int = 6
```

定义：

- `text_chars` 是正文清理后字符串的 Python `len()`，不是 token 数、字节数或词数；
- `text_lines` 是正文清理后字符串的 `splitlines()` 数量；空字符串为 0 行；
- `fetch_min_text_chars` 保持原有含义，继续控制候选正文选择，不得改名或复用。

兼容性决策：当前代码的拒绝条件是“字符数低于 1000 且行数不超过 5”同时成立。新配置默认值必须保留这个逻辑，以避免把长的单行 JSON、RSS 或合法纯文本误判为失败。也就是说，默认判定为：

```python
low_quality = (
    not text
    or (
        text_chars < fetch_quality_min_text_chars
        and text_lines < fetch_quality_min_text_lines
    )
)
```

其中 `fetch_quality_min_text_lines: 6` 等价于当前的 `line_count <= 5` 边界。两个阈值必须作为独立的观测字段输出；本 PRD 不偷偷把现有逻辑改成“字符数和行数必须同时达标”的严格 AND 策略。若后续确认产品要求严格 AND，应作为另一个明确的策略变更处理。

Stage 1 的质量失败必须进入 Stage 2 fallback，不能被当作 Stage 1 成功，也不能发布 curl 的低质量 challenge 页面 cookie。

### 5.6 SearXNG 引擎策略

SearXNG 继续使用 `latest`，但运行策略必须可解释：

- active `settings.yml` 是唯一运行配置；
- `settings.yml.example` 只作为生成新环境的模板；
- `settings copy.yml`、`settings.yml.new` 和历史备份不参与启动；
- 测试和文档可以被更新到真实目标策略，但不能用旧测试推翻用户确认的运行策略；
- 启动时记录 SearXNG 镜像 tag、digest、配置文件 hash 和 `/config` 返回的 engine 名单；
- 使用单 engine 请求验证国外代理和国内直连，而不是只根据配置文件文本推断代理成功。

推荐的配置表达是“全局无代理、engine 按需代理”：

```yaml
outgoing:
  request_timeout: 10.0
  max_request_timeout: 15.0
  extra_proxy_timeout: 3.0
  # 不设置 outgoing.proxies

engines:
  - name: google
    proxies:
      all://:
        - http://host.docker.internal:10808

  - name: baidu
    # 不设置 proxies，保持直连
```

上面的 timeout 是初始验证值，不构成固定性能承诺；实际值应根据 SearXNG 日志和搜索 deadline 调整。本次不以增加启用引擎数量作为成功率优化手段。

### 5.7 失败与观测语义

每个结果至少保留以下状态之一：

- `成功`：全文获取且通过质量检查；
- `超时`：达到 per-result 或整体 deadline；
- `失败`：网络、代理、HTTP、解析或 worker 错误；
- `摘要兜底`：全文失败，但返回 SearXNG title/description，必须标明 `source=searxng_snippet`。

结构化日志至少记录：

- request id、query、URL host；
- SearXNG engine 名称、分页、耗时和结果数；
- Stage 1/Stage 2 transport；
- `proxy_configured` 布尔值，不记录代理认证信息；
- HTTP status、blocked marker、challenge 分类；
- 正文字符数、行数、质量阈值和质量判定结果；
- Crawl4AI success/error、页面耗时和 cleanup 结果；
- 是否进入 snippet fallback；
- 当前 Crawl4AI/SearXNG 版本和镜像 digest。

## 6. 预期修改文件

### 必需修改

- `config/config-agent-operation.yaml`
  - 增加 `fetch_quality_min_text_chars: 1000`；
  - 增加 `fetch_quality_min_text_lines: 6`；
  - 增加 Crawl4AI proxy/stealth/magic 的配置说明或对应配置键；
  - 保留 curl、Crawl4AI fallback、SearXNG `latest` 和现有并发值。

- `backend/config.py`
  - 为两个质量阈值增加字段和 YAML loader；
  - 验证非负整数；非法值使用明确的配置错误或既有默认策略；
  - 不修改 `fetch_min_text_chars` 的既有语义。

- `backend/tools/web_tools.py`
  - 从 `_get_fetch_config()` 暴露两个质量阈值；
  - 将 `_looks_like_low_quality_text()` 改为接收配置阈值；
  - 保持 Stage 1 curl、统一 fallback、cookie 和 blocked 检测行为；
  - 增加质量判定的可观测元数据。

- `backend/tools/fetch_worker_runtime.py`
  - 创建 `BrowserConfig` 时传入可选 `proxy_config`；
  - 接入目标 Crawl4AI 版本支持的 stealth 配置；
  - 在取消、stream close 和 runtime close 路径确认 browser context、dispatcher task 和 browser 都完成清理。

- `deploy/searxng/core-config/settings.yml`
  - 将代理键改为规范化表达；
  - 采用全局无代理、国外 engine 显式代理、国内 engine 直连的策略；
  - 保留用户选择的 active engine 集合，不因为旧测试自动切回旧模板。

- `deploy/searxng/docker-compose.yml`
  - 增加 `host.docker.internal:host-gateway`；
  - 不修改 Valkey service name 或端口绑定策略。

- `deploy/searxng/.env`
  - 保留 `SEARXNG_VERSION=latest`；
  - 启动观测记录实际 digest，不把 latest 替换成固定版本。

### 依赖修改

- 将 `crawl4ai` pin 更新到 `0.9.3` 或项目实际验收的 0.9.3-compatible release。
- Playwright 先按目标 Crawl4AI 的依赖约束和当前 Jetson 浏览器运行结果决定；如果目标版本要求升级，则同步更新 pin 和浏览器安装/验证步骤；如果当前 `playwright 1.61.0` 通过兼容性测试，则不因“看起来应该同步升级”而无条件升级。
- 不修改任何第三方依赖源码。

## 7. 验证方案

### 7.1 配置与静态验证

- YAML 可加载，两个质量阈值被正确映射到 `WebToolsConfig`。
- `fetch_min_text_chars` 与 `fetch_quality_min_text_chars` 的语义和测试互不混淆。
- Compose 配置可解析，`host.docker.internal` 映射存在，Valkey 仍使用 service DNS `valkey`。
- active SearXNG 配置中的国外 engine 有代理、国内 engine 没有代理覆盖。
- SearXNG 镜像仍解析为 `latest`。

### 7.2 质量判定回归

必须覆盖以下边界：

| 正文 | 字符数 | 行数 | 默认结论 |
|---|---:|---:|---|
| 空正文 | 0 | 0 | 失败 |
| 短单行正文 | 小于 1000 | 1 | 失败 |
| 长单行正文 | 不小于 1000 | 1 | 保持当前兼容行为，不因新增配置被误判 |
| 短多行正文 | 小于 1000 | 不少于 6 | 保持当前兼容行为，不因新增配置被误判 |
| 长多行正文 | 不小于 1000 | 不少于 6 | 成功 |

测试还必须确认质量失败会触发 Crawl4AI fallback，而不是直接返回 Stage 1 的低质量正文。

### 7.3 代理行为验证

在实际 Docker/Jetson 环境中验证：

1. 容器内能解析并访问 `host.docker.internal:10808`。
2. 一个国外 SearXNG engine 的出口 IP 是系统 VPN 出口。
3. 一个国内 SearXNG engine 不经过系统 VPN。
4. `fetch_proxy` 非空时，curl 和 Crawl4AI 都使用该代理。
5. `fetch_proxy: null` 时，curl 和 Crawl4AI 都保持直连，且不受环境代理变量影响。
6. 代理不可达时，日志能明确显示 proxy connection failure，而不是归类成普通空正文。

### 7.4 Crawl4AI 运行时验证

- `crawl4ai-doctor` 通过。
- Chromium 可以在 Jetson 上启动并抓取一个受控页面。
- `BrowserConfig.proxy_config` 实际出现在 runtime 配置中。
- 代理配置的 Crawl4AI 抓取能够访问一个需要 VPN 出口的测试 URL。
- `enable_stealth`、`wait_until`、`page_timeout` 和 retry 配置都能被验证到实际调用参数。
- 中途取消、deadline、worker 重启后，后续请求仍能执行。
- 关闭 `arun_many(..., stream=True)` 后，dispatcher tasks、page tasks、context 和 browser cleanup 均可观察并完成。

### 7.5 端到端 Stage 验证

固定一组包含以下类型的 URL：

- 普通静态 HTML；
- JS 渲染页面；
- 403/429/challenge 页面；
- 慢响应页面；
- 正文少于质量阈值的页面；
- 重定向页面。

每个 URL 记录 Stage 1 结果、Stage 2 结果、最终状态、质量指标、耗时和是否摘要兜底。成功率必须区分“全文成功率”和“包含摘要兜底的可返回率”。

## 8. 发布与回滚策略

- SearXNG 继续 `latest`，因此每次启动或更新都记录 tag/digest 和 `/config` engine 列表。
- Crawl4AI 依赖升级前保存当前环境版本信息；升级后先运行配置、质量判定、worker lifecycle 和受控 smoke test，再进行公网 URL 验证。
- 发生回归时，回滚 Crawl4AI/Playwright 到上一个已验证的依赖组合；不通过删除 fallback、放宽质量门槛或降低并发掩盖问题。
- SearXNG 引擎可以按 engine 禁用或恢复，但每次改变都必须保留 engine-level 成功率和失败原因。

## 9. 完成标准

本 PRD 对应实现只有在以下条件同时满足时才可宣称完成：

1. curl 仍是 Stage 1，Crawl4AI 仍是 Stage 2，未引入 curl-cffi。
2. 并发配置值未被本次变更降低或重新设计。
3. SearXNG 仍使用 `latest`，国内/国外 engine 代理策略可从运行配置和单 engine 测试中证明。
4. `fetch_proxy` 非空时，Crawl4AI `BrowserConfig` 确实收到 proxy config；为空时确实直连。
5. Crawl4AI 依赖已升级并通过目标环境验证，Playwright pin 与其兼容。
6. `fetch_quality_min_text_chars` 和 `fetch_quality_min_text_lines` 已从 YAML 读取，默认值分别为 1000 和 6，且没有把既有 `fetch_min_text_chars` 误作同一参数。
7. Stage 1 质量失败会进入 Stage 2 或 snippet fallback，并在结果/日志中保持失败来源可见。
8. 受限测试环境中的 socket 失败与真实产品回归被明确区分；不能把未执行的真实 Docker/公网验证报告为通过。
