# macOS 客户端检查更新与发布

本 fork 的 EasyColab macOS 客户端可以从界面检查并安装 **本仓库** [`zdxrockets/easycolab`](https://github.com/zdxrockets/easycolab) 的 GitHub Releases。它 **不会** 检查上游 `wy51ai/easycolab`，也 **不会** 在更新客户端时自动更新云端 `app.py`、清理云端缓存或重新部署 Colab。

这是实验性功能：box 上的 Linux 环境无法编译 SwiftUI，因此 macOS 实际编译、下载安装、权限不足回退和重启路径尚未在真机上跑通。

## 版本是怎样比较的

历史上仓库根目录 `VERSION` 曾停留在 `1.1.0`，而上游发布标签是 `1.1.1`，预编译包的 `CFBundleShortVersionString` 也仍是 `1.1.0`。因此客户端 **不能只比较** 短版本号。

从包含本功能的构建开始，每个 `.app` 都会带上 `Contents/Resources/EasyColabBuildInfo.json`（以及 Info.plist 里的 `EasyColabGitCommit` / `EasyColabReleaseTag`），包含：

- `version`（x.y.z，无无意义的 `latest`）
- `release_tag`（例如 `v1.1.2`）
- `git_commit`

比较规则（见 `scripts/app_update_lib.py` 与 `macos_app/Sources/AppUpdateCore.swift`）：

1. 只看本仓库 **非 draft、非 prerelease**，并且同时带有 `EasyColab-macOS.zip` 和 `EasyColab-macOS.manifest.json` 的 Release。缺少 manifest 的旧 zip（或只推了源码）不能自动安装。
2. 用 semver 比较 `version`（允许 `v` 前缀）。
3. 版本号相同且两边都有提交戳时，再比较 git commit：相同则已是最新；不同则提示「同版本不同构建」并允许更新。
4. 当前应用 **没有提交戳**（例如旧的 1.1.0 自编译或误标成 1.1.0 的 1.1.1 包）时，只有候选版本号更高才会提示更新，避免对同一 `1.1.0` 字符串反复提示。

**推送 `master` 或打开 PR 本身不会产生可安装包。** 必须打版本标签（或对已有标签手动运行 workflow）并得到带 manifest 的 Release。

## 签名与校验范围（请勿当成公证）

`macos_app/build_macos_app.sh` 使用 `codesign --force --deep --sign -`，即 **ad-hoc 签名**。这不是 Apple Developer ID 签名，也 **没有** notarization。

安装前客户端会：

- 只接受 `https://github.com/zdxrockets/easycolab/releases/download/<tag>/...`（以及 GitHub 对该资产的 HTTPS 重定向）
- 用 manifest 中的 SHA-256 校验 zip，防止损坏或被掉包
- 检查 zip 路径、拒绝 `..` / 绝对路径 / 符号链接 / 加密条目
- 校验 bundle id `app.easycolab.studio.macos`、当前 CPU 架构、最低系统版本与版本号

这些只能证明「这个 zip 与同一条 Release 上的 manifest 一致」，**不能**证明开发者身份。客户端 **不包含** GitHub PAT 或其他秘密。

## 首次如何用上这个按钮

你现在正在运行的 App（自编译 2f3a8ae / 官方 1.1.1 包）还没有检查更新按钮。需要 **先手动安装一个包含本功能的构建**，之后才能在应用内更新。

可选方式（都不要在未确认时自动覆盖当前 App）：

1. 在已安装 Xcode Command Line Tools 的 Mac 上检出本分支或合并后的 fork `master`，执行 `bash macos_app/build_macos_app.sh`，再打开生成的 `EasyColab.app`。
2. 在 bump `VERSION` 并推送 `vX.Y.Z` 标签后（见下文），从本仓库 Releases 下载带 manifest 的 `EasyColab-macOS.zip`，手动替换。ad-hoc 包若被隔离，可在确认来源后执行 `xattr -cr /path/to/EasyColab.app`。

用户配置、本地 venv 与密钥在 `~/Library/Application Support/EasyColab`，替换 `.app` 不会删除它们。

## 以后如何发布一个可被客户端识别的版本

不要依赖「一推 master 就发版」。fork 同步上游会频繁触发无意义且昂贵的 macOS runner。发布流程是：

1. 把仓库根目录 `VERSION` 改成即将发布的 `X.Y.Z`（必须与标签去掉 `v` 后的数字完全一致，否则 CI 会失败）。
2. 提交并推送到本 fork。
3. 打标签并推送，例如：

```bash
git tag v1.1.2
git push origin v1.1.2
```

4. GitHub Actions 工作流 `Build EasyColab macOS Release` 会在 **标签** `v*.*.*` 上构建 Universal zip、写入 `EasyColab-macOS.manifest.json` 和 `SHA256SUMS`，并挂到该标签的 Release。
5. 也可以在 Actions 里手动 `workflow_dispatch`，输入一个 **已存在** 的标签（例如 `v1.1.2`）。不会创建名为 `latest` 的浮动 prerelease。

**第一次启用该 workflow 需要你在仓库 Actions 中允许 GitHub Actions，并理解 macOS runner 会计费。本 PR 没有替你启动构建，也没有写入 Actions secrets。**

当前用于推送的 GitHub OAuth 令牌没有 `workflow` 范围，因此无法把 YAML 直接写入 `.github/workflows/`。完整文件在 [`docs/github-workflows/macos-release.yml`](github-workflows/macos-release.yml) 与 [`docs/github-workflows/python-tests.yml`](github-workflows/python-tests.yml)。启用时请将它们复制到 `.github/workflows/`（可用具有 `workflow` 范围的令牌推送，或在 GitHub 网页上添加）。复制前请确认不会在 push `master` 时启动 macOS 构建：发布 workflow 只应在 `v*.*.*` 标签或 `workflow_dispatch`（输入已有标签）时运行。


## 界面行为

- 菜单「检查更新…」或左侧栏「客户端」里的按钮会打开面板并只读检查 Releases。
- 若发现可安装更新，需要你再次确认后才会下载安装。
- 若此时有唤醒 / 下载权重 / 加载模型 / 生成 / Comfy 任务，确认框会额外警告：重启本机客户端可能打断界面状态，但不会自动停止云端实例。
- 目录不可写时，会提示把 App 移到可写位置，或把已校验的包放到「下载」并用 Finder 手动替换，不会循环弹出 sudo。
- 替换使用 `FileManager.replaceItem` 并保留旁边的 `EasyColab.app.bak`；失败应尽量回到原包。

## 与其他 PR 的关系

`feat/h3-image-hybrid-offload`（PR #1）改的是云端 `app.py` 加载策略。本功能只涉及 macOS 客户端、workflow、测试和文档。两支分支基于同一 fork `master`，通常可以分别合并，互不包含对方未合并的改动。客户端更新之后，仍需另行确认才会把新的 `app.py` 部署到云端。
