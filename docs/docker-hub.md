# Push 后自动构建和发布 Docker Hub

仓库的 CI and Docker Hub 工作流先运行测试和 Compose 校验，再并行构建 controller、dashboard，推送后校验远程镜像 manifest。Docker 官方 Actions 用法参考：[GitHub Actions](https://docs.docker.com/build/ci/github-actions/)。

## 一次性配置

在 Docker Hub 创建两个镜像仓库并选择需要的可见性：

- YOUR_NAMESPACE/sync-pic-controller
- YOUR_NAMESPACE/sync-pic-dashboard

在 GitHub 仓库 Settings → Secrets and variables → Actions 设置：

| 类型 | 名称 | 内容 |
| --- | --- | --- |
| Variable | DOCKERHUB_USERNAME | 用于 Docker Hub 登录的用户名。也兼容同名 Secret。 |
| Secret | DOCKERHUB_TOKEN | 对目标仓库有推送权限的 Docker Hub access token。 |
| Variable，可选 | DOCKERHUB_NAMESPACE | 发布到的组织或命名空间，不填则使用登录用户名。 |
| Variable，可选 | DOCKER_PLATFORMS | 默认 linux/amd64,linux/arm64；只需要本机架构可设 linux/amd64。 |

用户名、组织及令牌不写入源码。组织发布时，登录账号需要拥有对应组织仓库的权限。缺少凭据时，工作流会报错并终止发布。

## 触发和标签

| 事件 | 行为 |
| --- | --- |
| push 到 main | 测试成功后发布 latest、main、sha-短提交号。 |
| push 版本标签，如 v1.2.3 | 发布 1.2.3、1.2、sha-短提交号；latest 继续跟随 main。 |
| Pull Request 到 main | 测试并构建 amd64，不登录、不推送，不需要提供 Secret。 |
| Actions 手动 Run workflow | 根据选择的分支构建和发布，可用于补齐凭据后的重试。 |

默认发布 amd64、arm64 多架构镜像，QEMU 构建 arm64 可能比 amd64 更慢。两个镜像分别缓存构建层。Syncthing、Pixel 和 MySQL 仍使用各自上游镜像，不重新发布。

main 的发布串行执行，避免较旧的 main 构建覆盖更新的 latest。生产部署建议固定版本、sha 标签或镜像摘要，不把可变标签当成不可变版本。

## 使用预构建镜像

在部署机器的私有 .env 中填写：

    PICSYNC_CONTROLLER_IMAGE=docker.io/YOUR_NAMESPACE/sync-pic-controller:latest
    PICSYNC_DASHBOARD_IMAGE=docker.io/YOUR_NAMESPACE/sync-pic-dashboard:latest

然后：

    docker compose pull preflight controller dashboard
    docker compose up -d --no-build --pull never controller dashboard syncthing

preflight 和可选容器 ADB 共用 controller 镜像。启用了 usb profile 时同时拉取和更新 adb；启用了 phone profile 时其镜像仍来自原上游。私有镜像需先在部署机器 docker login。

发布只上传镜像到 Docker Hub，不通过工作流自动连接部署机器。目标机器仍使用自己的 .env、数据库和持久目录，GitHub Actions 不传送运行配置或照片。

凭据首次设置后，可以在 GitHub Actions 对失败的运行选择 Re-run failed jobs，或手动触发工作流。镜像仓库中出现相应标签并通过工作流末尾 manifest 校验，才表示发布完成。
