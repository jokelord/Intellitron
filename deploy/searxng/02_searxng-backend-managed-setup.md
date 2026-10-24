# Backend 托管的 SearXNG 启动说明

## 这是什么

本项目当前的联网搜索能力依赖一个自部署的 SearXNG 服务。
你不需要先判断自己属于哪一种部署方式：`scripts/3_start_backend.sh` 会在后端完全启动后优先探测并复用已有实例；如果当前地址不可用，再尝试使用本仓库内的 `deploy/searxng/` 进行托管启动。

## 默认端口

- 后端 API: `8090`
- 前端开发端口: `3000`
- 推理服务: `30000` for local SGLang LLM, `8000` for vLLM NGC (container); `8118` for PaddleX OCR and `30002` for its OCR SGLang backend
- SearXNG: `8080`

SearXNG 使用官方默认端口 `8080`，后端改为 `8090` 以避免冲突。

## 第一次使用前的准备

你需要先安装：

- Docker
- Docker Compose（`docker compose`、`docker-compose`，或教材里的本地 `./dc` 任一可用即可）

## 如何启动

直接像平时一样启动后端：

```bash
bash scripts/3_start_backend.sh
```

启动顺序是：

1. 后端先启动
2. 后端健康检查通过
3. 终端提示你现在可以去开启前端了
4. 如果当前 `SEARXNG_BASE_URL` 已有可用的 SearXNG，后端脚本直接复用它
5. 如果当前地址还没有可用实例，后端脚本会继续尝试本仓库托管启动
6. 如果托管启动失败，后端脚本会打印部署指引，你可以继续按教程完成 SearXNG 安装

## 为什么第一次可能很慢

如果你的机器以前没有拉取过相关容器镜像，后端终端里会出现较长时间的下载输出。
这是正常现象。

即使此时搜索服务还没准备好，只要后端已经启动成功，你就已经可以去开启前端。

## 当前默认引擎策略

当前默认目标引擎组为：

- `google`
- `startpage`
- `mojeek`
- `360search`
- `sogou`
- `chinaso news`

`baidu`、`cn.bing.com`（Bing）以及 ChinaSo 图片/视频引擎目前只保留在配置示例中，
不在当前 `keep_only` 激活列表内；启用它们前应先重新评估代理归属并更新对应的 engine 配置。

代理策略为：

- 中国系引擎直连
- 外网引擎按引擎配置经宿主机 VPN 代理出站（`host.docker.internal:10808`）

这是 SearXNG 的引擎级策略：不设置全局 `outgoing.proxies`；国内引擎不配置 `proxies`，外网引擎使用 `all://` 代理键。代理程序需要监听宿主机可被容器访问的 `10808` 端口。

当前 live 配置的 `image_proxy` 仍为 `true`，用于兼容现有搜索结果图片访问；如需关闭，
应在同一份 live `settings.yml` 中显式修改并重启 SearXNG。

## 当搜索服务还没准备好时会怎样

如果 SearXNG 仍在下载、启动失败、或者暂时不可用：

- 你仍然可以去开启前端
- 当前仅网络搜索功能受影响
- 所有非搜索功能仍然可以正常使用
- 终端会继续给出 `./dc`、Docker 代理、日志查看与重启命令等操作指引

## 如何确认搜索服务正常

搜索服务正常后，后端终端会打印 SearXNG 可用提示。
如果你已经按教材手动启动过实例，后端脚本会优先复用这个已运行实例，而不是强制再次托管启动。

你也可以手动访问：

```bash
curl "http://127.0.0.1:8080/search?q=ping&format=json"
```

## 如何停止

在运行 `scripts/3_start_backend.sh` 的同一个终端按下 `Ctrl+C`。

这会：

1. 安全关闭后端
2. 安全关闭由本次后端会话拉起的 SearXNG 服务

## 常见问题

### 1. 后端已经起来了，但搜索服务还没好

这是允许的。
你现在就可以去开启前端；只有网络搜索不可用，非搜索功能不受影响。

### 2. 提示 Docker 或 Docker Compose 不可用

这说明后端无法自动托管搜索服务。
如果你已经按教材用 `./dc up -d` 手动启动了 SearXNG，后端脚本应直接复用现有实例；如果还没启动实例，请按终端打印的教程指引继续完成：

```bash
cd deploy/searxng
chmod +x ./dc
./dc up -d
./dc logs -f core
```

如果你修改了 `settings.yml`，请执行：

```bash
./dc down && ./dc up -d
```

### 3. 教材里的 `./dc` 能不能继续用

可以。
后端脚本兼容三种托管启动方式：`docker compose`、`docker-compose`、以及部署目录里的本地 `./dc`。

### 4. 8080 端口冲突

请先释放本地 `8080`，或修改 `deploy/searxng/.env` 中的端口映射后重新启动后端。
