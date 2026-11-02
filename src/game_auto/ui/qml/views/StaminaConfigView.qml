import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import HuskarUI.Basic

Item {
    id: root
    objectName: "staminaConfigRoot"

    property real fs: Math.max(0.8, Math.min(1.25, width / 1400))
    property var configs: []
    property bool loading: false
    property bool saving: false
    property string statusText: ""
    property int selectedIndex: 0
    // 页面只渲染当前游戏的一张卡：7 张卡同时构建是首帧卡顿的主因。
    property var visibleConfigs: {
        if (!configs || configs.length === 0) return []
        var idx = Math.max(0, Math.min(selectedIndex, configs.length - 1))
        return [configs[idx]]
    }
    // ListView 会回收滚出屏幕的卡片，未保存的编辑必须放在页面级草稿里，
    // 否则滚远再滚回来输入就没了。键 = gameKey + 分隔符 + fieldKey。
    property var fieldDrafts: ({})

    function draftKey(gameKey, fieldKey) {
        return String(gameKey || "") + "\u0001" + String(fieldKey || "")
    }

    function setFieldDraft(gameKey, fieldKey, valueText, boolValue, kind) {
        if (!gameKey || !fieldKey) return
        var drafts = fieldDrafts
        drafts[draftKey(gameKey, fieldKey)] = {
            valueText: String(valueText === undefined || valueText === null ? "" : valueText),
            boolValue: !!boolValue,
            kind: String(kind || ""),
            dirty: true
        }
        fieldDrafts = drafts
    }

    function fieldDraft(gameKey, fieldKey) {
        return fieldDrafts[draftKey(gameKey, fieldKey)] || null
    }

    function clearGameDrafts(gameKey) {
        var prefix = String(gameKey || "") + "\u0001"
        var drafts = fieldDrafts
        var next = {}
        for (var key in drafts) {
            if (key.indexOf(prefix) !== 0) next[key] = drafts[key]
        }
        fieldDrafts = next
    }

    function loadConfigs() {
        loading = true
        statusText = "读取中..."
        try {
            var oldKey = (configs.length > selectedIndex && configs[selectedIndex])
                         ? String(configs[selectedIndex].game_key || "") : ""
            var raw = bridge.getStaminaConfigs()
            var parsed = JSON.parse(raw || "[]")
            fieldDrafts = ({})
            configs = Array.isArray(parsed) ? parsed : [parsed]
            var nextIndex = 0
            if (oldKey) {
                for (var i = 0; i < configs.length; i++) {
                    if (String(configs[i].game_key || "") === oldKey) {
                        nextIndex = i
                        break
                    }
                }
            }
            selectedIndex = Math.max(0, Math.min(nextIndex, Math.max(0, configs.length - 1)))
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
                clearGameDrafts(card.gameKey)
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
                    font.pixelSize: Math.max(22, Math.round(20 * root.fs))
                    font.bold: true
                    color: "#2c241d"
                }
                HusText {
                    text: "支持原神 / 崩铁 / 绝区零 / 鸣潮 / 终末地 / 异环 / 暖暖。保存前自动备份，脚本或游戏运行中禁止写入；绝区零 OneDragon 仅支持单账户（固定 config/01）。脚本内改过配置请点「刷新」同步。"
                    font.pixelSize: Math.max(14, Math.round(13 * root.fs))
                    color: "#5f554b"
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
            font.pixelSize: Math.max(13, Math.round(12 * root.fs))
            color: root.statusText.indexOf("失败") >= 0 ? "#b04b44" : "#2f7d4a"
            wrapMode: Text.Wrap
            Layout.fillWidth: true
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 10
            HusText {
                text: "选择游戏"
                font.pixelSize: Math.max(14, Math.round(13 * root.fs))
                color: "#2c241d"
                Layout.alignment: Qt.AlignVCenter
            }
            Flow {
                Layout.fillWidth: true
                spacing: 8
                Repeater {
                    objectName: "staminaGameSelector"
                    model: root.configs
                    HusButton {
                        text: (modelData && modelData.display_name)
                              ? modelData.display_name : ("配置 " + (index + 1))
                        type: index === root.selectedIndex
                              ? HusButton.Type_Primary : HusButton.Type_Outlined
                        enabled: !root.saving
                        onClicked: root.selectedIndex = index
                    }
                }
            }
        }

        ListView {
            id: cardList
            objectName: "staminaCardList"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            spacing: 14
            cacheBuffer: 100
            // 只渲染当前游戏的一张卡；不复用 delegate，草稿已存到页面级。
            reuseItems: false
            model: root.visibleConfigs
            ScrollBar.vertical: ScrollBar {}

            delegate: HusCard {
                id: gameCard
                width: cardList.width
                property var gameData: modelData
                property bool locked: !!(modelData && modelData.running)
                property string gameKey: (modelData && modelData.game_key) || ""
                property int dirtyCount: 0
                property int visibilityVersion: 0
                padding: 14
                        title: (modelData && modelData.display_name) ? modelData.display_name : "未知脚本"

                        function refreshDirty() {
                            var n = 0
                            for (var i = 0; i < fieldModel.count; i++) {
                                if (fieldModel.get(i).dirty) n++
                            }
                            dirtyCount = n
                        }

                        function refreshVisibility() {
                            visibilityVersion++
                        }

                        ListModel {
                            id: fieldModel
                            objectName: "fieldModel_" + gameCard.gameKey
                        }

                        contentItem: ColumnLayout {
                            id: cardContent
                            spacing: 10
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
                                    var optLabels = f.option_labels || []
                                    var options = (f.options || []).map(function (value, idx) {
                                        return { value: value, label: optLabels[idx] || value }
                                    })
                                    fieldModel.append({
                                        fieldKey: f.key,
                                        label: f.label,
                                        kind: f.kind,
                                        help: f.help || "",
                                        optionsJson: JSON.stringify(options),
                                        dependsKey: (f.depends_on && f.depends_on.key) || "",
                                        dependsValuesJson: JSON.stringify((f.depends_on && f.depends_on.values) || []),
                                        allowEmpty: f.allow_empty !== false,
                                        valueText: (v === null || v === undefined) ? "" : String(v),
                                        boolValue: boolValue,
                                        readonly: !!f.readonly,
                                        dirty: false
                                    })
                                }
                                // 卡片被 ListView 回收后重建时，把页面级草稿灌回去
                                var drafts = root.fieldDrafts
                                for (var j = 0; j < fieldModel.count; j++) {
                                    var item = fieldModel.get(j)
                                    var draft = drafts[root.draftKey(gameCard.gameKey, item.fieldKey)]
                                    if (!draft) continue
                                    fieldModel.setProperty(j, "valueText", draft.valueText)
                                    fieldModel.setProperty(j, "boolValue", draft.boolValue)
                                    fieldModel.setProperty(j, "dirty", true)
                                }
                                gameCard.refreshDirty()
                                gameCard.refreshVisibility()
                            }

                            HusText {
                                visible: !!(gameCard.gameData && gameCard.gameData.error)
                                text: "读取失败: " + (gameCard.gameData ? gameCard.gameData.error : "")
                                color: "#b04b44"
                                font.pixelSize: Math.max(13, Math.round(12 * root.fs))
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }

                            HusText {
                                visible: gameCard.locked
                                text: "脚本/游戏正在运行，暂不允许修改。已经全部关闭的话，点右上角「刷新」重新检测；仍锁着说明有相关进程没退出。"
                                color: "#b66f1f"
                                font.pixelSize: Math.max(13, Math.round(12 * root.fs))
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }

                            HusText {
                                visible: !!(gameCard.gameData && gameCard.gameData.primary_file)
                                text: ((gameCard.gameData && gameCard.gameData.display_name)
                                       ? gameCard.gameData.display_name : "当前游戏")
                                      + " 配置: "
                                      + (gameCard.gameData ? gameCard.gameData.primary_file : "")
                                color: "#7a6d60"
                                font.pixelSize: Math.max(12, Math.round(11 * root.fs))
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }

                            HusText {
                                visible: gameCard.dirtyCount > 0
                                text: "有 " + gameCard.dirtyCount + " 项未保存（仅保存本页改过的字段）"
                                color: "#b66f1f"
                                font.pixelSize: Math.max(12, Math.round(11 * root.fs))
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }

                            GridLayout {
                                id: fieldsGrid
                                Layout.fillWidth: true
                                columns: root.width >= 1000 ? 2 : 1
                                columnSpacing: 22
                                rowSpacing: 14

                                Repeater {
                                    model: fieldModel

                                    delegate: ColumnLayout {
                                        id: fieldCell
                                        property int fieldIndex: index
                                        property var fieldOptions: JSON.parse(model.optionsJson || "[]")
                                        property string currentValue: (model.valueText === null || model.valueText === undefined)
                                                                      ? "" : String(model.valueText)
                                        property bool allowEmpty: model.allowEmpty !== false
                                        property string dependsKey: model.dependsKey || ""
                                        property var dependsValues: JSON.parse(model.dependsValuesJson || "[]")
                                        Layout.fillWidth: true
                                        Layout.minimumWidth: Math.round(280 * root.fs)
                                        spacing: 3
                                        visible: gameCard.visibilityVersion >= 0 && dependencyMet()

                                        function dependencyMet() {
                                            if (!dependsKey) return true
                                            for (var i = 0; i < fieldModel.count; i++) {
                                                var f = fieldModel.get(i)
                                                if (f.fieldKey === dependsKey) {
                                                    return dependsValues.indexOf(String(f.valueText)) >= 0
                                                }
                                            }
                                            return true
                                        }

                                        function optionLabelList() {
                                            var out = []
                                            for (var i = 0; i < fieldOptions.length; i++) {
                                                out.push(fieldOptions[i].label || fieldOptions[i].value)
                                            }
                                            return out
                                        }

                                        RowLayout {
                                            visible: model.kind === "bool"
                                            Layout.fillWidth: true
                                            spacing: 8

                                            ColumnLayout {
                                                Layout.fillWidth: true
                                                spacing: 1
                                                HusText {
                                                    text: model.label || ""
                                                    font.pixelSize: Math.max(14, Math.round(13 * root.fs))
                                                    color: "#2c241d"
                                                    wrapMode: Text.Wrap
                                                    Layout.fillWidth: true
                                                }
                                                HusText {
                                                    visible: !!model.help
                                                    text: model.help || ""
                                                    font.pixelSize: Math.max(12, Math.round(11 * root.fs))
                                                    color: "#6f6358"
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
                                                    var boolItem = fieldModel.get(fieldCell.fieldIndex)
                                                    root.setFieldDraft(gameCard.gameKey, boolItem.fieldKey, "", checked, "bool")
                                                    gameCard.refreshDirty()
                                                    gameCard.refreshVisibility()
                                                }
                                            }
                                        }

                                        ColumnLayout {
                                            visible: model.kind !== "bool"
                                            Layout.fillWidth: true
                                            spacing: 3

                                            HusText {
                                                text: model.label || ""
                                                font.pixelSize: Math.max(14, Math.round(13 * root.fs))
                                                color: "#2c241d"
                                                wrapMode: Text.Wrap
                                                Layout.fillWidth: true
                                            }
                                            HusText {
                                                visible: !!model.help
                                                text: model.help || ""
                                                font.pixelSize: Math.max(12, Math.round(11 * root.fs))
                                                color: "#6f6358"
                                                wrapMode: Text.Wrap
                                                Layout.fillWidth: true
                                            }

                                            HusText {
                                                visible: model.readonly === true
                                                text: model.valueText || "（无）"
                                                font.pixelSize: Math.max(13, Math.round(12 * root.fs))
                                                color: "#2c241d"
                                                wrapMode: Text.Wrap
                                                Layout.fillWidth: true
                                            }

                                            HusSelect {
                                                id: fieldSelect
                                                visible: !model.readonly && model.kind !== "list" && fieldCell.fieldOptions.length > 0
                                                Layout.fillWidth: true
                                                enabled: !gameCard.locked
                                                clearEnabled: false
                                                textRole: "label"
                                                model: ListModel { id: optionModel }
                                                property bool optionsReady: false
                                                property string sourceValue: fieldCell.currentValue

                                                function syncSelection() {
                                                    if (!optionsReady) return
                                                    var cur = String(sourceValue === null || sourceValue === undefined ? "" : sourceValue)
                                                    var idx = -1
                                                    for (var j = 0; j < optionModel.count; j++) {
                                                        if (String(optionModel.get(j).value) === cur) {
                                                            idx = j
                                                            break
                                                        }
                                                    }
                                                    if (idx < 0 && cur.length > 0) {
                                                        optionModel.append({ label: cur, value: cur })
                                                        idx = optionModel.count - 1
                                                    } else if (idx < 0 && fieldCell.allowEmpty === true) {
                                                        optionModel.append({ label: "(空/保持当前)", value: "" })
                                                        idx = optionModel.count - 1
                                                    }
                                                    currentIndex = idx >= 0 ? idx : (optionModel.count > 0 ? 0 : -1)
                                                }

                                                onSourceValueChanged: {
                                                    if (optionsReady) syncSelection()
                                                }

                                                Component.onCompleted: {
                                                    for (var i = 0; i < fieldCell.fieldOptions.length; i++) {
                                                        optionModel.append({
                                                            label: fieldCell.fieldOptions[i].label || fieldCell.fieldOptions[i].value,
                                                            value: fieldCell.fieldOptions[i].value
                                                        })
                                                    }
                                                    optionsReady = true
                                                    syncSelection()
                                                    // delegate 的 model 角色在部分时机比 onCompleted 晚就绪，
                                                    // 延迟再同步一次，避免回显落到第一项。
                                                    Qt.callLater(syncSelection)
                                                }
                                                onActivated: function (index) {
                                                    var item = optionModel.get(index)
                                                    var raw = (item.value === undefined ? item.label : item.value)
                                                    fieldModel.setProperty(fieldCell.fieldIndex, "valueText", raw)
                                                    fieldModel.setProperty(fieldCell.fieldIndex, "dirty", true)
                                                    var selectItem = fieldModel.get(fieldCell.fieldIndex)
                                                    root.setFieldDraft(gameCard.gameKey, selectItem.fieldKey, raw, false, selectItem.kind)
                                                    gameCard.refreshDirty()
                                                    gameCard.refreshVisibility()
                                                }
                                            }

                                            HusInput {
                                                visible: !model.readonly
                                                         && (model.kind === "list" || fieldCell.fieldOptions.length === 0)
                                                Layout.fillWidth: true
                                                enabled: !gameCard.locked
                                                placeholderText: model.kind === "int"
                                                                 ? "整数"
                                                                 : (model.kind === "list"
                                                                    ? (fieldCell.fieldOptions.length > 0
                                                                       ? "逗号分隔；可选: " + fieldCell.optionLabelList().join(" / ")
                                                                       : "逗号分隔")
                                                                    : "")
                                                Component.onCompleted: text = String(model.valueText === null || model.valueText === undefined ? "" : model.valueText)
                                                onTextEdited: {
                                                    fieldModel.setProperty(fieldCell.fieldIndex, "valueText", text)
                                                    fieldModel.setProperty(fieldCell.fieldIndex, "dirty", true)
                                                    var inputItem = fieldModel.get(fieldCell.fieldIndex)
                                                    root.setFieldDraft(gameCard.gameKey, inputItem.fieldKey, text, false, inputItem.kind)
                                                    gameCard.refreshDirty()
                                                    gameCard.refreshVisibility()
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
