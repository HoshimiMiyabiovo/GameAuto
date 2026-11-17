pragma Singleton
import QtQuick

// 2026-11-17 Codex 风中性调色板（浅色）——手绘元素（行、分隔线、徽章、对话框）
// 的唯一色值来源；HuskarUI 库控件的颜色由 Main.qml 的 HusTheme 引导从同一套
// 基色派生，两边必须保持一致。改主题先改这里 + Main.qml 的 install 基色。
QtObject {
    // ── 背景 ──
    readonly property color bgApp: "#f7f8fa"          // 页面底（窗口色）
    readonly property color bgSurface: "#ffffff"      // 卡片
    readonly property color bgSubtle: "#f6f6f7"       // 表头/次级填充
    readonly property color bgHover: "#f4f4f5"        // 悬停/按钮填充
    readonly property color bgSelected: "#ecfdf5"     // 选中态（翡翠浅底）
    // ── 边框 ──
    readonly property color hairline: "#ececee"       // 发丝线（分隔）
    readonly property color border: "#e4e4e7"         // 常规边框
    readonly property color borderStrong: "#d4d4d8"   // 强边框
    // ── 文字 ──
    readonly property color text: "#18181b"
    readonly property color textSecondary: "#71717a"
    readonly property color textTertiary: "#a1a1aa"
    readonly property color textDisabled: "#c4c4c8"
    // ── 语义色 ──
    readonly property color accent: "#059669"         // 主强调（翡翠）
    readonly property color accentStrong: "#047857"
    readonly property color success: "#16a34a"
    readonly property color warning: "#d97706"
    readonly property color danger: "#dc2626"
    readonly property color info: "#2563eb"
    readonly property color successSoft: "#ecfdf5"
    readonly property color warningSoft: "#fffbeb"
    readonly property color dangerSoft: "#fef2f2"
    readonly property color infoSoft: "#eff6ff"
}
