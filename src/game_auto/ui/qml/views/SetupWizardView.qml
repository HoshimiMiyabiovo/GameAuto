import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import QtQuick.Dialogs
import HuskarUI.Basic

// First-run path setup wizard
Item {
    id: root
    readonly property real fs: Application.window ? Application.window.fontScale : 1

    property var setupItems: []
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
