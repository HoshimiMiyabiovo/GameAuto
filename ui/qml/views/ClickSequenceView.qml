import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import QtQuick.Dialogs
import HuskarUI.Basic
import "../components"

// 固定点击序列：录制 / 回放 / 步骤时间轴
Item {
    id: root
    readonly property real fs: Application.window ? Application.window.fontScale : 1

    property var sequences: []
    property var detail: ({})
    property var events: []
    property string selectedName: ""
    property int selectedStep: -1
    property string lastClick: ""
    property int loopCount: 1

    // 录制窗口行为偏好（从 app_config.json 的 click_seq_record 段读取）
    property bool prefMinimizeSelf: true
    property bool prefPinTarget: false

    // 目标窗口：下拉里选中的那一个（hwnd 为 0 表示不限窗口）
    property int selHwnd: 0
    property string selTitle: ""
    property string selDesc: ""

    ListModel { id: winModel }

    Component.onCompleted: {
        bridge.refreshClickSequences()
        bridge.refreshWindowList()
        try {
            var prefs = JSON.parse(bridge.getRecordWindowPrefs() || "{}")
            root.prefMinimizeSelf = prefs.minimize_self !== false
            root.prefPinTarget = prefs.pin_target === true
        } catch (e) {
            root.prefMinimizeSelf = true
            root.prefPinTarget = false
        }
    }

    property double _lastRefreshTs: 0
    onVisibleChanged: {
        if (visible && Date.now() - _lastRefreshTs > 30000) {
            _lastRefreshTs = Date.now()
            bridge.refreshClickSequences()
            bridge.refreshWindowList()
        }
    }

    // 重建窗口下拉；刷新后尽量保住原来的选择（先按句柄、再按标题）
    function reloadWindows(jsonStr) {
        var keepHwnd = root.selHwnd
        var keepTitle = root.selTitle
        winModel.clear()
        winModel.append({
            "label": "无 — 记录全屏所有点击",
            "hwnd": 0, "title": "", "desc": "不限制窗口，按屏幕绝对坐标记录"
        })
        var arr = []
        try { arr = JSON.parse(jsonStr || "[]") } catch (e) { arr = [] }
        for (var i = 0; i < arr.length; i++) {
            var w = arr[i]
            var lab = w.title + "  [" + w.width + "×" + w.height + "]"
            if (w.exe) lab += " · " + w.exe
            if (w.minimized) lab += " · 最小化"
            if (w.foreground) lab = "▶ " + lab
            var d = "位置 (" + w.rect[0] + ", " + w.rect[1] + ") · "
                    + w.width + "×" + w.height
                    + (w.exe ? (" · " + w.exe) : "")
                    + (w.minimized ? " · 已最小化" : "")
            winModel.append({
                "label": lab, "hwnd": w.hwnd, "title": w.title, "desc": d
            })
        }
        var idx = 0
        for (var j = 1; j < winModel.count; j++) {
            var it = winModel.get(j)
            if (keepHwnd && it.hwnd === keepHwnd) { idx = j; break }
            if (!keepHwnd && keepTitle !== "" && it.title === keepTitle) { idx = j; break }
        }
        winSelect.currentIndex = idx
        root.applyWindow(idx)
    }

    function applyWindow(idx) {
        var it = winModel.get(idx)
        if (!it) return
        root.selHwnd = Number(it.hwnd || 0)
        root.selTitle = it.title || ""
        root.selDesc = it.desc || ""
    }

    function windowStatusText() {
        if (root.selHwnd === 0) return "不限窗口：记录屏幕上的所有点击（绝对坐标）"
        return "已选：" + root.selTitle + (root.selDesc ? ("\n" + root.selDesc) : "")
               + "\n只录该窗口内的点击，并记录窗口相对坐标（窗口移动也能回放）"
    }

    // 受保护缺失与删除确认状态
    property bool hasMissingProtected: false
    property string pendingDeleteName: ""
    property bool deleteConfirmOpen: false

    Connections {
        target: bridge
        function onWindowListChanged(jsonStr) {
            root.reloadWindows(jsonStr)
        }
        function onClickSeqListChanged(jsonStr) {
            try { root.sequences = JSON.parse(jsonStr || "[]") } catch (e) { root.sequences = [] }
            var miss = false
            for (var i = 0; i < root.sequences.length; i++) {
                if (root.sequences[i].missing === true) { miss = true; break }
            }
            root.hasMissingProtected = miss
        }
        function onClickSeqDetailChanged(jsonStr) {
            try { root.detail = JSON.parse(jsonStr || "{}") } catch (e) { root.detail = ({}) }
            root.events = root.detail.events || []
            root.selectedName = root.detail.name || ""
            root.selectedStep = -1
        }
        function onClickSeqLastChanged(jsonStr) {
            root.lastClick = jsonStr || ""
        }
    }

    // 连线宽度 ∝ 等待时长（1s ≈ 6px，夹在 [14, 120]）
    function gapWidth(gap) {
        return Math.max(14, Math.min(120, Number(gap || 0) * 6))
    }

    function relText(rx, ry) {
        return (rx === null || rx === undefined) ? "无" : ("(" + rx + ", " + ry + ")")
    }

    function btnText(b) {
        if (b === "right") return "右键"
        if (b === "key") return "键盘"
        if (b === "mark") return "标记"
        return "左键"
    }

    function lastClickText() {
        if (!root.lastClick || root.lastClick === "") return "尚无记录"
        try {
            var o = JSON.parse(root.lastClick)
            var gapText = (o.index === 1) ? "首步" : ("距上步 " + Number(o.gap || 0).toFixed(1) + "s")
            return "第 " + o.index + " 步 · 绝对 (" + o.x + ", " + o.y + ")\n"
                   + "相对 " + root.relText(o.rel_x, o.rel_y) + " · "
                   + root.btnText(o.button) + " · " + gapText
        } catch (e) {
            return "尚无记录"
        }
    }

    function stepDetailText() {
        if (root.selectedStep < 0 || root.selectedStep >= root.events.length)
            return "点击上方节点查看步骤详情"
        var e = root.events[root.selectedStep]
        return "第 " + e.index + " 步 · 绝对 (" + e.x + ", " + e.y + ") · 相对 "
               + root.relText(e.rel_x, e.rel_y) + " · " + root.btnText(e.button)
               + " · 距上步 " + Number(e.gap_before || 0).toFixed(1) + "s"
               + (e.ts ? (" · " + e.ts) : "")
    }

    ScrollView {
        anchors.fill: parent
        clip: true

        RowLayout {
            width: parent.width
            spacing: 14

            // ══════════ 左：录制控制 ══════════
            ColumnLayout {
                Layout.preferredWidth: 320
                Layout.alignment: Qt.AlignTop
                spacing: 12

                HusCard {
                    Layout.fillWidth: true
                    title: "录制控制"
                    padding: 16

                    contentItem: ColumnLayout {
                        spacing: 10

                        HusText {
                            text: "序列名称"
                            font.pixelSize: Math.round(12 * root.fs)
                            color: "#6d6257"
                        }
                        HusInput {
                            id: seqNameInput
                            Layout.fillWidth: true
                            placeholderText: "例如：日常签到-甲板"
                        }
                        // 受保护序列提示：输入的是一条龙内置名时给出覆盖警告
                        HusText {
                            visible: ["白荆回廊-日常","白荆回廊-验证","白荆回廊-每周商店购买"].indexOf(seqNameInput.text.trim()) !== -1
                            text: "⚠ 该名称为一条龙内置序列，录制完成将覆盖原文件；删除已被防误删保护"
                            font.pixelSize: Math.round(11 * root.fs)
                            color: "#b66f1f"
                            wrapMode: Text.Wrap
                            Layout.fillWidth: true
                        }

                        HusText {
                            text: "目标窗口"
                            font.pixelSize: Math.round(12 * root.fs)
                            color: "#6d6257"
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 6

                            HusSelect {
                                id: winSelect
                                Layout.fillWidth: true
                                clearEnabled: false
                                model: winModel
                                currentIndex: 0
                                enabled: !bridge.clickSeqRecording
                                // 选中只记录目标，不动窗口焦点；点「开始录制」才自动把目标窗口置前（默认并最小化本窗口让出空间）
                                onActivated: {
                                    root.applyWindow(currentIndex)
                                }
                            }
                            HusButton {
                                text: "刷新"
                                Layout.preferredWidth: 58
                                enabled: !bridge.clickSeqRecording
                                onClicked: bridge.refreshWindowList()
                            }
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: root.selHwnd === 0 ? 26 : 62
                            radius: 6
                            color: root.selHwnd === 0 ? "#f7f2ea" : "#eef4fb"
                            border.color: "#dbcdbd"
                            border.width: 1

                            HusText {
                                anchors.fill: parent
                                anchors.margins: 8
                                text: root.windowStatusText()
                                font.pixelSize: Math.round(12 * root.fs)
                                color: root.selHwnd === 0 ? "#6d6257" : "#295d86"
                                verticalAlignment: Text.AlignVCenter
                                wrapMode: Text.Wrap
                            }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 6
                            visible: root.selHwnd !== 0

                            HusButton {
                                text: "把该窗口置前"
                                Layout.fillWidth: true
                                enabled: !bridge.clickSeqRecording
                                onClicked: bridge.focusWindow(root.selHwnd)
                            }
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 4

                            HusCheckBox {
                                text: "录制时最小化本窗口"
                                checked: root.prefMinimizeSelf
                                enabled: !bridge.clickSeqRecording
                                onClicked: {
                                    root.prefMinimizeSelf = checked
                                    bridge.saveRecordWindowPref("minimize_self", checked)
                                }
                            }
                            HusCheckBox {
                                text: "录制时置顶目标窗口（会挡住其他窗口）"
                                checked: root.prefPinTarget
                                enabled: !bridge.clickSeqRecording
                                onClicked: {
                                    root.prefPinTarget = checked
                                    bridge.saveRecordWindowPref("pin_target", checked)
                                }
                            }
                        }

                        HusButton {
                            Layout.fillWidth: true
                            text: bridge.clickSeqRecording ? "停止录制 (小键盘0)" : "开始录制"
                            type: bridge.clickSeqRecording ? HusButton.Type_Outlined : HusButton.Type_Primary
                            enabled: !bridge.clickSeqPlaying
                            onClicked: {
                                if (bridge.clickSeqRecording)
                                    bridge.stopClickRecording()
                                else
                                    bridge.startClickRecording(
                                        seqNameInput.text, root.selTitle, root.selHwnd)
                            }
                        }

                        RowLayout {
                            spacing: 8
                            Rectangle {
                                width: 9
                                height: 9
                                radius: 5
                                color: bridge.clickSeqRecording ? "#b04b44" : "#c9bfb2"
                            }
                            HusText {
                                text: bridge.clickSeqRecording ? "录制中" : "空闲"
                                font.pixelSize: Math.round(13 * root.fs)
                                color: bridge.clickSeqRecording ? "#b04b44" : "#6d6257"
                            }
                            HusText {
                                Layout.alignment: Qt.AlignRight
                                text: "已记录 " + bridge.clickSeqCount + " 次"
                                font.pixelSize: Math.round(13 * root.fs)
                                color: "#2c241d"
                            }
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            height: 1
                            color: "#dbcdbd"
                        }

                        HusText {
                            text: "最近一次点击"
                            font.pixelSize: Math.round(12 * root.fs)
                            color: "#6d6257"
                        }
                        HusText {
                            Layout.fillWidth: true
                            text: root.lastClickText()
                            font.pixelSize: Math.round(13 * root.fs)
                            color: root.lastClick === "" ? "#999999" : "#2c241d"
                            wrapMode: Text.Wrap
                            lineHeight: 1.4
                        }

                        ColumnLayout {
                            spacing: 2
                            HusText {
                                text: "热键：小键盘0 停止 · 小键盘+ 打标记"
                                font.pixelSize: Math.round(12 * root.fs)
                                color: "#6d6257"
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }
                            HusText {
                                text: "鼠标甩至屏幕角落可紧急停止回放；最小化后仍可用小键盘0 恢复窗口"
                                font.pixelSize: Math.round(11 * root.fs)
                                color: "#8a7a68"
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }
                        }
                    }
                }
            }

            // ══════════ 右：序列列表 + 时间轴 ══════════
            ColumnLayout {
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignTop
                spacing: 12

                HusCard {
                    Layout.fillWidth: true
                    title: "序列列表"
                    padding: 16

                    contentItem: Column {
                        spacing: 8
                        width: parent.width

                        // 受保护缺失提示 + 恢复按钮
                        Rectangle {
                            visible: root.hasMissingProtected
                            width: parent.width
                            height: visible ? 42 : 0
                            radius: 8
                            color: "#fef3e2"
                            border.color: "#e6a23c"
                            border.width: 1

                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: 8
                                spacing: 8
                                HusText {
                                    text: "⚠ 检测到内置一条龙序列缺失"
                                    font.pixelSize: Math.round(12 * root.fs)
                                    color: "#b66f1f"
                                    Layout.fillWidth: true
                                }
                                HusButton {
                                    text: "恢复内置序列"
                                    type: HusButton.Type_Primary
                                    onClicked: bridge.restoreProtectedSequences()
                                }
                            }
                        }

                        Column {
                            width: parent.width
                            spacing: 6
                            Repeater {
                                model: root.sequences
                                delegate: Rectangle {
                                    width: parent.width
                                    height: modelData.protected ? 68 : 58
                                    radius: 8
                                    required property var modelData
                                    required property int index
                                    color: modelData.protected ? "#f0f4ff" : (root.selectedName === modelData.name ? "#eef8f1" : "#fffdf9")
                                    border.color: modelData.protected ? "#5b8fd4" : (root.selectedName === modelData.name ? "#2f7d4a" : "#dbcdbd")
                                    border.width: modelData.protected ? 1.5 : 1

                                    RowLayout {
                                        anchors.fill: parent
                                        anchors.margins: 10
                                        spacing: 8

                                        Item {
                                            Layout.fillWidth: true
                                            Layout.fillHeight: true

                                            ColumnLayout {
                                                anchors.fill: parent
                                                spacing: 2
                                                RowLayout {
                                                    spacing: 6
                                                    HusText {
                                                        text: modelData.name || "未命名"
                                                        font.pixelSize: Math.round(14 * root.fs)
                                                        font.bold: true
                                                        color: "#2c241d"
                                                        elide: Text.ElideRight
                                                        Layout.fillWidth: true
                                                    }
                                                    HusTag {
                                                        visible: modelData.protected === true
                                                        text: "🔒 一条龙"
                                                        tagState: HusTag.State_Info
                                                    }
                                                }
                                                HusText {
                                                    text: (modelData.steps || 0) + " 步 · "
                                                          + (modelData.created_at || "无时间")
                                                          + (modelData.window ? (" · 窗口 " + modelData.window) : "")
                                                          + (modelData.protected ? " · 防误删" : "")
                                                    font.pixelSize: Math.round(12 * root.fs)
                                                    color: "#6d6257"
                                                    elide: Text.ElideRight
                                                    Layout.fillWidth: true
                                                }
                                            }

                                            MouseArea {
                                                anchors.fill: parent
                                                cursorShape: Qt.PointingHandCursor
                                                onClicked: bridge.loadClickSequenceDetail(modelData.name)
                                            }
                                        }

                                        HusButton {
                                            text: "回放"
                                            enabled: !bridge.clickSeqPlaying && !bridge.clickSeqRecording
                                            onClicked: bridge.playClickSequence(modelData.name, root.loopCount, false)
                                        }
                                        HusButton {
                                            text: "试跑"
                                            enabled: !bridge.clickSeqPlaying && !bridge.clickSeqRecording
                                            onClicked: bridge.playClickSequence(modelData.name, 1, true)
                                        }
                                        HusButton {
                                            text: modelData.protected ? "受保护" : "删除"
                                            enabled: !bridge.clickSeqRecording && !modelData.protected
                                            type: modelData.protected ? HusButton.Type_Outlined : HusButton.Type_Default
                                            onClicked: {
                                                if (modelData.protected) return
                                                root.pendingDeleteName = modelData.name
                                                root.deleteConfirmOpen = true
                                            }
                                        }
                                    }
                                }
                            }
                        }

                        HusEmpty {
                            visible: root.sequences.length === 0
                            description: "暂无序列，先在左侧录制一个"
                        }
                    }
                }

                HusCard {
                    Layout.fillWidth: true
                    title: root.selectedName ? ("步骤时间轴 · " + root.selectedName) : "步骤时间轴"
                    padding: 16
                    visible: root.events.length > 0

                    contentItem: ColumnLayout {
                        spacing: 10

                        HusText {
                            text: "连线长度 ∝ 等待时长；长连线 = 加载 / 过场"
                            font.pixelSize: Math.round(12 * root.fs)
                            color: "#6d6257"
                        }

                        RowLayout {
                            spacing: 8
                            HusText {
                                text: "循环"
                                font.pixelSize: Math.round(13 * root.fs)
                                color: "#2c241d"
                            }
                            Repeater {
                                model: [1, 3, 5]
                                delegate: HusButton {
                                    required property var modelData
                                    text: String(modelData)
                                    Layout.preferredWidth: 48
                                    type: root.loopCount === modelData ? HusButton.Type_Primary : HusButton.Type_Outlined
                                    onClicked: root.loopCount = modelData
                                }
                            }
                            HusButton {
                                text: "回放"
                                type: HusButton.Type_Primary
                                enabled: !bridge.clickSeqPlaying && !bridge.clickSeqRecording
                                onClicked: bridge.playClickSequence(root.selectedName, root.loopCount, false)
                            }
                            HusButton {
                                text: "试跑（不真点）"
                                enabled: !bridge.clickSeqPlaying && !bridge.clickSeqRecording
                                onClicked: bridge.playClickSequence(root.selectedName, 1, true)
                            }
                            HusButton {
                                text: "停止回放"
                                enabled: bridge.clickSeqPlaying
                                onClicked: bridge.stopClickPlayback()
                            }
                        }

                        ScrollView {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 60
                            clip: true

                            Row {
                                spacing: 0
                                topPadding: 6

                                Repeater {
                                    model: root.events

                                    delegate: Row {
                                        spacing: 0
                                        required property var modelData
                                        required property int index

                                        // 连接线：宽度 = 上一步的等待时长
                                        Item {
                                            width: index > 0 ? root.gapWidth(root.events[index - 1].gap_after) : 0
                                            height: 46
                                            visible: index > 0

                                            HusText {
                                                anchors.top: parent.top
                                                anchors.topMargin: 2
                                                anchors.horizontalCenter: parent.horizontalCenter
                                                text: index > 0
                                                      ? (Number(root.events[index - 1].gap_after || 0).toFixed(1) + "s")
                                                      : ""
                                                font.pixelSize: Math.round(11 * root.fs)
                                                color: (index > 0 && Number(root.events[index - 1].gap_after || 0) >= 2)
                                                       ? "#b66f1f" : "#6d6257"
                                            }
                                            Rectangle {
                                                anchors.bottom: parent.bottom
                                                anchors.bottomMargin: 16
                                                width: parent.width
                                                height: 1
                                                color: (index > 0 && Number(root.events[index - 1].gap_after || 0) >= 2)
                                                       ? "#b66f1f" : "#dbcdbd"
                                            }
                                        }

                                        // 节点
                                        Item {
                                            width: 34
                                            height: 46

                                            Rectangle {
                                                width: 30
                                                height: 30
                                                radius: 15
                                                anchors.centerIn: parent
                                                color: root.selectedStep === index ? "#2f7d4a" : "#fffaf2"
                                                border.color: Number(modelData.gap_after || 0) >= 2 ? "#b66f1f" : "#2f7d4a"
                                                border.width: 1

                                                HusText {
                                                    anchors.centerIn: parent
                                                    text: String(index + 1)
                                                    font.pixelSize: Math.round(13 * root.fs)
                                                    color: root.selectedStep === index ? "#fffaf2" : "#2f7d4a"
                                                }
                                            }

                                            MouseArea {
                                                anchors.fill: parent
                                                cursorShape: Qt.PointingHandCursor
                                                onClicked: root.selectedStep = index
                                            }
                                        }
                                    }
                                }
                            }
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 56
                            radius: 8
                            color: "#f4ecdf"
                            border.color: "#dbcdbd"
                            border.width: 1

                            HusText {
                                anchors.fill: parent
                                anchors.margins: 10
                                text: root.stepDetailText()
                                font.pixelSize: Math.round(13 * root.fs)
                                color: root.selectedStep < 0 ? "#6d6257" : "#2c241d"
                                verticalAlignment: Text.AlignVCenter
                                wrapMode: Text.Wrap
                            }
                        }

                        ListView {
                            id: stepList
                            Layout.fillWidth: true
                            Layout.preferredHeight: Math.min(root.events.length, 8) * 30 + 8
                            clip: true
                            spacing: 2
                            model: root.events

                            delegate: Rectangle {
                                width: stepList.width
                                height: 28
                                radius: 6
                                required property var modelData
                                required property int index
                                color: root.selectedStep === index ? "#eef8f1" : "#fffdf9"
                                border.color: "#e0d6c8"
                                border.width: 1

                                RowLayout {
                                    anchors.fill: parent
                                    anchors.leftMargin: 10
                                    anchors.rightMargin: 10
                                    spacing: 8

                                    HusText {
                                        text: "#" + (index + 1)
                                        Layout.preferredWidth: 34
                                        font.pixelSize: Math.round(12 * root.fs)
                                        color: "#6d6257"
                                    }
                                    HusText {
                                        text: "(" + modelData.x + ", " + modelData.y + ")"
                                        Layout.preferredWidth: 100
                                        font.pixelSize: Math.round(12 * root.fs)
                                        color: "#2c241d"
                                    }
                                    HusText {
                                        text: "相对 " + root.relText(modelData.rel_x, modelData.rel_y)
                                        Layout.preferredWidth: 110
                                        font.pixelSize: Math.round(12 * root.fs)
                                        color: "#6d6257"
                                    }
                                    HusText {
                                        text: root.btnText(modelData.button)
                                        Layout.preferredWidth: 40
                                        font.pixelSize: Math.round(12 * root.fs)
                                        color: "#2c241d"
                                    }
                                    HusText {
                                        text: "前间隔 " + Number(modelData.gap_before || 0).toFixed(1) + "s"
                                        Layout.fillWidth: true
                                        font.pixelSize: Math.round(12 * root.fs)
                                        color: Number(modelData.gap_before || 0) >= 2 ? "#b66f1f" : "#6d6257"
                                    }
                                    HusText {
                                        text: modelData.ts || ""
                                        font.pixelSize: Math.round(12 * root.fs)
                                        color: "#6d6257"
                                    }
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: root.selectedStep = index
                                }
                            }
                        }
                    }
                }

                HusCard {
                    Layout.fillWidth: true
                    title: "使用说明"
                    padding: 16
                    visible: root.events.length === 0

                    contentItem: ColumnLayout {
                        spacing: 8
                        HusText {
                            Layout.fillWidth: true
                            text: "1. 填序列名，选目标窗口"
                            font.pixelSize: Math.round(12 * root.fs)
                            color: "#2c241d"
                            font.bold: true
                            wrapMode: Text.Wrap
                        }
                        HusText {
                            Layout.fillWidth: true
                            leftPadding: 14
                            text: "· 选具体窗口：只录该窗口内点击（记录相对坐标，窗口移动也能回放）\n· 选“无”：记录全屏所有点击"
                            font.pixelSize: Math.round(11 * root.fs)
                            color: "#6d6257"
                            wrapMode: Text.Wrap
                            lineHeight: 1.35
                        }
                        HusText {
                            Layout.fillWidth: true
                            text: "2. 录制与窗口置顶"
                            font.pixelSize: Math.round(12 * root.fs)
                            color: "#2c241d"
                            font.bold: true
                            wrapMode: Text.Wrap
                        }
                        HusText {
                            Layout.fillWidth: true
                            leftPadding: 14
                            text: "· 点“开始录制”才会置前目标窗口（默认最小化本窗口让出空间）\n· 需常置顶再勾选“录制时置顶目标窗口”\n· 点击/按键（含 ESC）均会记录；小键盘0 停止，小键盘+ 打标记（回放跳过）\n· 停止后自动取消置顶、保存并恢复本窗口"
                            font.pixelSize: Math.round(11 * root.fs)
                            color: "#6d6257"
                            wrapMode: Text.Wrap
                            lineHeight: 1.35
                        }
                        HusText {
                            Layout.fillWidth: true
                            text: "3. 回放"
                            font.pixelSize: Math.round(12 * root.fs)
                            color: "#2c241d"
                            font.bold: true
                            wrapMode: Text.Wrap
                        }
                        HusText {
                            Layout.fillWidth: true
                            leftPadding: 14
                            text: "· 会还原点击间隔；先用“试跑”确认坐标再真跑"
                            font.pixelSize: Math.round(11 * root.fs)
                            color: "#6d6257"
                            wrapMode: Text.Wrap
                        }
                    }
                }
            }
        }
    }

    // ── 删除确认弹窗（非受保护序列二次确认，防误删）──
    Rectangle {
        visible: root.deleteConfirmOpen
        anchors.fill: parent
        color: Qt.rgba(0, 0, 0, 0.32)
        z: 999

        MouseArea {
            anchors.fill: parent
            onClicked: root.deleteConfirmOpen = false
        }

        HusCard {
            anchors.centerIn: parent
            width: 380
            padding: 20

            contentItem: ColumnLayout {
                spacing: 14
                HusText {
                    text: "确认删除？"
                    font.pixelSize: Math.round(16 * root.fs)
                    font.bold: true
                    color: "#2c241d"
                }
                HusText {
                    text: "将删除序列「" + root.pendingDeleteName + "」，此操作不可恢复。"
                    font.pixelSize: Math.round(13 * root.fs)
                    color: "#6d6257"
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
                HusText {
                    text: "一条龙内置序列已做防误删保护，不会出现在此确认框中。"
                    font.pixelSize: Math.round(11 * root.fs)
                    color: "#8a7a68"
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                    visible: root.pendingDeleteName !== ""
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 12
                    Item { Layout.fillWidth: true }
                    HusButton {
                        text: "取消"
                        type: HusButton.Type_Outlined
                        onClicked: root.deleteConfirmOpen = false
                    }
                    HusButton {
                        text: "确认删除"
                        type: HusButton.Type_Primary
                        onClicked: {
                            var name = root.pendingDeleteName
                            root.deleteConfirmOpen = false
                            root.pendingDeleteName = ""
                            if (name) bridge.deleteClickSequence(name)
                        }
                    }
                }
            }
        }
    }
}
