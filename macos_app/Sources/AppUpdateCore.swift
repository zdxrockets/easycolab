import Foundation
import CryptoKit

// Algorithms in this file must stay aligned with scripts/app_update_lib.py
// and tests/fixtures/app_update/vectors.json.

enum AppUpdateConfig {
    static let owner = "zdxrockets"
    static let repository = "easycolab"
    static let bundleIdentifier = "app.easycolab.studio.macos"
    static let assetName = "EasyColab-macOS.zip"
    static let manifestName = "EasyColab-macOS.manifest.json"
    static let minimumOS = "13.0"
    static let manifestSchema = 1
    static let maxZipBytes: Int64 = 250 * 1024 * 1024

    static var releasesAPIURL: URL {
        URL(string: "https://api.github.com/repos/\(owner)/\(repository)/releases")!
    }

    static func downloadURL(tag: String, asset: String = assetName) -> URL? {
        let url = URL(string: "https://github.com/\(owner)/\(repository)/releases/download/\(tag)/\(asset)")
        guard let url, isAllowedDownloadURL(url, expectedAsset: asset) else { return nil }
        return url
    }
}

struct AppVersionIdentity: Equatable {
    var version: String
    var releaseTag: String
    var gitCommit: String
    var hasBuildStamp: Bool
    var bundleIdentifier: String
    var minOS: String

    var displayVersion: String {
        if hasBuildStamp {
            let short = String(normalizedCommit.prefix(7))
            let tag = releaseTag.isEmpty ? version : releaseTag
            return "\(tag) (\(short))"
        }
        if version.isEmpty {
            return "unknown"
        }
        return "\(version) (no build stamp)"
    }

    var normalizedCommit: String {
        Self.normalizeCommit(gitCommit)
    }

    static func normalizeCommit(_ value: String) -> String {
        value.lowercased().filter { ("0"..."9").contains($0) || ("a"..."f").contains($0) }
    }

    static func loadFromBundle(_ bundle: Bundle = .main) -> AppVersionIdentity {
        if let url = bundle.url(forResource: "EasyColabBuildInfo", withExtension: "json"),
           let data = try? Data(contentsOf: url),
           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            return identity(from: json, bundle: bundle)
        }
        let info = bundle.infoDictionary ?? [:]
        let version = stringValue(info["CFBundleShortVersionString"])
        let tag = stringValue(info["EasyColabReleaseTag"])
        let commit = stringValue(info["EasyColabGitCommit"])
        let stamp = AppVersionIdentity.normalizeCommit(commit).count >= 7
        return AppVersionIdentity(
            version: version,
            releaseTag: tag,
            gitCommit: commit,
            hasBuildStamp: stamp,
            bundleIdentifier: stringValue(info["CFBundleIdentifier"], fallback: AppUpdateConfig.bundleIdentifier),
            minOS: stringValue(info["LSMinimumSystemVersion"], fallback: AppUpdateConfig.minimumOS)
        )
    }

    static func identity(from json: [String: Any], bundle: Bundle) -> AppVersionIdentity {
        let info = bundle.infoDictionary ?? [:]
        let commit = stringValue(json["git_commit"])
        let version = firstNonEmpty(stringValue(json["version"]), stringValue(info["CFBundleShortVersionString"]))
        let tag = firstNonEmpty(stringValue(json["release_tag"]), stringValue(info["EasyColabReleaseTag"]))
        return AppVersionIdentity(
            version: version,
            releaseTag: tag,
            gitCommit: commit,
            hasBuildStamp: normalizeCommit(commit).count >= 7,
            bundleIdentifier: firstNonEmpty(stringValue(json["bundle_id"]), stringValue(info["CFBundleIdentifier"]), AppUpdateConfig.bundleIdentifier),
            minOS: firstNonEmpty(stringValue(json["min_os"]), stringValue(info["LSMinimumSystemVersion"]), AppUpdateConfig.minimumOS)
        )
    }
}

struct AppUpdateManifest: Equatable {
    var schema: Int
    var repository: String
    var releaseTag: String
    var version: String
    var gitCommit: String
    var assetName: String
    var assetSHA256: String
    var assetURL: URL
    var bundleIdentifier: String
    var minOS: String
    var universal: Bool
    var notes: String
    var builtAt: String
    var signing: String
    var signingNote: String

    var identity: AppVersionIdentity {
        AppVersionIdentity(
            version: version,
            releaseTag: releaseTag,
            gitCommit: gitCommit,
            hasBuildStamp: AppVersionIdentity.normalizeCommit(gitCommit).count >= 7,
            bundleIdentifier: bundleIdentifier,
            minOS: minOS
        )
    }

    static func parse(data: Data) throws -> AppUpdateManifest {
        guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw AppUpdateError.invalidManifest(["not_json"])
        }
        let errors = validateManifestJSON(json)
        if !errors.isEmpty {
            throw AppUpdateError.invalidManifest(errors)
        }
        guard let url = URL(string: stringValue(json["asset_url"])) else {
            throw AppUpdateError.invalidManifest(["missing_asset_url"])
        }
        return AppUpdateManifest(
            schema: intValue(json["schema"]),
            repository: stringValue(json["repository"]),
            releaseTag: stringValue(json["release_tag"]),
            version: stringValue(json["version"]),
            gitCommit: stringValue(json["git_commit"]),
            assetName: stringValue(json["asset_name"]),
            assetSHA256: stringValue(json["asset_sha256"]).lowercased(),
            assetURL: url,
            bundleIdentifier: stringValue(json["bundle_id"]),
            minOS: stringValue(json["min_os"], fallback: AppUpdateConfig.minimumOS),
            universal: (json["universal"] as? Bool) ?? true,
            notes: stringValue(json["notes"]),
            builtAt: stringValue(json["built_at"]),
            signing: stringValue(json["signing"], fallback: "adhoc"),
            signingNote: stringValue(json["signing_note"])
        )
    }
}

struct UpdateComparison: Equatable {
    var status: String
    var newer: Bool
    var reason: String
    var currentStamped: Bool
}

enum AppUpdateError: Error, Equatable {
    case invalidManifest([String])
    case urlNotAllowed
    case sha256Mismatch
    case unsafeZip([String])
    case bundleValidation([String])
    case notWritable
}

func parseSemver(_ text: String) -> (Int, Int, Int)? {
    var raw = text.trimmingCharacters(in: .whitespacesAndNewlines)
    if raw.first == "v" || raw.first == "V" {
        raw.removeFirst()
    }
    let parts = raw.split(separator: ".", omittingEmptySubsequences: false)
    guard parts.count == 3,
          let a = Int(parts[0]), let b = Int(parts[1]), let c = Int(parts[2]),
          parts.allSatisfy({ $0.allSatisfy(\.isNumber) }) else {
        return nil
    }
    return (a, b, c)
}

func compareSemver(_ left: String, _ right: String) -> Int? {
    guard let a = parseSemver(left), let b = parseSemver(right) else { return nil }
    if a < b { return -1 }
    if a > b { return 1 }
    return 0
}

func commitsMatch(_ current: String, _ candidate: String) -> Bool {
    let a = AppVersionIdentity.normalizeCommit(current)
    let b = AppVersionIdentity.normalizeCommit(candidate)
    guard a.count >= 7, b.count >= 7 else { return false }
    return a.hasPrefix(b) || b.hasPrefix(a)
}

func compareIdentities(_ current: AppVersionIdentity, _ candidate: AppVersionIdentity) -> UpdateComparison {
    let currentVersion = firstNonEmpty(current.version, current.releaseTag)
    let candidateVersion = firstNonEmpty(candidate.version, candidate.releaseTag)
    let stamped = current.hasBuildStamp
    guard let cmp = compareSemver(currentVersion, candidateVersion) else {
        return UpdateComparison(status: "incomparable", newer: false, reason: "unparsed_version", currentStamped: stamped)
    }
    if cmp < 0 {
        return UpdateComparison(status: "newer_version", newer: true, reason: "semver", currentStamped: stamped)
    }
    if cmp > 0 {
        return UpdateComparison(status: "older_or_equal", newer: false, reason: "installed_is_newer", currentStamped: stamped)
    }
    if stamped && !candidate.gitCommit.isEmpty {
        if commitsMatch(current.gitCommit, candidate.gitCommit) {
            return UpdateComparison(status: "same_build", newer: false, reason: "same_commit", currentStamped: true)
        }
        return UpdateComparison(status: "same_version_different_build", newer: true, reason: "commit_differs", currentStamped: true)
    }
    return UpdateComparison(status: "same_version_unstamped", newer: false, reason: "equal_version_without_commit_stamp", currentStamped: false)
}

func isAllowedReleasesAPIURL(_ url: URL) -> Bool {
    guard url.scheme?.lowercased() == "https",
          url.host?.lowercased() == "api.github.com",
          url.user == nil, url.password == nil,
          url.port == nil || url.port == 443 else {
        return false
    }
    let path = url.path.hasSuffix("/") ? String(url.path.dropLast()) : url.path
    let prefix = "/repos/\(AppUpdateConfig.owner)/\(AppUpdateConfig.repository)/releases"
    return path == prefix || path.hasPrefix(prefix + "/")
}

func isAllowedDownloadURL(_ url: URL, expectedAsset: String = AppUpdateConfig.assetName) -> Bool {
    isAllowedGitHubReleaseAssetURL(url, allowedNames: [expectedAsset])
}

func isAllowedManifestDownloadURL(_ url: URL) -> Bool {
    isAllowedGitHubReleaseAssetURL(url, allowedNames: [AppUpdateConfig.manifestName])
}

func isAllowedGitHubReleaseAssetURL(_ url: URL, allowedNames: [String]) -> Bool {
    guard url.scheme?.lowercased() == "https",
          url.host?.lowercased() == "github.com",
          url.user == nil, url.password == nil,
          url.port == nil || url.port == 443,
          (url.query == nil || url.query?.isEmpty == true),
          (url.fragment == nil || url.fragment?.isEmpty == true) else {
        return false
    }
    let parts = url.path.split(separator: "/").map(String.init)
    guard parts.count == 6,
          parts[0] == AppUpdateConfig.owner,
          parts[1] == AppUpdateConfig.repository,
          parts[2] == "releases",
          parts[3] == "download" else {
        return false
    }
    let tag = parts[4]
    let asset = parts[5]
    guard isSafeTag(tag), allowedNames.contains(asset) else {
        return false
    }
    if asset.hasSuffix(".zip") {
        return isSafeZipAsset(asset)
    }
    // Manifest file name is a fixed constant, not attacker-controlled.
    return asset == AppUpdateConfig.manifestName
}

func isGitHubAssetRedirectHost(_ host: String?) -> Bool {
    guard let host = host?.lowercased() else { return false }
    return host == "objects.githubusercontent.com"
        || host == "release-assets.githubusercontent.com"
        || host.hasSuffix(".githubusercontent.com")
}

func isSafeTag(_ tag: String) -> Bool {
    guard !tag.isEmpty, !tag.contains("..") else { return false }
    return tag.unicodeScalars.allSatisfy { CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "._-")).contains($0) }
}

func isSafeZipAsset(_ name: String) -> Bool {
    guard name.hasSuffix(".zip"), !name.contains("/"), !name.contains("..") else { return false }
    return name.unicodeScalars.allSatisfy { CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "._-")).contains($0) }
}

func validateManifestJSON(_ json: [String: Any]) -> [String] {
    var errors: [String] = []
    if intValue(json["schema"]) != AppUpdateConfig.manifestSchema {
        errors.append("unsupported_schema")
    }
    if parseSemver(stringValue(json["version"])) == nil {
        errors.append("invalid_version")
    }
    let tag = stringValue(json["release_tag"])
    if !isSafeTag(tag) {
        errors.append("invalid_release_tag")
    }
    if stringValue(json["bundle_id"]) != AppUpdateConfig.bundleIdentifier {
        errors.append("bundle_id_mismatch")
    }
    if stringValue(json["asset_name"]) != AppUpdateConfig.assetName {
        errors.append("unexpected_asset_name")
    }
    let sha = stringValue(json["asset_sha256"])
    if sha.count != 64 || sha.unicodeScalars.contains(where: { !CharacterSet(charactersIn: "0123456789abcdefABCDEF").contains($0) }) {
        errors.append("invalid_sha256")
    }
    let assetURL = stringValue(json["asset_url"])
    if let url = URL(string: assetURL) {
        if !isAllowedDownloadURL(url, expectedAsset: AppUpdateConfig.assetName) {
            errors.append("asset_url_not_allowed")
        }
    } else {
        errors.append("asset_url_not_allowed")
    }
    let repo = stringValue(json["repository"])
    if !repo.isEmpty && repo != "\(AppUpdateConfig.owner)/\(AppUpdateConfig.repository)" {
        errors.append("repository_mismatch")
    }
    return errors
}

func sha256Hex(ofFile url: URL) throws -> String {
    let handle = try FileHandle(forReadingFrom: url)
    defer { try? handle.close() }
    var hasher = SHA256()
    while autoreleasepool(invoking: {
        let chunk = handle.readData(ofLength: 1024 * 1024)
        if !chunk.isEmpty {
            hasher.update(data: chunk)
            return true
        }
        return false
    }) {}
    let digest = hasher.finalize()
    return digest.map { String(format: "%02x", $0) }.joined()
}

struct ZipEntryInfo: Equatable {
    var name: String
    var isSymlink: Bool
    var isEncrypted: Bool
}

func zipEntryIsSafe(_ name: String, isSymlink: Bool = false, isEncrypted: Bool = false) -> (Bool, String) {
    if isEncrypted { return (false, "encrypted") }
    if isSymlink { return (false, "symlink") }
    if name.isEmpty || name.contains("\0") { return (false, "empty_or_null") }
    let normalized = name.replacingOccurrences(of: "\\", with: "/")
    if normalized.hasPrefix("/") || normalized.hasPrefix("~") { return (false, "absolute") }
    let parts = normalized.split(separator: "/").map(String.init).filter { $0 != "." && !$0.isEmpty }
    if parts.contains("..") { return (false, "dotdot") }
    guard let top = parts.first else { return (false, "empty") }
    if !["EasyColab.app", "__MACOSX", ".DS_Store"].contains(top) {
        return (false, "unexpected_root")
    }
    return (true, "ok")
}

func inspectZipCentralDirectory(at url: URL) throws -> [ZipEntryInfo] {
    let data = try Data(contentsOf: url, options: [.mappedIfSafe])
    guard data.count >= 22 else { throw AppUpdateError.unsafeZip(["zip_too_small"]) }
    // Reject ZIP64 locator signature near the end.
    if data.range(of: Data([0x50, 0x4b, 0x06, 0x07])) != nil {
        throw AppUpdateError.unsafeZip(["zip64_unsupported"])
    }
    guard let eocd = findEndOfCentralDirectory(data) else {
        throw AppUpdateError.unsafeZip(["missing_eocd"])
    }
    var entries: [ZipEntryInfo] = []
    var offset = eocd.cdOffset
    for _ in 0..<eocd.totalEntries {
        guard offset + 46 <= data.count, u32(data, offset) == 0x02014b50 else {
            throw AppUpdateError.unsafeZip(["bad_central_directory"])
        }
        let flag = u16(data, offset + 8)
        let nameLen = Int(u16(data, offset + 28))
        let extraLen = Int(u16(data, offset + 30))
        let commentLen = Int(u16(data, offset + 32))
        let extAttr = u32(data, offset + 38)
        let nameStart = offset + 46
        guard nameStart + nameLen <= data.count else {
            throw AppUpdateError.unsafeZip(["truncated_name"])
        }
        let nameData = data.subdata(in: nameStart..<(nameStart + nameLen))
        let name = String(data: nameData, encoding: .utf8) ?? String(decoding: nameData, as: UTF8.self)
        let unixMode = (extAttr >> 16) & 0xFFFF
        let isSymlink = (unixMode & 0o170000) == 0o120000
        let isEncrypted = (flag & 0x1) != 0
        entries.append(ZipEntryInfo(name: name, isSymlink: isSymlink, isEncrypted: isEncrypted))
        offset = nameStart + nameLen + extraLen + commentLen
    }
    return entries
}

func validateZipEntries(_ entries: [ZipEntryInfo]) -> [String] {
    var errors: [String] = []
    var hasBinary = false
    for entry in entries {
        let (ok, reason) = zipEntryIsSafe(entry.name, isSymlink: entry.isSymlink, isEncrypted: entry.isEncrypted)
        if !ok {
            errors.append("\(entry.name):\(reason)")
        }
        if entry.name.replacingOccurrences(of: "\\", with: "/") == "EasyColab.app/Contents/MacOS/EasyColab" {
            hasBinary = true
        }
    }
    if !hasBinary {
        errors.append("missing_app_binary")
    }
    return errors
}

private struct EOCD {
    var totalEntries: UInt16
    var cdOffset: Int
}

private func findEndOfCentralDirectory(_ data: Data) -> EOCD? {
    let signature: [UInt8] = [0x50, 0x4b, 0x05, 0x06]
    let minSize = 22
    guard data.count >= minSize else { return nil }
    let start = max(0, data.count - minSize - 65535)
    var i = data.count - minSize
    while i >= start {
        if data[i] == signature[0], data[i + 1] == signature[1],
           data[i + 2] == signature[2], data[i + 3] == signature[3] {
            let commentLen = Int(u16(data, i + 20))
            if i + minSize + commentLen == data.count {
                return EOCD(totalEntries: u16(data, i + 10), cdOffset: Int(u32(data, i + 16)))
            }
        }
        if i == 0 { break }
        i -= 1
    }
    return nil
}

private func u16(_ data: Data, _ offset: Int) -> UInt16 {
    UInt16(data[offset]) | (UInt16(data[offset + 1]) << 8)
}

private func u32(_ data: Data, _ offset: Int) -> UInt32 {
    UInt32(data[offset])
        | (UInt32(data[offset + 1]) << 8)
        | (UInt32(data[offset + 2]) << 16)
        | (UInt32(data[offset + 3]) << 24)
}

func compareOSVersion(_ required: String, _ actual: OperatingSystemVersion) -> Bool {
    var raw = required.trimmingCharacters(in: .whitespacesAndNewlines)
    if raw.split(separator: ".").count == 2 { raw += ".0" }
    guard let need = parseSemver(raw) else { return false }
    let have = (actual.majorVersion, actual.minorVersion, actual.patchVersion)
    return have >= need
}

func inspectMachOArchitectures(at url: URL) throws -> [String] {
    let data = try Data(contentsOf: url, options: [.mappedIfSafe])
    guard data.count >= 8 else { throw AppUpdateError.bundleValidation(["binary_too_small"]) }
    let magic = u32BE(data, 0)
    // Fat (CAFEBABE / BEBAFECA) or thin 64-bit (FEEDFACF / CFFAEDFE)
    if magic == 0xCAFEBABE || magic == 0xBEBAFECA {
        let swap = magic == 0xBEBAFECA
        let nfat = swap ? u32(data, 4) : u32BE(data, 4)
        var arches: [String] = []
        var offset = 8
        for _ in 0..<nfat {
            guard offset + 20 <= data.count else { break }
            let cpu = swap ? u32(data, offset) : u32BE(data, offset)
            if let name = cpuTypeName(cpu) { arches.append(name) }
            offset += 20
        }
        return arches
    }
    let thin = magic == 0xFEEDFACF || magic == 0xCFFAEDFE || magic == 0xFEEDFACE || magic == 0xCEFAEDFE
    if thin {
        let swap = magic == 0xCFFAEDFE || magic == 0xCEFAEDFE
        let cpu = swap ? u32(data, 4) : u32BE(data, 4)
        if let name = cpuTypeName(cpu) { return [name] }
    }
    throw AppUpdateError.bundleValidation(["unrecognized_macho"])
}

private func u32BE(_ data: Data, _ offset: Int) -> UInt32 {
    (UInt32(data[offset]) << 24)
        | (UInt32(data[offset + 1]) << 16)
        | (UInt32(data[offset + 2]) << 8)
        | UInt32(data[offset + 3])
}

private func cpuTypeName(_ cpu: UInt32) -> String? {
    let raw = cpu & 0xFF
    if raw == 0x0C { return "arm64" } // CPU_TYPE_ARM64
    if raw == 0x07 { return "x86_64" } // CPU_TYPE_X86
    return nil
}

func currentRuntimeArchitecture() -> String {
    #if arch(arm64)
    return "arm64"
    #elseif arch(x86_64)
    return "x86_64"
    #else
    return "unknown"
    #endif
}

func validateExtractedApp(at appURL: URL, manifest: AppUpdateManifest, currentOS: OperatingSystemVersion) -> [String] {
    var errors: [String] = []
    let fm = FileManager.default
    var isDir: ObjCBool = false
    if !fm.fileExists(atPath: appURL.path, isDirectory: &isDir) || !isDir.boolValue {
        return ["missing_app_bundle"]
    }
    if let symlinkErrors = rejectSymlinks(at: appURL) {
        errors.append(contentsOf: symlinkErrors)
    }
    let plistURL = appURL.appendingPathComponent("Contents/Info.plist")
    guard let plist = NSDictionary(contentsOf: plistURL) as? [String: Any] else {
        errors.append("missing_info_plist")
        return errors
    }
    let bundleId = stringValue(plist["CFBundleIdentifier"])
    if bundleId != AppUpdateConfig.bundleIdentifier || bundleId != manifest.bundleIdentifier {
        errors.append("bundle_id_mismatch")
    }
    let shortVersion = stringValue(plist["CFBundleShortVersionString"])
    if let cmp = compareSemver(shortVersion, manifest.version), cmp != 0 {
        errors.append("version_mismatch")
    } else if parseSemver(shortVersion) == nil {
        errors.append("version_mismatch")
    }
    let minOS = stringValue(plist["LSMinimumSystemVersion"], fallback: manifest.minOS)
    if !compareOSVersion(minOS, currentOS) {
        errors.append("os_too_old")
    }
    let binary = appURL.appendingPathComponent("Contents/MacOS/EasyColab")
    do {
        let arches = try inspectMachOArchitectures(at: binary)
        let need = currentRuntimeArchitecture()
        if !arches.contains(need) {
            errors.append("arch_mismatch")
        }
    } catch {
        errors.append("arch_unreadable")
    }
    return errors
}

func rejectSymlinks(at root: URL) -> [String]? {
    let fm = FileManager.default
    guard let enumerator = fm.enumerator(
        at: root,
        includingPropertiesForKeys: [.isSymbolicLinkKey],
        options: [.skipsPackageDescendants],
        errorHandler: nil
    ) else {
        // Also walk files inside the .app; skipsPackageDescendants would skip contents if root is the .app
        return rejectSymlinksDeep(at: root)
    }
    // If root itself is the .app bundle, the enumerator with skipsPackageDescendants is wrong.
    return rejectSymlinksDeep(at: root)
}

func rejectSymlinksDeep(at root: URL) -> [String]? {
    let fm = FileManager.default
    guard let enumerator = fm.enumerator(
        at: root,
        includingPropertiesForKeys: [.isSymbolicLinkKey],
        options: [],
        errorHandler: nil
    ) else {
        return ["enum_failed"]
    }
    var errors: [String] = []
    for case let url as URL in enumerator {
        let values = try? url.resourceValues(forKeys: [.isSymbolicLinkKey])
        if values?.isSymbolicLink == true {
            errors.append("symlink:\(url.path)")
        }
    }
    return errors.isEmpty ? nil : errors
}

// MARK: - small helpers

func stringValue(_ raw: Any?, fallback: String = "") -> String {
    if let value = raw as? String { return value }
    if let value = raw as? NSNumber { return value.stringValue }
    return fallback
}

func intValue(_ raw: Any?) -> Int {
    if let value = raw as? Int { return value }
    if let value = raw as? NSNumber { return value.intValue }
    if let value = raw as? String, let parsed = Int(value) { return parsed }
    return 0
}

func firstNonEmpty(_ values: String...) -> String {
    values.first { !$0.isEmpty } ?? ""
}
