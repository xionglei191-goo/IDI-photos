# IDI Linux VPS 部署

这份部署包含完整工作台、飞书长连接收图、CPU OCR、外部模型接口和 SQLite 档案库。建议从 Linux x86_64、2 核 4 GB 内存、可扩容 SSD 起步；这是容量估算，不是目标 VPS 的压测结果。只运行一个 IDI 容器，不能对同一档案库启动多个工作进程或副本。

Windows 微信桌面自动化仍依赖 Windows 客户端，不在 Linux 上运行；飞书长连接可直接在 VPS 运行。“打开资料目录”需要在服务器侧访问对应目录，浏览器无法打开服务器的资源管理器。VPS 前端构建使用 `deploy/vps/frontend` 的独立依赖锁，保留现有 Windows 开发依赖。

镜像内置 Node.js、Playwright 和无界面 Chromium，供官网正常网页登录使用，服务器无需桌面环境。构建时需要下载这些组件；运行时浏览器只在登录过程中启动，凭据通过父子进程管道传递。生产账号和官网可达性仍需在目标 VPS 验证。

## 1. 准备及首次启动

服务器需要 Docker Engine 和 Docker Compose v2。使用支持的 Linux 发行版，按 [Docker 官方文档](https://docs.docker.com/engine/install/) 安装。将完整源码或交付的源码包放在服务器一个独立目录，例如 `/opt/idi`。不要覆盖已有服务目录。

```sh
cd /opt/idi
cp .env.example .env
docker compose build
docker compose up -d --wait --wait-timeout 120 idi
docker compose ps
docker compose logs --tail=80 idi
curl --fail http://127.0.0.1:8766/api/health
```

首次启动生成独立的 256 位主密钥，随后复用；数据库、原图、备份放在 `idi_data` 卷，主密钥放在 `idi_key` 卷，容器以 UID/GID `10001:10001` 运行。镜像不包含本机数据、账号、密钥、`.env` 或历史备份。内存上限默认 3 GB，可在 `.env` 中调整；内存不足时应增加容量或减少收图入口和批量任务并发。

应用只发布服务器的 `127.0.0.1:8766`。在自己的电脑建立 SSH 隧道，再访问 `http://127.0.0.1:18766`：

```sh
ssh -N -L 18766:127.0.0.1:8766 your-user@your-vps
```

如果服务器 8766 已被占用，修改 `.env` 中 `IDI_HTTP_PORT`，并同步调整上述隧道的目标端口。默认的 Docker 私网是 `172.30.80.0/24`；如与 VPN 或现有网络冲突，同时修改 `.env` 中网段、网关、应用、代理四项，保证地址位于同一私网且互不相同。

首次打开隧道地址时创建 IDI 系统管理员；没有默认密码。之后在「人员与权限」创建人员并分配项目。首次创建只接受本机或 SSH 隧道访问，不接受公网代理请求。详情见 [人员与项目权限](../../docs/project-access.md)。

## 2. 可选：域名、HTTPS 和登录

需要已有域名解析至 VPS，80/443 端口可用。已有反向代理的服务器应由原代理接管域名，避免抢占端口；本配置不修改服务器防火墙或其他服务。

1. 将 `.env` 中 `IDI_DOMAIN` 填为完整域名，例如 `idi.example.com`，不带协议、端口或路径。
2. 运行下面的交互命令设置网页访问密码。密码只在交互提示里输入，不放入命令历史：

   ```sh
   docker run --rm -it caddy:2-alpine caddy hash-password
   ```

3. 新建 `deploy/vps/users.caddy`，写一行 `admin <上一步得到的完整密码哈希>`，用实际哈希替换占位内容。执行 `chmod 600 deploy/vps/users.caddy`。这是工作台入口账号，与官网系统账号不同。
4. 启动 HTTPS 入口：

   ```sh
   docker compose --profile https up -d
   docker compose logs --tail=80 caddy
   ```

访问 `https://你的域名`，浏览器先进行入口认证，再使用每个人自己的 IDI 人员账号登录。IDI 区分系统管理员、项目管理员、项目成员，后端按项目校验访问权限；入口账号本身不授予项目权限。Caddy 自动管理证书。禁止直接把应用的 8766 映射至公网。Docker 端口发布的网络行为见 [官方说明](https://docs.docker.com/engine/network/port-publishing/)。

手机相机接口保留配对码及设备令牌认证，使用明确的路由白名单，避免网页 Basic Auth 与设备 Bearer 令牌冲突。手机配对管理仍需通过 SSH 隧道打开 `/camera-setup`；公网管理路由会拒绝请求。APK 不打入服务镜像，如需下载，将经过确认的 APK 放入数据卷的 `mobile-app/idi-camera.apk`。

## 3. 从 Windows 迁移

先在现有工作台创建并下载完整档案备份。备份包含数据库、原图、缩略图、报送材料及 IDI 人员权限（登录密码为哈希）；不包含官网密码、飞书密钥、模型密钥或今日相机授权。Windows DPAPI 文件不能在 Linux 上解密，需要在新服务中重新填写这些凭据。

在 VPS 上先构建镜像，**在第一次启动服务前**恢复到空数据卷：

```sh
docker compose build
docker compose run --rm --no-deps \
  -v /absolute/path/idi-backup-example.zip:/restore.zip:ro \
  idi python -m idi.cli restore /restore.zip --target /var/lib/idi
docker compose up -d --wait --wait-timeout 120 idi
```

恢复命令校验文件哈希、SQLite 完整性和关联关系，拒绝覆盖非空目录。若已试运行，保留试运行卷，在另一个目录用不同项目名部署，如在各条命令中使用 `docker compose -p idi-production ...`；不要删除已有卷来腾位置。

恢复默认暂停自动收图、群助手发送、自动跟进和官网写入任务。重新填写凭据、核对案件和图片，再逐项启用。正式切换飞书前先停用旧 Windows 端对应入口，避免两端同时处理同一消息。迁移期间可保留旧档案作为回退依据。

默认 `IDI_PLATFORM_WRITE=0`，官网写入保持关闭。确认迁移结果且准备开始业务写入后，将它改为 `1`，再执行 `docker compose up -d idi`。公司账号始终只读，写入必须配置本项目官网账号。照片自动保存草稿无需人工审核；正式提交仍由系统管理员或本项目管理员核对确认。

## 4. 备份、升级和回退

日常档案备份可在网页创建，也可运行：

```sh
docker compose exec -T idi python -m idi.cli backup --data-dir /var/lib/idi
docker compose cp idi:/var/lib/idi/backups/命令返回的文件名.zip ./备份文件名.zip
```

将备份复制到服务器以外的位置。普通档案备份有意排除凭据。若需要保留 VPS 登录配置，额外离线保管同一时间点的 `/var/lib/idi/secrets` 和 `/var/lib/idi-key/master.key`，并限制其读取权限；保留加密文件而丢失主密钥将无法解密。不要把主密钥写入 `.env`、源码包或日志，也不要只备份数据库后删除数据卷。

升级前保存镜像 ID、档案备份和独立凭据备份。基础镜像使用固定摘要，前后端依赖使用 Linux 验证后的锁文件；系统软件包仍从 Debian 仓库安装。建议构建一次后使用该镜像摘要部署，升级依赖后重新验证。

```sh
docker compose images
docker compose stop caddy idi
docker compose build
docker compose up -d --wait --wait-timeout 120 idi
# 使用 HTTPS 时：
docker compose --profile https up -d
docker compose ps
```

回退使用旧镜像和升级前的数据备份恢复到新卷，不让旧程序直接打开已升级的库。不要执行 `docker compose down -v`，该命令会删除档案和主密钥卷。

## 5. 验证与排查

- `docker compose ps` 应显示 IDI 为 `healthy`；检查网页、案件列表、原图下载和备份恢复。健康接口只证明服务已启动。
- 飞书是主动建立长连接，不需要回调公网端口；仍需在界面配置应用权限、事件订阅与指定群，并用实际收图验证连接。
- 服务器需要能访问飞书、业务官网、模型接口；AI 模型本身走外部 API，不需要本机 GPU。OCR 在 CPU 上运行。
- 服务拒绝错误主密钥、损坏凭据或凭据存在而密钥丢失的情况，日志不会输出密钥内容。保留原文件后核对卷挂载与 UID/权限。
- Docker 数据卷必须放在本机持久磁盘；不要在 NFS/SMB 上运行 SQLite。照片、目录副本及备份会持续占用空间，应监测磁盘余量。
- 容器启动沿用单实例文件锁；关闭时留出 90 秒停止后台工作。故障重启会按原有任务恢复规则处理，不代表未知的官网写入已成功。

浏览器与工作台渲染自检使用模拟登录页，不会请求真实官网或使用业务账号：

```sh
docker compose exec -T -e IDI_SMOKE_WORKBENCH=1 idi node scripts/vps-browser-smoke.mjs
```

凭据采用 [AES-GCM 认证加密](https://cryptography.io/en/latest/hazmat/primitives/aead/#cryptography.hazmat.primitives.ciphers.aead.AESGCM)；入口认证使用 [Caddy basic_auth](https://caddyserver.com/docs/caddyfile/directives/basic_auth)。Windows 默认仍使用原有 DPAPI。

本次验证范围及上线前仍需确认的项目见 [验证记录](VALIDATION.md)。
