# search_and_fetch 原生崩溃调查与进程隔离（已完成）

## 已确认的事故证据

原始报告为 `/var/crash/_usr_bin_python3.12.1000.crash`，Apport 日期为
2026-09-09 23:41:58。进程命令是 `python3 -m uvicorn backend.main:app ... --port 8090 --workers 1`，
主线程 PID 257641 与用户终端的崩溃日志一致。

GDB 检测到崩溃线程 LWP 279591 的调用链：

```text
SIGABRT -> malloc_printerr("free(): invalid pointer")
        -> _int_free -> __libc_free
        -> lxml/etree.cpython-312-aarch64-linux-gnu.so（五个原生帧）
        -> PyObject_Call -> _PyEval_EvalFrameDefault
```

另一线程 LWP 279588 同时位于 `lxml.etree`。这确定了错误被检测时的原生调用位置，
没有确定最初发生内存损坏的代码、输入或竞态；不能直接归因为某个 lxml 调用者。
尤其不能把“短暂正文解析压测未崩”当成已排除 trafilatura/readability 的依据。

局限：lxml 缺少内部调试符号，本地 GDB 没有 `py-bt`，因此没有 Python traceback。
GDB 有 executable mismatch 警告；Apport 的 Python 可执行文件时间戳与当前文件相同，
但 core 未包含 build-id 字节，无法完整确认二进制匹配。lxml 帧地址均落在 Apport
原始 ProcMaps 对应映射范围内。没有下载或修改依赖来获取调试符号。
本机 lxml 版本为 `7.0.0a3`；版本信息本身不能证明该版本存在本次触发的缺陷。

原始分析保存在 `/tmp/search-fetch-core-inspection/diagnosis.txt` 和
`/tmp/search-fetch-core-inspection/native-backtrace.txt`；这些是本机临时证据，不是仓库附件。

## 对前一版修改的复查

1. **并发上限 2 没有根因证据。** “7–10 个浏览器会把 Thor 堆冲垮”不是已验证结论。
   之前 3 × 25 个结果成功，也不能反向证明实际浏览器负载为 0。
2. **线程等待超时不等于资源终止。** 旧实现的 `thread.join(timeout=...)` 超时后抛错，
   但运行中的线程、事件循环和浏览器没有因此停止；外层 `finally` 释放 slot 会让计数
   与真实活动资源脱节。这个分支只出现在调用线程已经有运行中事件循环时。
3. **常规线程池路径没有覆盖完整生命周期的超时。** 旧实现直接 `asyncio.run(_run())`，
   页面导航超时不能保证浏览器初始化、退出、解析或底层 native 调用都按时结束。
4. **旧八个单元测试不证明原始 native 崩溃已经修复。** 它们验证配置、信号量及取消等
   Python 行为，不等同于在真实子进程发生 SIGABRT 后验证 API 存活。
5. **fallback 不只由 blocked 触发。** 旧 transport 循环在主 transport 超时、异常、
   内容提取失败等路径结束后仍可进入下一个 transport；`should_fallback_transport`
   只是其中一条提前结束重试的路径。

本次移除配置中无依据的浏览器并发和特定 curl_cffi 版本归因注释。进程隔离是针对
native 故障影响范围的修复，并不声称已经修复上游内存错误本身。

## 实施设计

API 进程保留搜索编排、取消控制、结果汇总和 curl 子进程编排。curl 命令继续作为独立
子进程运行；curl 返回后的 trafilatura/readability 正文解析、curl_cffi 和 Crawl4AI
进入常驻隔离 worker。仅隔离 Chromium 不足以保护仍在 Python 主进程内运行的原生解析库。

worker 复用 Crawl4AI 浏览器，并通过批量页面任务调度减少每个 URL 重新启动浏览器的
成本。父进程执行跨调用共享的页面任务上限，worker 结合系统内存压力推迟启动新页面。

| 配置 | 默认 | 含义 |
| --- | ---: | --- |
| `fetch_crawl4ai_max_concurrency` | 50 | 全局 Crawl4AI 页面任务上限，非浏览器进程数 |
| `fetch_worker_processes` | 4 | 常驻隔离 worker 数 |
| `fetch_worker_cancel_grace_seconds` | 1.0 | 取消后的清理宽限时间，秒 |
| `crawl4ai_memory_threshold_percent` | 85.0 | 系统内存压力阈值，必须大于 0 且小于 100 |
| `crawl4ai_memory_check_interval_seconds` | 0.1 | 内存状态检查间隔，秒 |
| `crawl4ai_batch_wait_seconds` | 0.01 | 页面任务批量汇集等待时间，秒；可设为 0 |

这些值是可调配置，不是硬件性能保证。50 是上限，不承诺在高内存压力或任务不足时
保持 50 个活动页面。85% 是调度阈值，不是阻止系统 OOM 的硬内存配额。
计数拒绝布尔值、非正数和非整数，浮点参数拒绝非有限值；非法值回退默认配置。

deadline/Stop 先请求目标任务协作取消。清理超过宽限期则升级为终止对应 worker，
必要时清理其浏览器子进程。一个 worker 内仍在进行的其他任务可能同时失败；父进程
应把这些失败收敛为工具错误或已有 snippet fallback，并能补充 worker 后继续服务。
不能在资源仍存活时提前释放其容量，也不能因单任务取消而终止无关 worker。

本机 Playwright 的 `coreBundle.js:8755` 使用 `detached: true` 启动浏览器，浏览器
可处于独立进程组，因此只对 Python worker 调用 `killpg` 不足以回收浏览器。
回收设计使用每个 worker 唯一、随进程环境继承的标记，限定同 UID，通过 `/proc`
识别对应子进程并利用 pidfd 验证和操作进程身份，覆盖脱离原进程组的浏览器后代。
不修改 Playwright 依赖，也不按浏览器进程名或宽泛 PID 范围终止其他应用。

不修改模型 prompt、canonical history、工具 schema、SSE 协议或 prefix-cache 规则。

## 验证结果与局限

配置测试先在旧实现上得到 56 个预期失败（默认仍为 2 或缺少新字段），再实现配置
校验；以下测试结果为 **70 passed**：

```bash
./.sglang/bin/python -m pytest tests/test_fetch_worker_config.py tests/test_web_tools_config.py -q
```

进程池 19 项与 runtime 10 项联合得到 **29 passed**。覆盖真实 worker 的
`os.abort()` 故障隔离、后续任务恢复、deadline/取消清理、独立进程组后代回收及
不终止无关进程；runtime 使用真实官方 dispatcher，替换浏览器层，验证浏览器复用、
独立 context、页面并发限制、内存压力调度、取消清理和晚到任务不被慢 batch 阻塞。
这类浏览器替身测试不等同于真实 Chromium 端到端验证。

真实 production parser 路径已通过隔离 worker 验证，并确认父进程没有导入
lxml、curl_cffi 或 Crawl4AI。启动阶段挂起的取消另经探针复查：清理完成前不 ACK，
因此 supervisor 可以在宽限期后终止故障 worker，避免永久污染后续请求。

最终大联合回归为 **213 passed / 10 failed，耗时 14.48 秒**，原始输出为
`/tmp/fetch-final-regression.txt`。十个失败与实施前基线一致，均为本地测试 HTTP
服务建立 `socket(AF_INET)` 时的 `PermissionError`，不是已验证通过的功能项。
`compileall` 退出码为 0。实际生效配置已核读：页面上限 50、worker 4、内存阈值
85%、取消宽限 1.0 秒、内存检查间隔 0.1 秒、批量等待 0.01 秒。

实施前基线（search、web fetch、config、transport report 相关测试）为
115 passed / 10 failed；这 10 个失败均在建立 `socket(AF_INET)` 时得到
`PermissionError`，受当前 sandbox 网络限制，不能当作产品功能回归或已通过测试。
本次环境不允许提升权限，真实本地 HTTP 验证若遇到同一限制须明确保留为未验证项。
主服务相关 lifespan 定向测试为 **2 passed，耗时 5.91 秒，两个已有 warnings**；
运行整个 main 测试文件时，无关 OCR 测试出现挂起，本轮未扩大到 OCR 排障，
不能将整个测试文件报告为通过。

手动浏览器 smoke 已实际运行，但在 `ThreadingHTTPServer` 创建
`socket(AF_INET)` 时因 `PermissionError` 停止，尚未进入浏览器阶段。
用户可在普通终端运行以下受控本地页面验证：

```bash
./.sglang/bin/python tests/manual_fetch_browser_smoke.py
```

尚未完成真实 50 页面吞吐测量，不承诺速度提升比例。原查询的公网重跑可以补充
端到端延迟和成功率数据，但单次无崩溃不能证明原始内存错误消失。
