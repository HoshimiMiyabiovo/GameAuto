import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import HuskarUI.Basic

Item {
    id: root

    property real fs: Math.max(0.8, Math.min(1.25, width / 1400))
    property var configs: []
    property bool loading: false
    property bool saving: false
    property string statusText: ""

    function loadConfigs() {
        loading = true
        statusText = "读取中..."
        try {
            var raw = bridge.getStaminaConfigs()
            var parsed = JSON.parse(raw || "[]")
            configs = Array.isArray(parsed) ? parsed : [parsed]
            statusText = "已读取 " + configs.length + " 个脚本配置"
        } catch (e) {
            configs = []
            statusText = "读取失败: " + e
        }
        loading = false
    }

    function saveGame(card, fieldModel) {
        if (!card || card.locked || saving) return
        var values = {}
        for (var i = 0; i < fieldModel.count; i++) {
            var it = fieldModel.get(i)
            var v = it.value
            if (it.kind === "bool") {
                v = !!v
            } else if (it.kind === "int") {
                var n = Number(v)
                if (!isFinite(n) || Math.floor(n) !== n) {
                    statusText = it.label + " 需要整数"
                    return
                }
                v = n
            } else if (it.kind === "list") {
                v = String(v || "").split(/[,，;；\n|]/).map(function (x) {
                    return String(x).trim()
                }).filter(function (x) {
                    return x.length > 0
                })
            } else {
                v = String(v || "")
            }
            values[it.fieldKey] = v
        }
        saving = true
        var raw = bridge.saveStaminaConfig(card.gameKey, JSON.stringify(values))
        saving = false
        try {
            var res = JSON.parse(raw || "{}")
            if (res.ok) {
                statusText = (card.title || "配置") + " 已保存（旧文件已备份）"
            } else {
                statusText = res.error || "保存失败"
            }
        } catch (e) {
            statusText = "保存返回解析失败: " + e
        }
    }

    Component.onCompleted: loadConfigs()

    ColumnLayout {
        anchors.fill: parent
        spacing: 12

        RowLayout {
            Layout.fillWidth: true
            spacing: 10

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2
                HusText {
                    text: "脚本刷本配置"
                    font.pixelSize: Math.round(20 * root.fs)
                    font.bold: true
                    color: "#2c241d"
                }
                HusText {
                    text: "第一期：原神 / 鸣潮 / 终末地 / 异环 / 暖暖。只改白名单字段，保存前自动备份；脚本或游戏运行中禁止写入。"
                    font.pixelSize: Math.round(12 * root.fs)
                    color: "#6d6257"
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
            }

            HusButton {
                text: root.loading ? "读取中..." : "刷新"
                type: HusButton.Type_Outlined
                enabled: !root.loading && !root.saving
                onClicked: root.loadConfigs()
            }
        }

        HusText {
            visible: !!root.statusText
            text: root.statusText
            font.pixelSize: Math.round(12 * root.fs)
            color: root.statusText.indexOf("失败") >= 0 ? "#b04b44" : "#2f7d4a"
            wrapMode: Text.Wrap
            Layout.fillWidth: true
        }

        Flickable {
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentWidth: width
            contentHeight: contentColumn.implicitHeight
            clip: true
            ScrollBar.vertical: ScrollBar {}

            ColumnLayout {
                id: contentColumn
                width: parent.width
                spacing: 12

                Repeater {
                    model: root.configs

                    delegate: HusCard {
                        id: gameCard
                        property var gameData: modelData
                        property bool locked: !!(modelData && modelData.running)
                        property string gameKey: (modelData && modelData.game_key) || ""
                        Layout.fillWidth: true
                        padding: 14
                        title: (modelData && modelData.display_name) ? modelData.display_name : "未知脚本"

                        ListModel { id: fieldModel }

                        contentItem: ColumnLayout {
                            id: cardContent
                            spacing: 8
                            Layout.fillWidth: true

                            Component.onCompleted: {
                                var fs = (gameCard.gameData && gameCard.gameData.fields) || []
                                var vals = (gameCard.gameData && gameCard.gameData.values) || {}
                                for (var i = 0; i < fs.length; i++) {
                                    var f = fs[i]
                                    var v = vals[f.key]
                                    if (f.kind === "list" && Array.isArray(v)) {
                                        v = v.join(", ")
                                    }
                                    fieldModel.append({
                                        fieldKey: f.key,
                                        label: f.label,
                                        kind: f.kind,
                                        help: f.help || "",
                                        value: (v === null || v === undefined) ? "" : v
                                    })
                                }
                            }

                            HusText {
                                visible: !!(gameCard.gameData && gameCard.gameData.error)
                                text: "读取失败: " + (gameCard.gameData ? gameCard.gameData.error : "")
                                color: "#b04b44"
                                font.pixelSize: Math.round(12 * root.fs)
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }

                            HusText {
                                visible: gameCard.locked
                                text: "脚本/游戏正在运行，暂不允许修改；先结束运行再保存。"
                                color: "#b66f1f"
                                font.pixelSize: Math.round(12 * root.fs)
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }

                            HusText {
                                visible: !!(gameCard.gameData && gameCard.gameData.primary_file)
                                text: "配置: " + (gameCard.gameData ? gameCard.gameData.primary_file : "")
                                color: "#8a7d70"
                                font.pixelSize: Math.round(11 * root.fs)
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }

                            Repeater {
                                model: fieldModel

                                delegate: RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 10

                                    ColumnLayout {
                                        Layout.preferredWidth: Math.round(250 * root.fs)
                                        spacing: 1
                                        HusText {
                                            text: model.label || ""
                                            font.pixelSize: Math.round(13 * root.fs)
                                            color: "#2c241d"
                                            wrapMode: Text.Wrap
                                            Layout.fillWidth: true
                                        }
                                        HusText {
                                            visible: !!model.help
                                            text: model.help || ""
                                            font.pixelSize: Math.round(10 * root.fs)
                                            color: "#8a7d70"
                                            wrapMode: Text.Wrap
                                            Layout.fillWidth: true
                                        }
                                    }

                                    HusCheckBox {
                                        visible: model.kind === "bool"
                                        checked: model.value === true
                                        enabled: !gameCard.locked
                                        onClicked: fieldModel.setProperty(index, "value", checked)
                                    }

                                    HusInput {
                                        visible: model.kind !== "bool"
                                        Layout.fillWidth: true
                                        enabled: !gameCard.locked
                                        placeholderText: model.kind === "int" ? "整数" : (model.kind === "list" ? "逗号分隔" : "")
                                        Component.onCompleted: text = String(model.value === null || model.value === undefined ? "" : model.value)
                                        onTextChanged: fieldModel.setProperty(index, "value", text)
                                    }
                                }
                            }

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 8
                                Item { Layout.fillWidth: true }
                                HusButton {
                                    text: "打开配置文件"
                                    type: HusButton.Type_Outlined
                                    enabled: !!(gameCard.gameData && gameCard.gameData.primary_file)
                                    onClicked: bridge.openStaminaConfigFile(gameCard.gameKey)
                                }
                                HusButton {
                                    text: root.saving ? "保存中..." : "保存"
                                    type: HusButton.Type_Primary
                                    enabled: !gameCard.locked && !root.saving && !(gameCard.gameData && gameCard.gameData.error)
                                    onClicked: root.saveGame(gameCard, fieldModel)
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
