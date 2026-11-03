import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import QtQuick.Dialogs
import HuskarUI.Basic

// First-run path setup wizard
Item {
    id: root
    objectName: "setupWizardRoot"
    readonly property real fs: Application.window ? Application.window.fontScale : 1

    property var setupItems: []
    property var scriptVersions: []
    property int currentEditIndex: -1

    readonly property int configuredCount: {
        var n = 0
        for (var i = 0; i < setupItems.length; i++) {
            if (setupItems[i].current_value || "") n++
        }
        return n
    }

    Component.onCompleted: {
        var json = bridge.getSetupItems()
        try {
            root.setupItems = JSON.parse(json)
        } catch(e) {
            root.setupItems = []
        }
        loadScriptVersions()
    }

    onVisibleChanged: if (visible) loadScriptVersions()

    // 后台检查（手动按钮 / 启动静默检查）完成后的推送
    Connections {
        target: bridge
        function onScriptVersionsChanged(json) {
            try {
                root.scriptVersions = JSON.parse(json)
            } catch(e) {
                root.scriptVersions = []
            }
        }
    }

    function loadScriptVersions() {
        var json = bridge.getScriptVersions()
        try {
            root.scriptVersions = JSON.parse(json)
        } catch(e) {
            root.scriptVersions = []
        }
    }

    // status → 状态 Tag 的文案与颜色（stale 时外面再追加"（缓存）"）。
    // 注意 HusTag 只有 Default/Success/Processing/Error/Warning 五种状态，
    // 中性信息一律 State_Default（State_Info 不存在，赋 undefined 会报错）
    function statusMeta(row) {
        switch (row.status) {
            case "update":        return { text: "有更新",       state: HusTag.State_Warning }
            case "latest":        return { text: "已是最新",     state: HusTag.State_Success }
            case "local_unknown": return { text: "本地版本未知", state: HusTag.State_Default }
            case "no_release":    return { text: "无发布版",     state: HusTag.State_Default }
            case "no_repo":       return { text: "仅本地版本",   state: HusTag.State_Default }
            case "no_path":       return { text: "未配置路径",   state: HusTag.State_Default }
        }
        return { text: "未知", state: HusTag.State_Default }
    }

    function groupLabelOf(key) {
        // 分组 key → 展示名（数据里 group 即展示名，这里留作兜底）
        return key || "其它"
    }

    function filePathFromUrl(url) {
        var s = url.toString()
        if (s.startsWith("file:///")) s = s.substring(8)
        return decodeURIComponent(s)
    }

    function collectAndSave() {
        var results = []
        for (var i = 0; i < pathRepeater.count; i++) {
            var item = pathRepeater.itemAt(i)
            if (!item) continue
            var data = root.setupItems[i]
            results.push({
                app_key: data.app_key,
                field: data.field,
                value: item.pathValue
            })
        }
        var ok = bridge.saveSetupPaths(JSON.stringify(results))
        if (ok) bridge.switchView("automation")
    }

    FileDialog {
        id: fileDialog
        title: "选择启动器文件"
        nameFilters: ["可执行文件 (*.exe *.bat)", "所有文件 (*)"]
        onAccepted: {
            if (root.currentEditIndex >= 0 && root.currentEditIndex < pathRepeater.count) {
                var item = pathRepeater.itemAt(root.currentEditIndex)
                if (item) {
                    item.pathValue = root.filePathFromUrl(fileDialog.selectedFile)
                    item.fileValid = bridge.checkFileExists(item.pathValue)
                }
            }
            root.currentEditIndex = -1
        }
    }

    ScrollView {
        anchors.fill: parent
        clip: true

        ColumnLayout {
            width: parent.width
            spacing: 14

            // Title card
            HusCard {
                Layout.fillWidth: true
                padding: 20

                contentItem: ColumnLayout {
                    spacing: 10
                    HusText {
                        text: "路径配置向导"
                        font.pixelSize: Math.round(22 * root.fs)
                        font.bold: true
                    }
                    HusText {
                        text: "配置各游戏工具的启动器路径，以及各独立/每月流程的程序路径。\n选择对应的 .exe 或 .bat 文件即可，可选项目可留空；随时可回到本页修改。\n★ = 必填，未标注 = 可选。白荆回廊游戏程序路径通常已自动带上。"
                        font.pixelSize: Math.round(13 * root.fs)
                        color: "#6d6257"
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                    RowLayout {
                        spacing: 10
                        HusTag {
                            text: "已配置 " + root.configuredCount + " / " + root.setupItems.length
                            tagState: root.configuredCount >= root.setupItems.length
                                      ? HusTag.State_Success : HusTag.State_Warning
                        }
                        HusText {
                            text: "带路径的输入框表示已配置；空白则尚未配置"
                            font.pixelSize: Math.round(12 * root.fs)
                            color: "#8a7a68"
                        }
                        Item { Layout.fillWidth: true }
                    }
                }
            }

            // Path items
            HusCard {
                Layout.fillWidth: true
                title: "工具路径"
                padding: 14

                contentItem: ColumnLayout {
                    spacing: 10
                    Layout.fillWidth: true

                    Repeater {
                        id: pathRepeater
                        model: root.setupItems

                        delegate: ColumnLayout {
                            id: pathItem
                            Layout.fillWidth: true
                            spacing: 4

                            property string pathValue: modelData.current_value || ""
                            property bool fileValid: pathValue !== "" && bridge.checkFileExists(pathValue)

                            // 分组标题：本行 group 与上一行不同时显示
                            HusText {
                                visible: index === 0 || root.setupItems[index - 1].group !== modelData.group
                                text: {
                                    var done = 0, total = 0
                                    for (var i = 0; i < root.setupItems.length; i++) {
                                        if (root.setupItems[i].group === modelData.group) {
                                            total++
                                            if (root.setupItems[i].current_value || "") done++
                                        }
                                    }
                                    return "【" + modelData.group + "】 " + done + "/" + total + " 已配置"
                                }
                                font.pixelSize: Math.round(14 * root.fs)
                                font.bold: true
                                color: "#5a4d3f"
                                Layout.topMargin: index === 0 ? 0 : 10
                            }

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 8

                                HusText {
                                    text: {
                                        var disp = modelData.display_name || ""
                                        var label = modelData.label || ""
                                        // display_name 为空或与 label 重复时不显示前缀
                                        if (!disp || label.indexOf(disp) >= 0) return label
                                        return disp + " · " + label
                                    }
                                    font.pixelSize: Math.round(13 * root.fs)
                                    Layout.preferredWidth: Math.round(260 * root.fs)
                                    elide: Text.ElideRight
                                }
                                HusText {
                                    text: modelData.required ? "★" : ""
                                    font.pixelSize: Math.round(13 * root.fs)
                                    color: "#b04b44"
                                    Layout.preferredWidth: 14
                                }
                                HusInput {
                                    Layout.fillWidth: true
                                    text: pathItem.pathValue
                                    placeholderText: "点击右侧浏览按钮选择文件..."
                                    onTextEdited: {
                                        pathItem.pathValue = text
                                        pathItem.fileValid = bridge.checkFileExists(text)
                                    }
                                }
                                HusButton {
                                    text: "浏览"
                                    type: HusButton.Type_Outlined
                                    onClicked: {
                                        root.currentEditIndex = index
                                        fileDialog.open()
                                    }
                                }
                                HusText {
                                    text: pathItem.fileValid ? "✓" : (pathItem.pathValue !== "" ? "✗" : "")
                                    font.pixelSize: Math.round(16 * root.fs)
                                    color: pathItem.fileValid ? "#3b7d3b" : "#b04b44"
                                    Layout.preferredWidth: 24
                                }
                            }
                        }
                    }
                }
            }

            // Script version / update check
            HusCard {
                Layout.fillWidth: true
                title: "脚本版本 / 更新"
                padding: 14

                contentItem: ColumnLayout {
                    spacing: 10
                    Layout.fillWidth: true

                    RowLayout {
                        spacing: 10
                        Layout.fillWidth: true

                        HusText {
                            text: "对照 GitHub 最新 Release 检测第三方脚本是否落后，建议有更新时先升级再跑日常。每 24 小时联网一次，也可手动刷新。"
                            font.pixelSize: Math.round(12 * root.fs)
                            color: "#8a7a68"
                            wrapMode: Text.Wrap
                            Layout.fillWidth: true
                        }
                        HusButton {
                            text: bridge.scriptCheckRunning ? "检查中..." : "检查更新"
                            type: HusButton.Type_Outlined
                            enabled: !bridge.scriptCheckRunning
                            onClicked: bridge.checkScriptUpdates()
                        }
                    }

                    HusText {
                        visible: root.scriptVersions.length === 0
                        text: bridge.scriptCheckRunning ? "正在检查..." : "未获取到脚本信息"
                        font.pixelSize: Math.round(13 * root.fs)
                        color: "#8a7a68"
                    }

                    Repeater {
                        model: root.scriptVersions

                        delegate: RowLayout {
                            Layout.fillWidth: true
                            spacing: 8

                            property var meta: root.statusMeta(modelData)

                            HusText {
                                text: {
                                    var disp = modelData.display_name || ""
                                    var label = modelData.label || ""
                                    if (!disp || label.indexOf(disp) >= 0) return label
                                    return disp + " · " + label
                                }
                                font.pixelSize: Math.round(13 * root.fs)
                                Layout.preferredWidth: Math.round(240 * root.fs)
                                elide: Text.ElideRight
                            }
                            HusText {
                                text: modelData.local ? ("本地 v" + modelData.local) : "本地版本未知"
                                font.pixelSize: Math.round(13 * root.fs)
                                color: modelData.local ? "#5a4d3f" : "#8a7a68"
                                Layout.preferredWidth: Math.round(150 * root.fs)
                                elide: Text.ElideRight
                            }
                            HusText {
                                text: modelData.latest ? ("最新 " + modelData.latest) : "—"
                                font.pixelSize: Math.round(13 * root.fs)
                                color: modelData.has_update ? "#b66f1f" : "#5a4d3f"
                                Layout.preferredWidth: Math.round(110 * root.fs)
                                elide: Text.ElideRight
                            }
                            HusTag {
                                text: modelData.stale ? (meta.text + "（缓存）") : meta.text
                                tagState: meta.state
                            }
                            Item { Layout.fillWidth: true }
                            // 首启可选自动下载：支持的未安装工具直接一键装（进度在"脚本管理"页）
                            HusButton {
                                visible: modelData.downloadable === true && (modelData.path || "") === ""
                                text: "一键安装"
                                type: HusButton.Type_Outlined
                                onClicked: {
                                    bridge.installTool(modelData.tool_key)
                                    installHint.visible = true
                                }
                            }
                            HusText {
                                id: installHint
                                visible: false
                                text: "安装任务已开始，进度见侧边栏「脚本管理」页"
                                font.pixelSize: Math.round(12 * root.fs)
                                color: "#8a7a68"
                            }
                            HusButton {
                                visible: (modelData.repo_url || "") !== ""
                                text: "发布页"
                                type: HusButton.Type_Outlined
                                onClicked: Qt.openUrlExternally(modelData.repo_url)
                            }
                        }
                    }
                }
            }

            // Action buttons
            HusCard {
                Layout.fillWidth: true
                padding: 14

                contentItem: RowLayout {
                    spacing: 12

                    HusButton {
                        text: "保存并继续"
                        type: HusButton.Type_Primary
                        onClicked: root.collectAndSave()
                    }
                    HusButton {
                        text: "跳过（稍后配置）"
                        type: HusButton.Type_Outlined
                        onClicked: bridge.skipSetup()
                    }
                }
            }
        }
    }
}
