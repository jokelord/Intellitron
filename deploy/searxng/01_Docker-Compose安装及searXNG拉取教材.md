# SearXNG 完美部署实战指南 (Jetson Thor / ARM64 特供版)

本教程总结了在 Ubuntu 24 环境（特别是 Jetson Thor 等 ARM64 架构设备）下，零侵入、无痛部署最新版 SearXNG 搜索引擎的完整流程。全面修复了大陆网络下的镜像拉取、容器出站代理，以及新版 Valkey 命名与引擎精确裁剪的暗坑。

## 🛠️ 第一阶段：系统环境准备（无痛外挂法）

为了**绝对不影响**你宿主机原有的英伟达 NGC 开发环境以及 `nvidia-container-toolkit` 的稳定性，我们采用局部外挂二进制与服务级代理注入。

### 1. 下载本地外挂版 Docker Compose (ARM64 架构专用)

在你的项目目录下直接拉取独立二进制文件，不污染系统全局路径：

```bash
# 进入你的项目部署目录
cd ~/Project/LLM-deployment/deploy/searxng

# 下载针对 Jetson (ARM64) 架构编译的专用 Compose 工具，并命名为 dc
curl -SL https://github.com/docker/compose/releases/download/v2.35.0/docker-compose-linux-aarch64 -o ./dc

# 赋予其局部可执行权限
chmod +x ./dc

# 验证版本是否正确
./dc version

```

### 2. 打通网络：为 Docker 守护进程配置代理

由于下载镜像是由系统后台的 `dockerd` 负责（它听不到你终端的 export 代理），需要通过服务覆盖文件让其学会走你的梯子（假设本地代理端口为 `10808`）：

```bash
# 创建 Docker 服务的系统配置覆盖目录
sudo mkdir -p /etc/systemd/system/docker.service.d

# 写入代理规则（127.0.0.1 在宿主机服务层面是完全合法的）
sudo tee /etc/systemd/system/docker.service.d/http-proxy.conf <<-'EOF'
[Service]
Environment="HTTP_PROXY=http://127.0.0.1:10808"
Environment="HTTPS_PROXY=http://127.0.0.1:10808"
Environment="NO_PROXY=localhost,127.0.0.1,registry.docker-cn.com,hub-mirror.c.163.com"
EOF

# 刷新并重启 Docker 守护进程
sudo systemctl daemon-reload
sudo systemctl restart docker

```

---

## ⚙️ 第二阶段：核心配置三剑客（100% 正确缩进）

请确保在当前项目目录下创建了 `./core-config` 目录，然后分别完整写入以下三个文件：

### 1. `docker-compose.yml`

保持官方推荐的干净骨架，剔除冗余环境变量。

```yaml
name: searxng

services:
  core:
    container_name: searxng-core
    image: docker.io/searxng/searxng:${SEARXNG_VERSION:-latest}
    restart: always
    ports:
      - ${SEARXNG_HOST:+${SEARXNG_HOST}:}${SEARXNG_PORT:-8080}:${SEARXNG_PORT:-8080}
    env_file: ./.env
    volumes:
      - ./core-config/:/etc/searxng/:Z
      - core-data:/var/cache/searxng/

  valkey:
    container_name: searxng-valkey
    image: ${VALKEY_IMAGE:-ghcr.io/valkey-io/valkey:9-alpine}
    command: valkey-server --save 30 1 --loglevel warning
    restart: always
    volumes:
      - valkey-data:/data/

volumes:
  core-data:
  valkey-data:

```

### 2. `.env`

用于解耦端口及版本控制。

```env
SEARXNG_VERSION=latest
VALKEY_IMAGE=ghcr.io/valkey-io/valkey:9-alpine
SEARXNG_HOST=0.0.0.0
SEARXNG_PORT=8080

```

### 3. `./core-config/settings.yml`

**修正重点**：使用仓库内的 live `settings.yml` 作为唯一配置源；使用 `valkey` 正确连接缓存；
不要使用固定 Docker bridge IP 或全局代理。容器通过 Compose 的
`host.docker.internal:host-gateway` 访问宿主机 VPN 代理。

当前 engine policy 为：`google`、`startpage`、`mojeek` 使用
`http://host.docker.internal:10808` 的 engine 级 `all://` 代理；`360search`、`sogou`、
`chinaso news` 不设置 `proxies`，保持直连。SearXNG 的 `outgoing` 段不设置全局 `proxies`。
如果 `core-config/settings.yml` 由容器以 `nobody:nogroup` 持有而宿主机用户无法更新，
先修复该文件/目录的宿主机写权限，再应用配置；不要另建一份未被 Compose 挂载的配置。

```yaml
# 仅展示代理策略；完整字段以仓库 live settings.yml 为准
outgoing:
  request_timeout: 60.0
  max_request_timeout: 60.0

engines:
  - name: google
    proxies:
      all://:
        - http://host.docker.internal:10808
  - name: startpage
    proxies:
      all://:
        - http://host.docker.internal:10808
  - name: mojeek
    proxies:
      all://:
        - http://host.docker.internal:10808
  - name: 360search
    # 不设置 proxies，保持直连
  - name: sogou
    # 不设置 proxies，保持直连
  - name: chinaso news
    # 不设置 proxies，保持直连
```

---

## 🚀 第三阶段：权限修正与一键合闸

新版 SearXNG 容器内部以非 root 用户（UID 977）运行。如果宿主机映射文件夹所有权不正确，容器将直接崩溃。

```bash
# 1. 修正宿主机挂载目录的权限（移交给容器内 uid 977）
sudo chown -R 977:977 ./core-config

# 2. 局部命令一键拉取并常驻后台启动
./dc up -d

```

---

## V2rayN 的监听设置更改
为了让 V2rayN 能接收容器的网络连接请求，应监听宿主机的 0.0.0.0:10808；
容器侧通过 `host.docker.internal:10808` 访问该监听端口，
需要在 v2rayN 的【参数设置】里确保：
  - 打开“允许来自局域网的连接”，
  - **不要**勾选“为局域网打开新端口 / New port for LAN”

## 📋 运维常备指令

* **观察运行日志**：`./dc logs -f core`
* **应用配置更新**：每当你修改了 `settings.yml`，请务必执行毁灭性重启以彻底重载引擎列表：

```bash
cd ./deploy/searxng
./dc down && ./dc up -d

```


* **享受成果**：打开浏览器，直接访问 `http://localhost:8080` 即可开始安全、干净的联网搜索。
