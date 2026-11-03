import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import QtQuick.Dialogs
import HuskarUI.Basic

// 脚本管理：版本状态 + 托管目录一键安装/更新 + 根目录与每工具路径自定义
Item {
    id: root
    objectName: "toolManagerRoot"
    readonly property real fs: Application.window ? Application.window.fontScale : 1

    property var tools: []
    // 当前安装任务 {tool_key, phase, received, total, message}；空 = 无任务
    property var installState: ({})

    Component.onCompleted: loadTools()
    onVisibleChanged: if (visible) loadTools()

    Connections {
        target: bridge
        function onToolInstallChanged(json) {
            try {
                root.installState = JSON.parse(json)
            } catch(e) {
                root.installState = {}
            }
        }
        function onScriptVersionsChanged(json) {
            loadTools()  // 版本检查完成后同步本地/最新版本
        }
    }

    function loadTools() {
        var json = bridge.getToolManagerData()
        try {
            root.tools = JSON.parse(json)
        } catch(e) {
            root.tools = []
        }
    }

    function statusMeta(row) {
        switch (row.status) {
            case "update":        return { text: "有更新",       state: HusTag.State_Warning }
            case "latest":        return { text: "已是最新",     state: HusTag.State_Success }
            case "local_unknown": return { text: "本地版本未知", state: HusTag.State_Default }
            case "no_release":    return { text: "无发布版",     state: HusTag.State_Default }
            case "no_repo":       return { text: "仅本地版本",   state: HusTag.State_Default }
            case "no_path":       return { text: "未安装",       state: HusTag.State_Default }
        }
        return { text: "未知", state: HusTag.State_Default }
    }

    function phaseText(state) {
        switch (state.phase) {
            case "starting":    return "准备中..."
            case "downloading": return "下载中 " + fmtMB(state.received) + " / " + fmtMB(state.total)
            case "extracting":  return "解压中..."
            case "installing":  return "换血替换中..."
            case "registering": return "写入配置..."
            case "done":        return state.message || "安装完成"
            case "error":       return "失败: " + state.message
        }
        return state.message || ""
    }

    function fmtMB(bytes) {
        var n = Number(bytes || 0)
        if (n >= 1048576) return (n / 1048576).toFixed(1) + "MB"
        return Math.round(n / 1024) + "KB"
    }

    function isInstalling(toolKey) {
        return root.installState.tool_key === toolKey
               && root.installState.phase !== "idle"
               && root.installState.phase !== "done"
               && root.installState.phase !== "error"
    }

    function installBusy() {
        var p = root.installState.phase || ""
        return p !== "" && p !== "idle" && p !== "done" && p !== "error"
    }

    function filePathFromUrl(url) {
        var s = url.toString()
        if (s.startsWith("file:///")) s = s.substring(8)
        return decodeURIComponent(s)
    }

    FolderDialog {
        id: rootDirDialog
        title: "选择脚本安装根目录"
        onAccepted: {
            bridge.setToolInstallRoot(root.filePathFromUrl(selectedFolder))
            root.loadTools()
        }
    }

    FolderDialog {
        id: toolDirDialog
        property string toolKey: ""
        title: "选择该工具的安装目录"
        onAccepted: {
            bridge.setToolOverridePath(toolKey, root.filePathFromUrl(selectedFolder))
            root.loadTools()
        }
    }

    ScrollView {
        anchors.fill: parent
        clip: true

        ColumnLayout {
            width: parent.width
            spacing: 14

            // 说明 + 全局安装根目录
            HusCard {
                Layout.fillWidth: true
                padding: 20

                contentItem: ColumnLayout {
                    spacing: 10

                    HusText {
                        text: "脚本管理"
                        font.pixelSize: Math.round(22 * root.fs)
                        font.bold: true
                    }
                    HusText {
                        text: "一键下载安装/更新支持自动安装的脚本（ok-ww / ok-ef / ok-nte / March7th / BetterGI）；" +
                              "其余项目请按发布页指引手动安装。安装只写托管目录，已检测到的外部安装不会被改动；" +
                              "脚本或游戏运行中时拒绝安装。"
                        font.pixelSize: Math.round(13 * root.fs)
                        color: "#6d6257"
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                    RowLayout {
                        spacing: 8
                        Layout.fillWidth: true

                        HusText {
                            text: "安装根目录"
                            font.pixelSize: Math.round(13 * root.fs)
                        }
                        HusInput {
                            Layout.fillWidth: true
                            readOnly: true
                            text: (root.tools.length > 0 ? root.tools[0].install_root : "") || "(解析失败)"
                            placeholderText: "data/script_tools"
                        }
                        HusButton {
                            text: "浏览"
                            type: HusButton.Type_Outlined
                            onClicked: rootDirDialog.open()
                        }
                        HusButton {
                            text: "恢复默认"
                            type: HusButton.Type_Outlined
                            onClicked: {
                                bridge.setToolInstallRoot("")
                                root.loadTools()
                            }
                        }
                    }
                }
            }

            // 每工具一张卡
            Repeater {
                model: root.tools

                delegate: HusCard {
                    id: toolCard
                    required property var modelData
                    Layout.fillWidth: true
                    padding: 14

                    contentItem: ColumnLayout {
                        id: card
                        spacing: 8

                        property var meta: root.statusMeta(modelData)
                        property bool installing: root.isInstalling(modelData.tool_key)
                        property real progress: {
                            var st = root.installState
                            if (!root.isInstalling(modelData.tool_key) || !st.total) return 0
                            return Math.min(1.0, Number(st.received) / Number(st.total))
                        }

                        // 标题行：名称 + 状态
                        RowLayout {
                            spacing: 8
                            Layout.fillWidth: true

                            HusText {
                                text: {
                                    var disp = modelData.display_name || ""
                                    var label = modelData.label || ""
                                    if (!disp || label.indexOf(disp) >= 0) return label
                                    return disp + " · " + label
                                }
                                font.pixelSize: Math.round(15 * root.fs)
                                font.bold: true
                                elide: Text.ElideRight
                                Layout.maximumWidth: Math.round(300 * root.fs)
                            }
                            HusTag {
                                text: modelData.stale ? (card.meta.text + "（缓存）") : card.meta.text
                                tagState: card.meta.state
                            }
                            Item { Layout.fillWidth: true }
                            HusText {
                                text: "本地 " + (modelData.local ? "v" + modelData.local : "未知")
                                      + "   最新 " + (modelData.latest || "—")
                                font.pixelSize: Math.round(12 * root.fs)
                                color: modelData.has_update ? "#b66f1f" : "#5a4d3f"
                            }
                        }

                        // 路径行
                        RowLayout {
                            spacing: 8
                            Layout.fillWidth: true

                            HusText {
                                text: modelData.path || "未安装"
                                font.pixelSize: Math.round(12 * root.fs)
                                color: modelData.path ? "#5a4d3f" : "#8a7a68"
                                elide: Text.ElideMiddle
                                Layout.fillWidth: true
                            }
                            HusButton {
                                visible: modelData.tool_key !== ""
                                text: "自定义目录"
                                type: HusButton.Type_Outlined
                                onClicked: {
                                    toolDirDialog.toolKey = modelData.tool_key
                                    toolDirDialog.open()
                                }
                            }
                        }

                        // 进度条（安装任务进行中显示）
                        ColumnLayout {
                            visible: card.installing
                            spacing: 4
                            Layout.fillWidth: true

                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: Math.round(6 * root.fs)
                                radius: 3
                                color: "#e8e0d5"

                                Rectangle {
                                    width: parent.width * card.progress
                                    height: parent.height
                                    radius: 3
                                    color: "#2f7d4a"
                                }
                            }
                            HusText {
                                text: root.phaseText(root.installState)
                                font.pixelSize: Math.round(12 * root.fs)
                                color: "#6d6257"
                            }
                        }

                        // 操作行
                        RowLayout {
                            spacing: 8
                            Layout.fillWidth: true

                            HusButton {
                                visible: modelData.downloadable
                                text: {
                                    if (card.installing) return "安装中..."
                                    if (modelData.path) return "更新" + (modelData.latest ? "到 " + modelData.latest : "")
                                    return "下载安装" + (modelData.latest ? " " + modelData.latest : "")
                                }
                                type: HusButton.Type_Primary
                                enabled: modelData.downloadable && !root.installBusy()
                                onClicked: bridge.installTool(modelData.tool_key)
                            }
                            HusButton {
                                visible: card.installing
                                text: "取消"
                                type: HusButton.Type_Outlined
                                onClicked: bridge.cancelInstall(modelData.tool_key)
                            }
                            HusButton {
                                visible: modelData.repo_url !== ""
                                text: "打开发布页"
                                type: HusButton.Type_Outlined
                                onClicked: Qt.openUrlExternally(modelData.repo_url)
                            }
                            HusText {
                                visible: !modelData.downloadable && modelData.tool_key !== ""
                                text: "该项目暂不支持自动安装，请按发布页指引手动安装"
                                font.pixelSize: Math.round(12 * root.fs)
                                color: "#8a7a68"
                            }
                            Item { Layout.fillWidth: true }
                        }
                    }
                }
            }

            Item { Layout.preferredHeight: 4 }
        }
    }
}
