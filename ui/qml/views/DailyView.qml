import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import HuskarUI.Basic
import "../components"

// Daily progress check view
Item {
    id: root
    readonly property real fs: Application.window ? Application.window.fontScale : 1

    property var progressData: ({ date_key: "", games: [] })
    property bool isLoading: false

    // ── Game options ──
    property var gameOptions: [
        { key: "genshin",   name: "原神",       checked: true },
        { key: "honkai_sr", name: "星穹铁道",   checked: true },
        { key: "zzz",       name: "绝区零",     checked: true },
        { key: "wuwa",      name: "鸣潮",       checked: true },
        { key: "nikki",     name: "无限暖暖",   checked: true },
        { key: "endfield",  name: "终末地",     checked: true },
        { key: "nte",       name: "异环",       checked: true }
    ]

    Connections {
        target: bridge
        function onDailyProgressChanged(jsonStr) {
            try {
                root.progressData = JSON.parse(jsonStr || "{}")
            } catch(e) {
                root.progressData = { date_key: "", games: [] }
            }
            root.isLoading = false
            dailyStatusText.text = root.buildStatusText()
        }
        function onStatusTitleChanged() {
            if (bridge.statusTone === "running") {
                root.isLoading = true
                dailyStatusText.text = "正在查询..."
            }
        }
    }

    function buildStatusText() {
        var games = root.progressData.games || []
        if (games.length === 0) return "暂无数据，请先刷新"
        var success = 0, failed = 0
        for (var i = 0; i < games.length; i++) {
            if (games[i].is_all_success) success++
            if (games[i].has_failed) failed++
        }
        var dateKey = root.progressData.date_key || "今日"
        return dateKey + " 记录：" + games.length + " 款游戏，" + success + " 款完成，" + failed + " 款失败"
    }

    function collectSelectedGames() {
        var keys = []
        for (var i = 0; i < gameOptionsRepeater.count; i++) {
            var item = gameOptionsRepeater.itemAt(i)
            if (item && item.checked) keys.push(gameOptions[i].key)
        }
        return JSON.stringify(keys)
    }

    // ── Load persisted progress on startup ──
    Component.onCompleted: bridge.loadDailyProgress()

    ScrollView {
        anchors.fill: parent
        clip: true

        ColumnLayout {
            width: parent.width
            spacing: 14

            // Intro card
            HusCard {
                Layout.fillWidth: true
                padding: 20

                contentItem: ColumnLayout {
                    spacing: 10
                    HusText { text: "📅 每日进度查询"; font.pixelSize: Math.round(22 * root.fs); font.bold: true }
                    HusText {
                        text: "统一查询原神 / 星铁 / 绝区零 / 鸣潮 / 暖暖 / 终末地的每日完成度。\n请先在下方勾选需要查询的游戏，然后点击刷新按钮。"
                        font.pixelSize: Math.round(13 * root.fs)
                        color: "#6d6257"
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                    RowLayout {
                        spacing: 12
                        HusButton {
                            text: "刷新每日进度"
                            type: HusButton.Type_Primary
                            enabled: !root.isLoading
                            onClicked: {
                                root.isLoading = true
                                bridge.checkDailyProgress(root.collectSelectedGames())
                            }
                        }
                        HusButton {
                            text: "加载上次记录"
                            type: HusButton.Type_Outlined
                            enabled: !root.isLoading
                            onClicked: bridge.loadDailyProgress()
                        }
                    }
                }
            }

            // Game checkboxes
            HusCard {
                Layout.fillWidth: true
                title: "选择游戏"
                padding: 14

                contentItem: Flow {
                    spacing: 12
                    Layout.fillWidth: true

                    Repeater {
                        id: gameOptionsRepeater
                        model: root.gameOptions
                        HusCheckBox {
                            text: modelData.name
                            checked: modelData.checked
                        }
                    }
                }
            }

            // Status area
            HusCard {
                Layout.fillWidth: true
                title: "查询状态"
                padding: 14

                contentItem: HusText {
                    id: dailyStatusText
                    text: "点击刷新按钮开始查询..."
                    font.pixelSize: Math.round(13 * root.fs)
                    color: root.isLoading ? "#295d86" : "#6d6257"
                }
            }

            // Results list
            HusCard {
                Layout.fillWidth: true
                title: "查询结果"
                padding: 14

                contentItem: ColumnLayout {
                    spacing: 8
                    Layout.fillWidth: true

                    // Empty state
                    HusEmpty {
                        visible: (root.progressData.games || []).length === 0
                        description: "暂无数据，请先刷新"
                    }

                    // Game result cards
                    Repeater {
                        model: root.progressData.games || []

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: gameResultCol.implicitHeight + 24
                            color: modelData.is_all_success ? "#dff1e4" : (modelData.has_failed ? "#f7dfdd" : "#fffaf2")
                            radius: 10
                            border.color: "#dbcdbd"
                            border.width: 1

                            ColumnLayout {
                                id: gameResultCol
                                anchors.fill: parent
                                anchors.margins: 12
                                spacing: 6

                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 8

                                    HusText {
                                        text: modelData.game_name || modelData.game_key
                                        font.pixelSize: Math.round(16 * root.fs)
                                        font.bold: true
                                        Layout.fillWidth: true
                                    }

                                    HusTag {
                                        text: modelData.is_all_success ? "全部完成" : (modelData.has_failed ? "有失败" : "进行中")
                                        tagState: modelData.is_all_success ? HusTag.State_Success : (modelData.has_failed ? HusTag.State_Error : HusTag.State_Warning)
                                    }

                                    HusText {
                                        text: (modelData.progress_percent || 0) + "%"
                                        font.pixelSize: Math.round(13 * root.fs)
                                        color: "#6d6257"
                                    }
                                }

                                // Task details
                                Repeater {
                                    model: modelData.tasks || []

                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        spacing: 3
                                        // 签到任务走附属样式：左侧加边条 + 任务名前加 📬
                                        Rectangle {
                                            visible: modelData.is_sign_task === true
                                            Layout.fillWidth: true
                                            Layout.leftMargin: 4
                                            Layout.preferredHeight: 1
                                            color: "#dbcdbd"
                                        }

                                        RowLayout {
                                            Layout.fillWidth: true
                                            spacing: 6

                                            HusText {
                                                text: {
                                                    if (modelData.is_sign_task === true) {
                                                        if (modelData.status === "success") return "📬"
                                                        if (modelData.status === "failed") return "📭"
                                                        return "📨"
                                                    }
                                                    var s = modelData.status
                                                    if (s === "success") return "✅"
                                                    if (s === "failed") return "❌"
                                                    if (s === "already") return "✔"
                                                    if (s === "skipped") return "⏭"
                                                    return "⏳"
                                                }
                                                font.pixelSize: Math.round(14 * root.fs)
                                            }
                                            HusText {
                                                text: (modelData.is_sign_task === true ? "签到" : (modelData.task_name || modelData.task_key)) + (modelData.is_sign_task === true ? "（附属项，不计入完成度）" : "")
                                                font.pixelSize: Math.round(13 * root.fs)
                                                color: modelData.is_sign_task === true ? "#8a7a68" : "#2c241d"
                                                font.italic: modelData.is_sign_task === true
                                                Layout.fillWidth: true
                                            }
                                            HusText {
                                                text: {
                                                    var msg = modelData.message || ""
                                                    var cur = modelData.current
                                                    var tot = modelData.total
                                                    if (cur !== undefined && cur !== null &&
                                                        tot !== undefined && tot !== null && tot > 0) {
                                                        var pct = Math.round(cur / tot * 100)
                                                        return msg + "  (" + pct + "%)"
                                                    }
                                                    return msg
                                                }
                                                font.pixelSize: Math.round(12 * root.fs)
                                                color: modelData.status === "failed" ? "#b04b44" : "#6d6257"
                                            }
                                        }

                                        // 进度条：仅有 current/total 的任务显示（签到不显示进度条）
                                        Rectangle {
                                            visible: {
                                                if (modelData.is_sign_task === true) return false
                                                var cur = modelData.current
                                                var tot = modelData.total
                                                return cur !== undefined && cur !== null &&
                                                       tot !== undefined && tot !== null && tot > 0
                                            }
                                            Layout.fillWidth: true
                                            Layout.preferredHeight: 4
                                            color: "#e8ddd0"
                                            radius: 2

                                            Rectangle {
                                                property real ratio: {
                                                    var cur = modelData.current || 0
                                                    var tot = modelData.total || 1
                                                    return tot > 0 ? Math.min(1.0, cur / tot) : 0
                                                }
                                                width: parent.width * ratio
                                                height: parent.height
                                                color: modelData.status === "success" ? "#2f7d4a" :
                                                       (modelData.status === "failed" ? "#b04b44" : "#b66f1f")
                                                radius: 2
                                            }
                                        }
                                    }
                                }

                                // Error messages
                                Repeater {
                                    model: modelData.errors || []
                                    HusText {
                                        text: "⚠ " + modelData
                                        font.pixelSize: Math.round(12 * root.fs)
                                        color: "#b04b44"
                                        Layout.fillWidth: true
                                        wrapMode: Text.Wrap
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
