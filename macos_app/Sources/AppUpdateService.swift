import Foundation
import AppKit

struct AvailableUpdate: Equatable {
    var manifest: AppUpdateManifest
    var releaseName: String
    var htmlURL: URL?
    var comparison: UpdateComparison
}

enum AppUpdatePhase: Equatable {
    case idle
    case checking
    case upToDate(String)
    case noRecognizableRelease
    case available(AvailableUpdate)
    case networkError(String)
    case downloading(AvailableUpdate, Double)
    case verifying
    case installing
    case failed(String)
    case readyToRelaunch(URL)
}

@MainActor
final class AppUpdateController: NSObject, ObservableObject {
    @Published var phase: AppUpdatePhase = .idle
    @Published var showSheet: Bool = false
    @Published var pendingConfirm: AvailableUpdate?
    @Published var confirmBusyCloud: Bool = false

    let currentIdentity: AppVersionIdentity
    var cloudTaskActive: () -> Bool = { false }

    private var session: URLSession!
    private var downloadProgress: Double = 0
    private var downloadContinuation: CheckedContinuation<URL, Error>?

    override init() {
        currentIdentity = AppVersionIdentity.loadFromBundle()
        super.init()
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 30
        config.httpAdditionalHeaders = [
            "Accept": "application/vnd.github+json",
            "User-Agent": "EasyColab-macOS-Updater",
            "X-GitHub-Api-Version": "2022-11-28"
        ]
        session = URLSession(configuration: config, delegate: self, delegateQueue: nil)
    }

    var currentVersionLabel: String { currentIdentity.displayVersion }

    var isBusy: Bool {
        switch phase {
        case .checking, .downloading, .verifying, .installing:
            return true
        default:
            return false
        }
    }

    func openSheet() {
        showSheet = true
    }

    func checkForUpdates() async {
        guard !isBusy else { return }
        phase = .checking
        do {
            guard isAllowedReleasesAPIURL(AppUpdateConfig.releasesAPIURL) else {
                phase = .failed("Internal URL allowlist rejected the releases API.")
                return
            }
            let (data, response) = try await session.data(from: AppUpdateConfig.releasesAPIURL)
            if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
                throw URLError(.badServerResponse)
            }
            guard let payload = try JSONSerialization.jsonObject(with: data) as? [[String: Any]] else {
                throw URLError(.cannotParseResponse)
            }
            guard let release = pickLatestStableRelease(payload) else {
                phase = .noRecognizableRelease
                return
            }
            let manifest = try await fetchManifest(from: release)
            let comparison = compareIdentities(currentIdentity, manifest.identity)
            if comparison.newer {
                phase = .available(AvailableUpdate(
                    manifest: manifest,
                    releaseName: stringValue(release["name"], fallback: manifest.releaseTag),
                    htmlURL: URL(string: stringValue(release["html_url"])),
                    comparison: comparison
                ))
            } else if comparison.status == "incomparable" {
                phase = .failed(L("无法比较版本号，请确认发布 manifest 使用 x.y.z 版本。", "Cannot compare versions. The release manifest must use an x.y.z version."))
            } else {
                phase = .upToDate(manifest.identity.displayVersion)
            }
        } catch {
            phase = .networkError(userFacingNetworkError(error))
        }
    }

    func requestInstall(_ update: AvailableUpdate) {
        pendingConfirm = update
        confirmBusyCloud = cloudTaskActive()
    }

    func cancelPendingInstall() {
        pendingConfirm = nil
        confirmBusyCloud = false
    }

    func confirmAndInstall(_ update: AvailableUpdate) async {
        pendingConfirm = nil
        confirmBusyCloud = false
        await downloadVerifyAndInstall(update)
    }

    private func downloadVerifyAndInstall(_ update: AvailableUpdate) async {
        let manifest = update.manifest
        guard isAllowedDownloadURL(manifest.assetURL, expectedAsset: manifest.assetName) else {
            phase = .failed(L("下载地址不在允许列表内（仅限本仓库 GitHub HTTPS 发布资产）。", "The download URL is not allowed. Only this repository's GitHub HTTPS release assets can be used."))
            return
        }
        phase = .downloading(update, 0)
        let fm = FileManager.default
        let updatesDir = LocalRuntimePaths.appSupportDir.appendingPathComponent("Updates", isDirectory: true)
        do {
            try fm.createDirectory(at: updatesDir, withIntermediateDirectories: true)
            let zipURL = updatesDir.appendingPathComponent(AppUpdateConfig.assetName)
            if fm.fileExists(atPath: zipURL.path) {
                try fm.removeItem(at: zipURL)
            }
            let downloaded = try await downloadFile(from: manifest.assetURL, to: zipURL)
            phase = .verifying
            let digest = try sha256Hex(ofFile: downloaded)
            if digest != manifest.assetSHA256.lowercased() {
                try? fm.removeItem(at: downloaded)
                throw AppUpdateError.sha256Mismatch
            }
            let entries = try inspectZipCentralDirectory(at: downloaded)
            let zipErrors = validateZipEntries(entries)
            if !zipErrors.isEmpty {
                try? fm.removeItem(at: downloaded)
                throw AppUpdateError.unsafeZip(zipErrors)
            }
            let extractRoot = updatesDir.appendingPathComponent("extract-\(UUID().uuidString)", isDirectory: true)
            try fm.createDirectory(at: extractRoot, withIntermediateDirectories: true)
            try runProcess(launchPath: "/usr/bin/ditto", arguments: ["-x", "-k", downloaded.path, extractRoot.path])
            if let linkErrors = rejectSymlinksDeep(at: extractRoot) {
                try? fm.removeItem(at: extractRoot)
                try? fm.removeItem(at: downloaded)
                throw AppUpdateError.unsafeZip(linkErrors)
            }
            let extractedApp = extractRoot.appendingPathComponent("EasyColab.app")
            let bundleErrors = validateExtractedApp(
                at: extractedApp,
                manifest: manifest,
                currentOS: ProcessInfo.processInfo.operatingSystemVersion
            )
            if !bundleErrors.isEmpty {
                try? fm.removeItem(at: extractRoot)
                throw AppUpdateError.bundleValidation(bundleErrors)
            }
            try runProcess(launchPath: "/usr/bin/xattr", arguments: ["-cr", extractedApp.path])
            phase = .installing
            let installed = try installReplacingCurrentApp(with: extractedApp)
            try? fm.removeItem(at: extractRoot)
            try? fm.removeItem(at: downloaded)
            phase = .readyToRelaunch(installed)
        } catch AppUpdateError.notWritable {
            phase = .failed(L(
                "当前应用目录不可写（例如在受保护的 /Applications 位置）。请把 EasyColab.app 移到你拥有写权限的位置后再试，或在验证通过后用 Finder 手动替换。配置、模型与密钥在 ~/Library/Application Support/EasyColab，不会随替换删除。",
                "The current app location is not writable (for example a protected /Applications copy). Move EasyColab.app to a folder you own and try again, or replace it manually in Finder after verification. Settings, models, and keys live in ~/Library/Application Support/EasyColab and are not deleted by a replacement."
            ))
        } catch AppUpdateError.sha256Mismatch {
            phase = .failed(L("下载文件的 SHA-256 与 manifest 不一致，已中止安装。", "The downloaded file SHA-256 does not match the manifest. Installation was aborted."))
        } catch let AppUpdateError.unsafeZip(details) {
            phase = .failed(L("安装包未通过安全检查：", "The package failed the safety checks: ") + details.joined(separator: ", "))
        } catch let AppUpdateError.bundleValidation(details) {
            phase = .failed(L("安装包内容校验失败：", "Bundle validation failed: ") + details.joined(separator: ", "))
        } catch {
            phase = .failed(userFacingNetworkError(error))
        }
    }

    func relaunchAndQuit(at url: URL) {
        let configuration = NSWorkspace.OpenConfiguration()
        NSWorkspace.shared.openApplication(at: url, configuration: configuration) { _, _ in
            DispatchQueue.main.async {
                NSApp.terminate(nil)
            }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) {
            NSApp.terminate(nil)
        }
    }

    // MARK: - GitHub

    private func pickLatestStableRelease(_ releases: [[String: Any]]) -> [String: Any]? {
        var best: [String: Any]?
        var bestVer: (Int, Int, Int)?
        for rel in releases {
            if (rel["draft"] as? Bool) == true { continue }
            if (rel["prerelease"] as? Bool) == true { continue }
            let tag = stringValue(rel["tag_name"])
            guard let ver = parseSemver(tag) else { continue }
            let names = Set(((rel["assets"] as? [[String: Any]]) ?? []).map { stringValue($0["name"]) })
            guard names.contains(AppUpdateConfig.assetName), names.contains(AppUpdateConfig.manifestName) else {
                continue
            }
            if bestVer == nil || ver > bestVer! {
                best = rel
                bestVer = ver
            }
        }
        return best
    }

    private func fetchManifest(from release: [String: Any]) async throws -> AppUpdateManifest {
        let assets = (release["assets"] as? [[String: Any]]) ?? []
        guard let manifestAsset = assets.first(where: { stringValue($0["name"]) == AppUpdateConfig.manifestName }),
              let manifestURL = URL(string: stringValue(manifestAsset["browser_download_url"])) else {
            throw AppUpdateError.invalidManifest(["missing_manifest_asset"])
        }
        guard isAllowedManifestDownloadURL(manifestURL) else { throw AppUpdateError.urlNotAllowed }
        let (data, response) = try await session.data(from: manifestURL)
        if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
            throw URLError(.badServerResponse)
        }
        return try AppUpdateManifest.parse(data: data)
    }

    // MARK: - Download & install

    private func downloadFile(from url: URL, to destination: URL) async throws -> URL {
        try await withCheckedThrowingContinuation { continuation in
            self.downloadContinuation = continuation
            self.downloadProgress = 0
            let task = self.session.downloadTask(with: url)
            task.taskDescription = destination.path
            task.resume()
        }
    }

    private func installReplacingCurrentApp(with newApp: URL) throws -> URL {
        let fm = FileManager.default
        let current = Bundle.main.bundleURL.standardizedFileURL
        let parent = current.deletingLastPathComponent()
        var writable: Bool = false
        writable = fm.isWritableFile(atPath: parent.path) && fm.isWritableFile(atPath: current.path)
        if !writable {
            // Leave a verified copy in Downloads for manual Finder replacement.
            if let downloads = fm.urls(for: .downloadsDirectory, in: .userDomainMask).first {
                let staged = downloads.appendingPathComponent("EasyColab.app")
                if fm.fileExists(atPath: staged.path) {
                    try? fm.removeItem(at: staged)
                }
                try fm.copyItem(at: newApp, to: staged)
                NSWorkspace.shared.activateFileViewerSelecting([staged])
            }
            throw AppUpdateError.notWritable
        }
        let backupName = "EasyColab.app.bak"
        let backupURL = parent.appendingPathComponent(backupName)
        var resulting: NSURL?
        try fm.replaceItem(
            at: current,
            withItemAt: newApp,
            backupItemName: backupName,
            options: [.withoutDeletingBackupItem],
            resultingItemURL: &resulting
        )
        let installed = (resulting as URL?) ?? current
        // Best-effort: keep one backup next to the app; do not touch Application Support.
        _ = backupURL
        return installed
    }

    private func runProcess(launchPath: String, arguments: [String]) throws {
        // Argument array only — never interpolate untrusted strings into a shell.
        let process = Process()
        process.executableURL = URL(fileURLWithPath: launchPath)
        process.arguments = arguments
        process.standardOutput = Pipe()
        process.standardError = Pipe()
        try process.run()
        process.waitUntilExit()
        if process.terminationStatus != 0 {
            throw NSError(domain: "EasyColabUpdater", code: Int(process.terminationStatus), userInfo: [
                NSLocalizedDescriptionKey: "\(launchPath) exited \(process.terminationStatus)"
            ])
        }
    }

    private func userFacingNetworkError(_ error: Error) -> String {
        if let urlError = error as? URLError {
            switch urlError.code {
            case .notConnectedToInternet, .networkConnectionLost, .timedOut:
                return L("网络不可用或请求超时，请稍后重试。", "The network is unavailable or the request timed out. Please try again.")
            case .cannotFindHost, .cannotConnectToHost, .dnsLookupFailed:
                return L("无法连接到 GitHub，请检查网络后重试。", "Could not reach GitHub. Check your network and try again.")
            default:
                break
            }
        }
        return L("检查更新失败：", "Update check failed: ") + error.localizedDescription
    }
}

extension AppUpdateController: URLSessionTaskDelegate, URLSessionDownloadDelegate {
    nonisolated func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        guard let url = request.url, url.scheme?.lowercased() == "https" else {
            completionHandler(nil)
            return
        }
        let original = task.originalRequest?.url
        let originalAllowed = original.map { isAllowedDownloadURL($0) || isAllowedManifestDownloadURL($0) } ?? false
        let nextAllowed = isAllowedDownloadURL(url) || isAllowedManifestDownloadURL(url)
            || (originalAllowed && isGitHubAssetRedirectHost(url.host))
        completionHandler(nextAllowed ? request : nil)
    }

    nonisolated func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didWriteData bytesWritten: Int64, totalBytesWritten: Int64, totalBytesExpectedToWrite: Int64) {
        let fraction: Double
        if totalBytesExpectedToWrite > 0 {
            fraction = min(1.0, Double(totalBytesWritten) / Double(totalBytesExpectedToWrite))
        } else {
            fraction = 0
        }
        if totalBytesWritten > AppUpdateConfig.maxZipBytes {
            downloadTask.cancel()
        }
        Task { @MainActor in
            if case .downloading(let update, _) = self.phase {
                self.phase = .downloading(update, fraction)
            }
        }
    }

    nonisolated func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {
        // The temporary file is deleted when this method returns; move it synchronously.
        let destPath = downloadTask.taskDescription
        do {
            guard let destPath else {
                throw NSError(domain: "EasyColabUpdater", code: -1, userInfo: [NSLocalizedDescriptionKey: "missing download destination"])
            }
            let dest = URL(fileURLWithPath: destPath)
            if FileManager.default.fileExists(atPath: dest.path) {
                try FileManager.default.removeItem(at: dest)
            }
            try FileManager.default.moveItem(at: location, to: dest)
            Task { @MainActor in
                if let cont = self.downloadContinuation {
                    self.downloadContinuation = nil
                    cont.resume(returning: dest)
                }
            }
        } catch {
            Task { @MainActor in
                if let cont = self.downloadContinuation {
                    self.downloadContinuation = nil
                    cont.resume(throwing: error)
                }
            }
        }
    }

    nonisolated func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard let error else { return }
        Task { @MainActor in
            if let cont = self.downloadContinuation {
                self.downloadContinuation = nil
                cont.resume(throwing: error)
            }
        }
    }
}
