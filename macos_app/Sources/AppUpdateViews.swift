import SwiftUI
import AppKit

extension Notification.Name {
    static let openEasyColabUpdater = Notification.Name("EasyColab.OpenUpdater")
}

struct AppUpdateSheet: View {
    @ObservedObject var updater: AppUpdateController

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(L("检查更新", "Check for Updates"))
                .font(.title3.weight(.semibold))

            VStack(alignment: .leading, spacing: 4) {
                Text(L("当前客户端：", "This app: ") + updater.currentVersionLabel)
                    .font(.callout)
                Text(L(
                    "只检查本仓库 zdxrockets/easycolab 的 GitHub Releases，不会检查上游 wy51ai。源码 push 不等于已有可安装包。客户端更新不会自动更新已运行的云端 app.py，也不会清理云端缓存或重新部署 Colab。",
                    "Checks GitHub Releases on zdxrockets/easycolab only, not upstream wy51ai. Pushing source does not publish an installable build. Updating the client does not update a running cloud app.py, clear cloud caches, or redeploy Colab."
                ))
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            }

            statusBlock

            Text(L(
                "安装包为 ad-hoc 签名，不是 Apple 公证。校验只证明 zip 的 SHA-256 与本仓库同一条 Release 的 manifest 一致，不能证明开发者身份。",
                "Builds are ad-hoc signed, not Apple-notarized. Verification only proves the zip SHA-256 matches the manifest on the same GitHub Release; it does not prove developer identity."
            ))
            .font(.caption2)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)

            HStack {
                Spacer()
                Button(L("关闭", "Close")) { updater.showSheet = false }
                    .keyboardShortcut(.cancelAction)
                Button(checkButtonTitle) {
                    Task { await updater.checkForUpdates() }
                }
                .disabled(updater.isBusy)
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(minWidth: 460, idealWidth: 520, maxWidth: 560)
        .alert(
            L("确认安装更新并重启客户端？", "Install the update and restart the app?"),
            isPresented: Binding(
                get: { updater.pendingConfirm != nil },
                set: { if !$0 { updater.cancelPendingInstall() } }
            )
        ) {
            Button(L("取消", "Cancel"), role: .cancel) { updater.cancelPendingInstall() }
            Button(L("下载并安装", "Download & Install")) {
                if let update = updater.pendingConfirm {
                    Task { await updater.confirmAndInstall(update) }
                }
            }
        } message: {
            Text(confirmMessage)
        }
    }

    @ViewBuilder
    private var statusBlock: some View {
        switch updater.phase {
        case .idle:
            labeled(L("尚未检查。", "Not checked yet."), systemImage: "questionmark.circle")
        case .checking:
            HStack(spacing: 8) {
                ProgressView()
                Text(L("正在检查更新…", "Checking for updates…"))
            }
        case .upToDate(let latest):
            labeled(L("已是最新。仓库最新可安装版：", "You are up to date. Latest installable release: ") + latest, systemImage: "checkmark.circle")
        case .noRecognizableRelease:
            labeled(L(
                "没有找到带 manifest 与 SHA-256 的可安装 macOS 发布。仅推送源码或缺少 EasyColab-macOS.manifest.json 的 Release 不能自动安装。",
                "No installable macOS release with a manifest and SHA-256 was found. A source-only push, or a Release missing EasyColab-macOS.manifest.json, cannot be installed automatically."
            ), systemImage: "info.circle")
        case .available(let update):
            VStack(alignment: .leading, spacing: 8) {
                labeled(
                    L("发现新版本 ", "Update available: ") + update.manifest.identity.displayVersion,
                    systemImage: "arrow.down.app"
                )
                if !update.manifest.notes.isEmpty {
                    Text(update.manifest.notes)
                        .font(.caption)
                        .textSelection(.enabled)
                }
                Text(L(
                    "状态：\(update.comparison.status)。下载安装前会校验 HTTPS 来源与 SHA-256。",
                    "Status: \(update.comparison.status). HTTPS origin and SHA-256 are verified before install."
                ))
                .font(.caption)
                .foregroundStyle(.secondary)
                Button(L("下载并安装…", "Download & Install…")) {
                    updater.requestInstall(update)
                }
                .disabled(updater.isBusy)
            }
        case .networkError(let message):
            labeled(message, systemImage: "wifi.slash")
        case .downloading(_, let fraction):
            VStack(alignment: .leading, spacing: 6) {
                Text(L("正在下载…", "Downloading…"))
                ProgressView(value: fraction)
                Text("\(Int((fraction * 100).rounded()))%")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        case .verifying:
            HStack(spacing: 8) {
                ProgressView()
                Text(L("正在校验 SHA-256、压缩包路径与应用包…", "Verifying SHA-256, zip paths, and the app bundle…"))
            }
        case .installing:
            HStack(spacing: 8) {
                ProgressView()
                Text(L("正在替换应用包…", "Replacing the app bundle…"))
            }
        case .failed(let message):
            labeled(message, systemImage: "exclamationmark.triangle")
        case .readyToRelaunch(let url):
            VStack(alignment: .leading, spacing: 8) {
                labeled(L("已安装新版本，需要重启客户端。云端实例不会被这次重启自动关闭或重新部署。", "The new version is installed. The app needs to restart. This does not stop or redeploy your cloud instance."), systemImage: "checkmark.circle")
                Button(L("重启以完成更新", "Restart to Finish")) {
                    updater.relaunchAndQuit(at: url)
                }
            }
        }
    }

    private var checkButtonTitle: String {
        if case .checking = updater.phase {
            return L("检查中…", "Checking…")
        }
        return L("检查更新", "Check for Updates")
    }

    private var confirmMessage: String {
        let updateNote: String
        if let update = updater.pendingConfirm {
            updateNote = L("将下载并安装 ", "This will download and install ") + update.manifest.identity.displayVersion + "."
        } else {
            updateNote = ""
        }
        let busy = updater.confirmBusyCloud
            ? L(" 当前似乎有唤醒、下载、加载或生成任务正在进行，更新会重启本机客户端，可能打断这些界面状态；云端任务不一定停止。", " A wake, download, load, or generation task appears to be running. Updating restarts this Mac app and may interrupt that UI state; the cloud job may keep running.")
            : ""
        let rest = L(" 不会请求管理员密码。用户配置与本地密钥在 Application Support 中，不会被覆盖。", " No administrator password is requested. Settings and local keys in Application Support are left in place.")
        return updateNote + busy + rest
    }

    private func labeled(_ text: String, systemImage: String) -> some View {
        Label(text, systemImage: systemImage)
            .font(.callout)
            .fixedSize(horizontal: false, vertical: true)
    }
}

struct AppUpdateSidebarFooter: View {
    @ObservedObject var updater: AppUpdateController

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(L("客户端", "Client"))
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            Text(updater.currentVersionLabel)
                .font(.system(size: 11.5, weight: .medium, design: .monospaced))
                .textSelection(.enabled)
            Button {
                updater.openSheet()
            } label: {
                Text(buttonCaption)
            }
            .controlSize(.small)
            .disabled(updater.isBusy && updater.showSheet)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 4)
        .padding(.vertical, 2)
    }

    private var buttonCaption: String {
        switch updater.phase {
        case .available:
            return L("有更新，查看…", "Update available…")
        case .checking:
            return L("正在检查…", "Checking…")
        default:
            return L("检查更新…", "Check for Updates…")
        }
    }
}
