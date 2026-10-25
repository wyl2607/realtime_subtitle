#!/usr/bin/env bash
# Build rslite.app and a release zip for macOS Apple Silicon.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PACKAGE_DIR="${REPO_ROOT}/macos-native"
VERSION_FILE="${REPO_ROOT}/realtime_subtitle/version.py"
PRODUCT="rslite"
APP_BUNDLE="rslite.app"
BUNDLE_ID="com.realtimesubtitle.rslite"
DEFAULT_OUT_DIR="${REPO_ROOT}/dist/macos"

usage() {
    cat <<'EOF'
Usage: scripts/macos/build_rslite_app.sh [--out DIR]

Builds macos-native rslite in release mode, assembles rslite.app, signs it
with an ad-hoc signature, then writes rslite-<version>-macos-arm64.zip and
its .sha256 file.
EOF
}

die() {
    echo "error: $*" >&2
    exit 1
}

read_version() {
    local version_file="${1:-${VERSION_FILE}}"
    local line version

    [[ -f "${version_file}" ]] || die "version file not found: ${version_file}"
    line="$(grep -E '^[[:space:]]*__version__[[:space:]]*=' "${version_file}" | head -n 1 || true)"
    version="$(printf '%s\n' "${line}" | sed -E 's/^[[:space:]]*__version__[[:space:]]*=[[:space:]]*"([^"]+)".*$/\1/')"
    [[ "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "could not read semver __version__ from ${version_file}"
    printf '%s\n' "${version}"
}

read_macos_min_version() {
    local package_file="${1:-${PACKAGE_DIR}/Package.swift}"
    local text major minor

    [[ -f "${package_file}" ]] || die "Package.swift not found: ${package_file}"
    text="$(tr '\n' ' ' < "${package_file}")"
    if [[ "${text}" =~ \.macOS\(\.v([0-9]+)(_([0-9]+))?\) ]]; then
        major="${BASH_REMATCH[1]}"
        minor="${BASH_REMATCH[3]:-0}"
        printf '%s.%s\n' "${major}" "${minor}"
        return 0
    fi
    die "could not read macOS platform from ${package_file}"
}

write_info_plist() {
    local plist_path="$1"
    local version="$2"
    local minimum_system_version="$3"

    mkdir -p "$(dirname "${plist_path}")"
    cat > "${plist_path}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>zh_CN</string>
    <key>CFBundleDisplayName</key>
    <string>rslite</string>
    <key>CFBundleExecutable</key>
    <string>rslite</string>
    <key>CFBundleIdentifier</key>
    <string>${BUNDLE_ID}</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>rslite</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>${version}</string>
    <key>CFBundleVersion</key>
    <string>${version}</string>
    <key>LSMinimumSystemVersion</key>
    <string>${minimum_system_version}</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSAudioCaptureUsageDescription</key>
    <string>rslite 需要捕获系统播放声音，用于生成实时字幕和翻译。</string>
    <key>NSMicrophoneUsageDescription</key>
    <string>rslite 需要读取麦克风声音，用于麦克风输入模式下的实时字幕和翻译。</string>
    <key>NSScreenCaptureUsageDescription</key>
    <string>rslite 需要“屏幕与系统音频录制”权限来捕获系统音频，不会保存屏幕画面。</string>
    <key>NSSpeechRecognitionUsageDescription</key>
    <string>rslite 使用本机语音识别把音频转成文字，用于显示和翻译字幕。</string>
</dict>
</plist>
EOF
}

parse_args() {
    OUT_DIR="${DEFAULT_OUT_DIR}"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --out)
                [[ $# -ge 2 ]] || die "--out requires a directory"
                OUT_DIR="$2"
                shift 2
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                die "unknown argument: $1"
                ;;
        esac
    done
}

main() {
    parse_args "$@"

    [[ "$(uname -m)" == "arm64" ]] || die "rslite.app release packaging is for macOS arm64"
    command -v swift >/dev/null 2>&1 || die "swift not found"
    command -v codesign >/dev/null 2>&1 || die "codesign not found"
    command -v ditto >/dev/null 2>&1 || die "ditto not found"
    command -v shasum >/dev/null 2>&1 || die "shasum not found"

    local version minimum_system_version bin_dir binary app_path zip_name zip_path sha_path
    version="$(read_version)"
    minimum_system_version="$(read_macos_min_version)"
    mkdir -p "${OUT_DIR}"

    echo "[1/5] swift build -c release --package-path macos-native --product ${PRODUCT}"
    swift build -c release --package-path "${PACKAGE_DIR}" --product "${PRODUCT}"
    bin_dir="$(swift build -c release --package-path "${PACKAGE_DIR}" --product "${PRODUCT}" --show-bin-path)"
    binary="${bin_dir}/${PRODUCT}"
    [[ -x "${binary}" ]] || die "built binary not found: ${binary}"

    app_path="${OUT_DIR}/${APP_BUNDLE}"
    rm -rf "${app_path}"
    mkdir -p "${app_path}/Contents/MacOS" "${app_path}/Contents/Resources"

    echo "[2/5] assemble ${APP_BUNDLE}"
    install -m 755 "${binary}" "${app_path}/Contents/MacOS/${PRODUCT}"
    write_info_plist "${app_path}/Contents/Info.plist" "${version}" "${minimum_system_version}"
    printf 'APPL????' > "${app_path}/Contents/PkgInfo"
    if command -v plutil >/dev/null 2>&1; then
        plutil -lint "${app_path}/Contents/Info.plist" >/dev/null
    fi

    echo "[3/5] ad-hoc codesign"
    codesign --force --deep --sign - "${app_path}"
    codesign --verify --deep --strict "${app_path}"

    zip_name="rslite-${version}-macos-arm64.zip"
    zip_path="${OUT_DIR}/${zip_name}"
    sha_path="${zip_path}.sha256"
    rm -f "${zip_path}" "${sha_path}"

    echo "[4/5] zip ${zip_name}"
    (
        cd "${OUT_DIR}"
        ditto -c -k --keepParent "${APP_BUNDLE}" "${zip_name}"
        shasum -a 256 "${zip_name}" > "${zip_name}.sha256"
    )

    echo "[5/5] done"
    echo "app: ${app_path}"
    echo "zip: ${zip_path}"
    echo "sha256: ${sha_path}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
