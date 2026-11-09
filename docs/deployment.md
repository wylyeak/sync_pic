# Docker Compose 部署

需要 Linux 和 Docker Compose v2。控制器、看板、目录检查和 Syncthing 均在 Compose 中；数据库、ADB、手机网页按需启用。不会自动迁移现有部署。

## 1. 准备配置和存储

    sh scripts/prepare.sh

该脚本只复制三个示例配置并创建 state 目录，不启动服务。编辑 .env；全部参数见 [配置参考](configuration.md)。

PICSYNC_PHOTO_ROOT 是宿主机媒体共同父目录，例如 /srv/photos；其下 source、target 是已存在、互不重叠、位于同一文件系统的目录。target 必须专用于本项目。用 findmnt -T /srv/photos 核实挂载和文件系统类型，并填写 PICSYNC_MEDIA_FS；NAS 可能需要 nfs4，不能照抄 ext4。

控制器仅将共同父目录挂载一次为 /photos，避免分别 bind mount 源和目标导致硬链接失败。启动前检查容器只读挂载媒体；Syncthing 仅获得目标目录 /sync/photos，不获得源或共同父目录。

    docker compose config --quiet
    docker compose build controller dashboard
    docker compose run --rm preflight

目录不存在、路径穿越、符号链接、重叠、挂载别名或文件系统类型不符会阻止检查通过。preflight 不写照片或数据库。

NAS 必须在宿主机先挂载成功，再启动 Compose。容器内检查不能完全替代宿主机挂载依赖；不要让 NAS 未挂载时的本地空目录冒充媒体目录。准确填写文件系统类型可以拒绝大部分挂错文件系统的情况。

## 2A. 外部 MySQL

不启用 database profile。在 .env 填写外部地址、端口、数据库名，以及控制器账号和看板只读账号。

    PICSYNC_MYSQL_HOST=db.example.test
    PICSYNC_MYSQL_PORT=3306
    PICSYNC_MYSQL_DATABASE=picsync
    PICSYNC_MYSQL_USER=picsync
    PICSYNC_MYSQL_PASSWORD='自行设置'
    PICSYNC_DASHBOARD_MYSQL_USER=picsync_reader
    PICSYNC_DASHBOARD_MYSQL_PASSWORD='另一个密码'

由管理员按 deploy/mysql-users.example.sql 创建库和用户。自定义名称时同步修改授权 SQL。控制器仅需自己的业务库权限；看板只能 SELECT，不能复用控制器账号。

控制器和看板使用 host 网络，可以连接本机或外部数据库，不需要 Docker 服务 DNS。核实防火墙和数据库账号来源限制。模板未实现 MySQL TLS 参数；跨不可信网络时应另外使用安全隧道。

## 2B. Compose 内置 MySQL

在 .env 设置上述两个业务账号密码，并设置 MYSQL_ROOT_PASSWORD。连接地址使用 127.0.0.1。宿主机已有 MySQL 时，将 PICSYNC_MYSQL_PORT 改为另一个空闲端口。

    docker compose --profile database up -d --wait mysql
    docker compose --profile database ps mysql

模板使用 MySQL 8.4。首次初始化由官方镜像创建业务库和控制器账号，deploy/mysql-init.sh 创建独立看板只读账号。两个账号必须不同，名称只允许字母、数字、下划线，最长 64 字符，密码不可为空。

数据在 state/mysql。初始化变量仅在数据目录为空时生效；修改 .env 不会重置已有账号密码和授权。已有数据应通过 SQL 修改，不能通过删除数据库目录来修复。数据库端口只映射到本机回环地址，不直接开放到局域网。

## 3. ADB 二选一

### 复用宿主机 ADB（默认）

安装系统 ADB，开启手机 USB 调试，在手机确认授权，并填写 PICSYNC_SERIAL：

    adb devices -l

共享服务只监听本机 5037，不要加 -a。可沿用现有管理方式，或使用 deploy/adb/picsync-adb.service。

可选 scripts/install-user-services.sh 只安装用户服务文件。只需 ADB 时仅启用 picsync-adb.service，不要将宿主机控制器与 Compose 控制器同时运行。用户服务开机运行还需要配置 linger，完成后通过实际重启验证。

### ADB 也在 Compose 中

先停止宿主机 ADB 及其他持有手机 USB 的服务：

    docker compose --profile usb up -d --build adb
    docker compose --profile usb exec adb adb -H 127.0.0.1 -P 5037 devices -l

仅 adb 服务拥有 USB 挂载与设备规则，不使用 privileged。它以 root 运行以访问 USB，仅保留 DAC_OVERRIDE 以访问 USB 和私有授权目录，移除其他 capabilities。若设备不可访问，检查宿主机权限及安全策略，不要盲目放宽为 privileged。

ADB 授权在 state/adb-auth，首次使用需手机确认。controller、手机网页和宿主机客户端共用这个服务。不能同时启用宿主机 ADB；手机重启后应验证自动重连。

## 4. Syncthing 与手机配对

    docker compose up -d syncthing

访问 http://127.0.0.1:8384/，先设置 GUI 认证，再与手机配对。电脑共享路径只能是 /sync/photos，文件夹 ID 与 controller/config.toml 的 syncthing.folder 一致（默认 photos）。

把手机完整设备 ID 填入 PICSYNC_SYNCTHING_DEVICE。手机同步目录必须被 Google 相册备份，两端允许双向同步删除。先用测试文件验证删除传播与开关机重连，不能共享源或共同父目录。

控制器只读挂载 Syncthing XML 配置，从中读取本机 API 密钥。此文件包含敏感信息，不得提交。

## 5. 检查并启动工作流

数据库、手机和配对配置完成后：

    docker compose run --rm controller python controller/file_sync.py dry-run

该命令核对目录边界和 Syncthing 状态，不扫描入库、不释放空间、不删除照片；它不等于整条相册 UI 自动化验证。先用专用目录和小批量做实际演练。

    docker compose up -d controller dashboard syncthing
    docker compose ps
    docker compose logs --tail 100 controller

控制器首次启动自动建表和迁移，之后会执行真实备份、相册释放及目标清理。看板位于 http://127.0.0.1:8027/。

也可一次启用完整内置组件：

    docker compose --profile database --profile usb up -d --build

但第一次仍建议按上面的顺序完成数据库初始化及配对，再启用控制器。所有真实配置与运行数据保持在 Git 之外。

## 6. 可选手机控制网页

可以继续使用其他手机控制服务，不必启用 phone profile。

从 Google 官方 platform-tools 获取与 Linux 架构兼容的独立 adb 客户端，放在 state/adb-client，并赋予可执行权限。不能直接复制依赖宿主机特有共享库的系统 adb。

    chmod 755 state/adb-client
    docker compose --profile phone up -d pixel

示例网页的独立客户端方案目前按 Linux x86_64 组织，其他架构需验证上游支持。网页只监听本机 PIXEL_WEB_PORT，没有 USB 挂载；包装器阻止其关闭或另起共享 ADB。fix-stream.cjs 是特定上游版本的缓冲补丁，更新镜像后必须重新验证。

按上游说明初始化网页账号和自己的反向代理，再设置 PICSYNC_PIXEL_URL、PICSYNC_SYNCTHING_URL。URL 不得包含密码或访问令牌。

## 7. 开机、升级和备份

长期服务使用 restart: unless-stopped，preflight 是一次性服务。Docker 自动恢复容器不会在每次开机时重跑完整 Compose 依赖图；宿主机仍需确保 NAS、ADB 等依赖先恢复。

升级前备份数据库和私有配置，再 build 和 up 控制器、看板。不要用 down -v 或删除 state/mysql 做常规升级。

备份应包含一致性 MySQL 导出、私有配置、Syncthing 状态、ADB 授权及必要的网页状态，源照片另行备份。Git 不是这些运行数据的备份。

迁移已有实例必须先停旧控制器、备份数据库、核对路径及批次归属，再启用新实例；不能两套控制器同时处理同一照片或数据库。
