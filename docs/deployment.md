# Docker Compose 部署

本教程使用 Docker Hub 已发布镜像。你只需下载部署文件、填写配置并连接手机，无需安装 Python 或构建镜像。所有命令均在 Linux 部署主机执行；另有说明时除外。

## 1. 准备主机和部署文件

需要 Linux、Git、Docker Engine 和 Docker Compose v2，以及一部能使用 Google 相册备份的 Android 手机。主机需要能访问 Docker Hub，手机需要能完成 Google 相册云端备份。先确认 Docker 可用：

```sh
docker version
docker compose version
```

下载项目并准备配置：

```sh
git clone https://github.com/wylyeak/sync_pic.git
cd sync_pic
sh scripts/prepare.sh
```

克隆仓库是为了获取 Compose、配置模板及配套挂载文件。准备脚本只创建私有配置和 `state/` 目录，不启动服务，也不修改已有配置。后续命令均在 `sync_pic` 目录执行。

## 2. 填写照片路径和镜像地址

编辑 `.env`，将下面示例替换为自己的值：

```dotenv
PICSYNC_PHOTO_ROOT=/srv/photos
PICSYNC_SOURCE_SUBDIR=source
PICSYNC_TARGET_SUBDIR=target
PICSYNC_MEDIA_FS=ext4
PICSYNC_UID=1000
PICSYNC_GID=1000
PICSYNC_CONTROLLER_IMAGE=docker.io/wangyulie/sync-pic-controller:latest
PICSYNC_DASHBOARD_IMAGE=docker.io/wangyulie/sync-pic-dashboard:latest
```

此例表示原照片在 `/srv/photos/source`，专用同步目录在 `/srv/photos/target`。两个目录必须已经存在、互不重叠，且位于同一挂载；目标不能混入其他用途的文件。不要移动或清空已有源照片。NAS 应先在宿主机挂载好，再启动服务。

用下面的命令核实实际文件系统类型和当前用户身份：

```sh
findmnt -T /srv/photos/source
findmnt -T /srv/photos/target
id -u
id -g
```

将实际类型填入 `PICSYNC_MEDIA_FS`，例如 `nfs`、`nfs4`、`ext4`、`xfs` 或 `btrfs`，不要照抄示例。UID/GID 应匹配媒体和本地 `state/` 的访问权限；更改后要检查已有文件权限。`state/` 应放在可靠的本机存储中。

Compose 已将源和目标的共同父目录一次性挂载到控制器，保留硬链接所需的挂载关系。不要改成分别挂载两个目录。Syncthing 只能看到目标 `/sync/photos`，不能获得源或共同父目录。

`latest` 方便首次安装。长期运行建议在拉取后按第 9 节固定摘要，避免后续拉取时无意更换版本。控制器和看板应选用同一提交的镜像。

```sh
docker compose config --quiet
docker compose pull preflight controller dashboard syncthing
docker compose run --rm --pull never preflight
```

最后一条是只读目录检查，不写照片或数据库。不存在的目录、路径穿越、符号链接、重叠、挂载别名或文件系统类型不符都会导致失败。请修复配置后重试，不要跳过检查。NAS 未挂载时的本地空目录不能代替真实媒体目录；容器检查也不能完全替代宿主机的挂载依赖。

完整参数见 [配置参考](configuration.md)。常规部署的数据库、设备和路径值放在 `.env`；两个应用 TOML 通常可先沿用默认值。

## 3. 准备数据库（二选一）

### 新部署：使用内置 MySQL

没有现成数据库时，使用 Compose 提供的 MySQL 8.4。在 `.env` 中设置：

```dotenv
COMPOSE_PROFILES=database
PICSYNC_MYSQL_HOST=127.0.0.1
PICSYNC_MYSQL_PORT=3306
PICSYNC_MYSQL_DATABASE=picsync
PICSYNC_MYSQL_USER=picsync
PICSYNC_MYSQL_PASSWORD='替换为控制器的强密码'
PICSYNC_DASHBOARD_MYSQL_USER=picsync_reader
PICSYNC_DASHBOARD_MYSQL_PASSWORD='替换为另一个强密码'
MYSQL_ROOT_PASSWORD='替换为管理员强密码'
```

宿主机已有服务占用 3306 时，将 `PICSYNC_MYSQL_PORT` 改为空闲端口。两个业务账号必须不同，名称仅允许字母、数字、下划线，最长 64 字符，密码不能为空。将 profile 写入 `.env` 后，后续命令会持续启用该组件。

```sh
docker compose pull mysql
docker compose up -d --no-build --pull never --wait mysql
docker compose ps mysql
```

首次启动自动创建业务库、控制器账号和独立看板只读账号。数据保存在 `state/mysql`，数据库端口只开放在主机回环地址。

初始化变量只对空数据目录生效。修改 `.env` 不会更改已有账号密码或授权，已有账号需通过 SQL 修改。不要删除数据库目录来解决登录问题。

### 已有 MySQL：使用外部数据库

不启用 `database` profile，在 `.env` 填写现有服务地址、端口、库名及两个独立账号：

```dotenv
PICSYNC_MYSQL_HOST=db.example.test
PICSYNC_MYSQL_PORT=3306
PICSYNC_MYSQL_DATABASE=picsync
PICSYNC_MYSQL_USER=picsync
PICSYNC_MYSQL_PASSWORD='替换为控制器密码'
PICSYNC_DASHBOARD_MYSQL_USER=picsync_reader
PICSYNC_DASHBOARD_MYSQL_PASSWORD='替换为只读账号密码'
```

请数据库管理员按 `deploy/mysql-users.example.sql` 创建库和账号；自定义名称时同步修改授权 SQL。控制器需要自己的业务库权限，看板只能 SELECT，不能复用控制器账号。

应用使用 host 网络，可以连接本机或外部数据库，地址应填写主机能访问的地址，而不是 Docker 服务名。核实防火墙和账号来源限制。模板未配置 MySQL TLS 参数，跨不可信网络时需另外使用安全隧道。

## 4. 连接手机（ADB 二选一）

### 复用宿主机 ADB（默认）

在主机安装系统 ADB，手机开启开发者选项和 USB 调试，用 USB 连接主机，并在手机上确认调试授权：

```sh
adb devices -l
```

找到状态为 `device` 的手机，将其序列号填入 `.env` 的 `PICSYNC_SERIAL`。显示 `unauthorized` 时先解锁手机并确认授权；列表为空时检查 USB 连接和权限。

ADB 服务只应监听本机 5037，不要添加 `-a`。已有受管理的 ADB 服务可直接沿用。没有长期运行的服务时，可安装仓库提供的用户服务：

```sh
sh scripts/install-user-services.sh
systemctl --user daemon-reload
adb kill-server
systemctl --user enable --now picsync-adb.service
adb devices -l
```

`adb kill-server` 会中断当前 ADB 连接，只在接管已有临时服务时执行。脚本还会安装宿主机控制器的服务文件，但本教程只启用 ADB；不要启用 `picsync-controller.service`，避免与 Compose 控制器重复运行。

### 没有宿主机 ADB：使用容器 ADB

先停止宿主机 ADB 和其他持有手机 USB 的服务，确保不会再次自动启动。将 `.env` 中的 profiles 改为 `COMPOSE_PROFILES=database,usb`；使用外部数据库时仅填 `usb`。

```sh
docker compose pull adb
docker compose up -d --no-build --pull never adb
docker compose exec adb adb -H 127.0.0.1 -P 5037 devices -l
```

在手机确认授权，将显示的序列号填入 `PICSYNC_SERIAL`。授权保存在 `state/adb-auth`。控制器、可选手机网页和宿主机客户端连接同一 ADB 服务；不能同时启用另一套宿主机 ADB。

只有 `adb` 容器获得 USB 访问，以 root 运行并仅保留 DAC_OVERRIDE，未使用 privileged。无法访问设备时检查主机权限和安全策略，不要直接放宽为 privileged。

## 5. 配对 Syncthing 并启用相册备份

```sh
docker compose up -d --no-build --pull never syncthing
```

在部署主机打开 `http://127.0.0.1:8384/`，先设置 GUI 认证。如果从自己的电脑管理远端主机，可在自己电脑的终端建立 SSH 隧道，将 `user@server` 替换为部署主机的 SSH 地址：

```sh
ssh -N -L 8384:127.0.0.1:8384 -L 8027:127.0.0.1:8027 user@server
```

保持隧道运行，在自己电脑浏览器访问相同的本地地址。也可以使用已配置访问控制的反向代理。

1. 在手机安装并运行 Syncthing，与主机互相添加设备。
2. 主机添加文件夹，路径只能是 `/sync/photos`，文件夹 ID 使用 `photos`；如果更换 ID，同时修改 `controller/config.toml` 的 `syncthing.folder`。
3. 将该文件夹共享到手机的专用目录，允许双向同步删除，不要配置为忽略删除。
4. 将手机完整 Syncthing 设备 ID 填入 `.env` 的 `PICSYNC_SYNCTHING_DEVICE`。
5. 在手机 Google 相册中登录备份账号，启用这个同步目录的备份，确认测试照片能够完成云端备份。
6. 用专门的测试文件确认双向删除传播，并检查手机重启后的连接恢复。不要用源照片测试删除，也不要将源或共同父目录共享给 Syncthing。

控制器只读挂载 `state/syncthing/config`，从 XML 中读取本机 API 密钥。该配置包含敏感信息，应保持私有。

## 6. 检查并启动备份

确认数据库已启动、ADB 手机状态为 `device`，并且 Syncthing 两端已连接后，运行：

```sh
docker compose run --rm --pull never controller python controller/file_sync.py dry-run
```

此命令检查目录边界和 Syncthing 状态，不扫描入库、不释放空间、不删除照片，也不能验证完整的相册 UI 自动化流程。检查失败时先看错误信息并修复，不要直接启动控制器。

**下一条启动命令会执行真实备份、相册“释放空间”和目标清理。** 首次请先使用专用测试目录和少量照片完成实际演练，再切换正式目录。“释放空间”是 Google 相册全局操作，可能释放本批之外此前已备份的手机内容；建议使用专用手机。完整条件见 [工作流与限制](workflow.md)。

```sh
docker compose up -d --no-build --pull never controller dashboard
docker compose ps -a
docker compose logs --tail 100 controller
```

首次启动会自动建表和迁移。控制器应处于 running，看板随后变为 healthy；一次性 `preflight` 显示 `Exited (0)` 属于正常结果。打开 `http://127.0.0.1:8027/` 查看批次、备份进度、手机状态和异常。远程访问可沿用上一步 SSH 隧道或受保护的反向代理。

```sh
curl -f http://127.0.0.1:8027/health
```

健康接口应返回 `{"ok":true}`。它只确认网页服务可用，还需在看板和控制器日志中确认数据库、手机、挂载和同步状态正常。手机电量仅供展示，不参与清理判断。

## 7. 日常使用和可选手机网页

常用命令：

```sh
# 查看状态和最近日志
docker compose ps -a
docker compose logs --tail 100 controller dashboard
# 停止控制器的后续自动处理；Syncthing 仍会继续同步
docker compose stop controller
# 恢复控制器
docker compose up -d --no-build --pull never controller
```

默认关闭 Bark；需要异常推送时，在 `controller/config.toml` 启用 `[bark]`，并在 `.env` 设置 `PICSYNC_BARK_KEY`。可选看板快捷入口使用 `PICSYNC_PIXEL_URL` 和 `PICSYNC_SYNCTHING_URL`，留空则隐藏；URL 不得包含密码或令牌。配置修改后重新创建相应服务，参数详见 [配置参考](configuration.md)。

手机控制网页不是备份必需组件，也可沿用自己的手机控制服务。需要仓库提供的网页时，从 Google 官方 platform-tools 获取与 Linux 架构兼容的独立 `adb` 客户端，保存为 `state/adb-client`。不能直接复制依赖宿主机特有共享库的系统 ADB。将 `phone` 加入 `.env` 现有的 `COMPOSE_PROFILES`，保留已有选项，例如 `database,phone`。

```sh
chmod 755 state/adb-client
docker compose pull pixel
docker compose up -d --no-build --pull never pixel
```

网页默认仅监听主机 `127.0.0.1:8001`，按上游说明初始化登录账号和反向代理。容器不挂载 USB，包装器阻止它关闭或另起共享 ADB。该独立客户端方案按 Linux x86_64 组织，其他架构需验证上游支持；缓冲补丁针对特定上游版本，更新网页镜像后需重新验证登录和投屏。

## 8. 开机恢复和备份

长期容器使用 `restart: unless-stopped`。主机重启前，应配置 NAS 自动挂载和 ADB 自动启动；Docker 自动恢复容器不会重新执行完整 Compose 依赖图。宿主机用户 ADB 服务需要配置 linger 才能脱离登录运行，可由管理员执行 `sudo loginctl enable-linger "$USER"`。配置后分别验证手机重启重连和宿主机重启恢复。

备份应包含一致性的 MySQL 导出、`.env`、两个应用 TOML、Syncthing 状态、ADB 授权及必要的手机网页状态。源照片需另行备份。数据库运行中直接复制 `state/mysql` 不能替代一致性数据库备份。私有配置、授权和运行数据均不提交 Git。

迁移已有实例时，先停旧控制器、备份数据库，再核对路径和批次归属。不能让两套控制器同时处理同一照片或数据库。

## 9. 镜像版本、升级和回退

发布后的镜像在 Docker Hub：

- [Controller](https://hub.docker.com/r/wangyulie/sync-pic-controller/tags)
- [Dashboard](https://hub.docker.com/r/wangyulie/sync-pic-dashboard/tags)

`latest` 跟随 main，也提供 `sha-短提交号` 标签；版本标签发布规则见 [镜像发布说明](docker-hub.md)。升级时先确认该提交的 GitHub Actions 测试和两个镜像发布全部成功，再选择同一提交的两个镜像。

先备份数据库和私有配置，记录当前两个镜像地址并保留旧镜像。暂停控制器后，获取匹配的部署文件：

```sh
docker compose stop controller
git pull --ff-only
```

阅读对应版本的配置变化，再修改 `.env` 的两个镜像地址为所选版本。拉取后检查各镜像的提交号和摘要，下面以 `.env` 选用了 `latest` 为例：

```sh
docker compose pull preflight controller dashboard
docker image inspect wangyulie/sync-pic-controller:latest --format '{{ index .Config.Labels "org.opencontainers.image.revision" }} {{json .RepoDigests}}'
docker image inspect wangyulie/sync-pic-dashboard:latest --format '{{ index .Config.Labels "org.opencontainers.image.revision" }} {{json .RepoDigests}}'
```

选用其他标签时，替换上述 inspect 命令中的 `latest`。两个 revision 应一致且对应所选提交。将 `.env` 中的镜像地址分别改成各自 RepoDigests 输出的完整 `仓库@sha256:摘要`，再启动：

```sh
docker compose config --quiet
docker compose run --rm --pull never preflight
docker compose up -d --no-build --pull never controller dashboard
docker compose ps -a
curl -f http://127.0.0.1:8027/health
```

检查控制器日志和看板运行状态。启用了容器 ADB 时，它也共用 controller 镜像，需要拉取并更新 `adb`；这会短暂中断手机连接，应在恢复后核实设备状态。Syncthing、MySQL 和手机网页使用各自上游镜像，单独评估其升级。

更新失败时不要继续处理照片。恢复备份的镜像地址和匹配的配置，再用相同的 `up --no-build --pull never` 命令恢复旧服务。若新版本包含数据库迁移，应先确认旧版兼容性，必要时按该版本说明恢复一致性数据库备份。不要将 `down -v`、删除 `state/mysql` 或清理旧镜像作为常规升级步骤。

GitHub Actions 只发布镜像，不自动更新你的主机。无需为使用公开镜像配置 Docker Hub 推送令牌或 GitHub Secrets。
