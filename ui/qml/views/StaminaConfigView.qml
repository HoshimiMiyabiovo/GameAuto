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
        var changed = 0
        for (var i = 0; i < fieldModel.count; i++) {
            var it = fieldModel.get(i)
            if (!it.dirty) continue
            var v = it.boolValue
            if (it.kind !== "bool") v = it.valueText
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
            changed++
        }
        if (changed === 0) {
            statusText = "没有修改需要保存"
            return
        }
        saving = true
        var raw = bridge.saveStaminaConfig(card.gameKey, JSON.stringify(values))
        saving = false
        try {
            var res = JSON.parse(raw || "{}")
            if (res.ok) {
                for (var j = 0; j < fieldModel.count; j++) {
                    fieldModel.setProperty(j, "dirty", false)
                }
                card.refreshDirty()
                statusText = (card.title || "配置") + " 已保存 " + changed + " 项（旧文件已备份）"
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
                    text: "第一期：原神 / 鸣潮 / 终末地 / 异环 / 暖暖。只改白名单字段，保存前自动备份；脚本或游戏运行中禁止写入。脚本自己的 UI 改了配置后请点「刷新」同步；保存只提交本页改过的字段。"
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
                        property int dirtyCount: 0
                        Layout.fillWidth: true
                        padding: 14
                        title: (modelData && modelData.display_name) ? modelData.display_name : "未知脚本"

                        function refreshDirty() {
                            var n = 0
                            for (var i = 0; i < fieldModel.count; i++) {
                                if (fieldModel.get(i).dirty) n++
                            }
                            dirtyCount = n
                        }

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
                                    var isBool = f.kind === "bool"
                                    var boolValue = false
                                    if (isBool) {
                                        boolValue = (v === true || v === 1
                                                      || String(v).toLowerCase() === "true"
                                                      || String(v) === "1")
                                    }
                                    fieldModel.append({
                                        fieldKey: f.key,
                                        label: f.label,
                                        kind: f.kind,
                                        help: f.help || "",
                                        optionsJson: JSON.stringify(f.options || []),
                                        allowEmpty: f.allow_empty !== false,
                                        valueText: (v === null || v === undefined) ? "" : String(v),
                                        boolValue: boolValue,
                                        dirty: false
                                    })
                                }
                                gameCard.refreshDirty()
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

                            HusText {
                                visible: gameCard.dirtyCount > 0
                                text: "有 " + gameCard.dirtyCount + " 项未保存（仅保存本页改过的字段）"
                                color: "#b66f1f"
                                font.pixelSize: Math.round(11 * root.fs)
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }

                            HusText {
                                visible: !!(gameCard.gameData && gameCard.gameData.notes
                                            && gameCard.gameData.notes.length > 0)
                                text: (gameCard.gameData && gameCard.gameData.notes)
                                      ? gameCard.gameData.notes.join(" ｜ ") : ""
                                color: "#b66f1f"
                                font.pixelSize: Math.round(11 * root.fs)
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }

                            GridLayout {
                                id: fieldsGrid
                                Layout.fillWidth: true
                                columns: root.width >= 1000 ? 2 : 1
                                columnSpacing: 18
                                rowSpacing: 10

                                Repeater {
                                    model: fieldModel

                                    delegate: ColumnLayout {
                                        id: fieldCell
                                        property int fieldIndex: index
                                        property var fieldOptions: JSON.parse(model.optionsJson || "[]")
                                        Layout.fillWidth: true
                                        Layout.minimumWidth: Math.round(280 * root.fs)
                                        spacing: 3

                                        RowLayout {
                                            visible: model.kind === "bool"
                                            Layout.fillWidth: true
                                            spacing: 8

                                            ColumnLayout {
                                                Layout.fillWidth: true
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
                                                checked: model.boolValue === true
                                                enabled: !gameCard.locked
                                                onClicked: {
                                                    fieldModel.setProperty(fieldCell.fieldIndex, "boolValue", checked)
                                                    fieldModel.setProperty(fieldCell.fieldIndex, "dirty", true)
                                                    gameCard.refreshDirty()
                                                }
                                            }
                                        }

                                        ColumnLayout {
                                            visible: model.kind !== "bool"
                                            Layout.fillWidth: true
                                            spacing: 3

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

                                            HusSelect {
                                                id: fieldSelect
                                                visible: model.kind !== "list" && fieldCell.fieldOptions.length > 0
                                                Layout.fillWidth: true
                                                enabled: !gameCard.locked
                                                clearEnabled: false
                                                textRole: "label"
                                                model: ListModel { id: optionModel }
                                                Component.onCompleted: {
                                                    for (var i = 0; i < fieldCell.fieldOptions.length; i++) {
                                                        optionModel.append({ label: fieldCell.fieldOptions[i] })
                                                    }
                                                    var cur = String(model.valueText === null || model.valueText === undefined ? "" : model.valueText)
                                                    var idx = -1
                                                    for (var j = 0; j < optionModel.count; j++) {
                                                        if (optionModel.get(j).label === cur) {
                                                            idx = j
                                                            break
                                                        }
                                                    }
                                                    if (idx < 0 && cur.length > 0) {
                                                        optionModel.append({ label: cur })
                                                        idx = optionModel.count - 1
                                                    } else if (idx < 0 && model.allowEmpty === true) {
                                                        optionModel.append({ label: "(空/保持当前)" })
                                                        idx = optionModel.count - 1
                                                    }
                                                    currentIndex = idx >= 0 ? idx : (optionModel.count > 0 ? 0 : -1)
                                                }
                                                onActivated: function (index) {
                                                    var label = optionModel.get(index).label
                                                    fieldModel.setProperty(fieldCell.fieldIndex, "valueText",
                                                                           label === "(空/保持当前)" ? "" : label)
                                                    fieldModel.setProperty(fieldCell.fieldIndex, "dirty", true)
                                                    gameCard.refreshDirty()
                                                }
                                            }

                                            HusInput {
                                                visible: model.kind === "list" || fieldCell.fieldOptions.length === 0
                                                Layout.fillWidth: true
                                                enabled: !gameCard.locked
                                                placeholderText: model.kind === "int"
                                                                 ? "整数"
                                                                 : (model.kind === "list"
                                                                    ? (fieldCell.fieldOptions.length > 0
                                                                       ? "逗号分隔；可选: " + fieldCell.fieldOptions.join(" / ")
                                                                       : "逗号分隔")
                                                                    : "")
                                                Component.onCompleted: text = String(model.valueText === null || model.valueText === undefined ? "" : model.valueText)
                                                onTextEdited: {
                                                    fieldModel.setProperty(fieldCell.fieldIndex, "valueText", text)
                                                    fieldModel.setProperty(fieldCell.fieldIndex, "dirty", true)
                                                    gameCard.refreshDirty()
                                                }
                                            }
                                        }
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
                                    text: root.saving
                                          ? "保存中..."
                                          : (gameCard.dirtyCount > 0 ? "保存 (" + gameCard.dirtyCount + ")" : "保存")
                                    type: HusButton.Type_Primary
                                    enabled: !gameCard.locked && !root.saving
                                             && !(gameCard.gameData && gameCard.gameData.error)
                                             && gameCard.dirtyCount > 0
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
