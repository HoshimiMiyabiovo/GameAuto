import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import QtQuick.Dialogs
import HuskarUI.Basic
import "../components"

// Wuthering Waves — Kujiequ sign + XLSX gacha analysis
Item {
    id: root
    readonly property real fs: Application.window ? Application.window.fontScale : 1

    property var gachaData: ({})
    property var overview: ({})
    property var pools: []

    // 库街区每日数据（体力/活跃度/周本/深塔等）
    property var dailyData: ({})
    property bool dailyLoading: false

    // 库街区凭据状态：token/did 已加密存入数据库，界面不回填明文
    property bool tokenConfigured: false
    property bool didConfigured: false

    // 页面激活标志：仅在页面可见时创建 delegate，避免 width=0 时创建后不更新
    readonly property bool pageActive: bridge.currentView === "wuwa"

    Component.onCompleted: {
        bridge.loadWuwaGacha()
        bridge.loadCredentials()
        bridge.loadWuwaDailyCache()
    }

    property double _lastVisibleTs: 0
    // 视图从隐藏→显示时按需灌凭据/缓存，30s 节流
    onVisibleChanged: {
        if (visible && Date.now() - _lastVisibleTs > 30000) {
            _lastVisibleTs = Date.now()
            bridge.loadCredentials()
            bridge.loadWuwaDailyCache()
        }
    }

    Connections {
        target: bridge
        function onWuwaDailyChanged(jsonStr) {
            try { root.dailyData = JSON.parse(jsonStr || "{}") } catch(e) { root.dailyData = ({}) }
            root.dailyLoading = false
        }
        function onWuwaGachaChanged(jsonStr) {
            try { root.gachaData = JSON.parse(jsonStr || "{}") } catch(e) { root.gachaData = ({}) }
            root.overview = root.gachaData.overview || {}
            root.pools = root.gachaData.pools || []
            root.updateDisplay()
        }
        function onCredentialsChanged(jsonStr) {
            try {
                var creds = JSON.parse(jsonStr || "{}")
                var wuwa = creds.wuwa_kujiequ || {}
                var profile = wuwa.profile || {}
                // token/did 已迁移到数据库加密存储，这里不回填明文，
                // 只用布尔标志提示"是否已配置"
                root.tokenConfigured = !!profile.token_configured
                root.didConfigured = !!profile.did_configured
                wuwaAutoSign.checked = !!wuwa.auto_sign
            } catch(e) { /* ignore */ }
        }
    }

    function updateDisplay() {
        var ov = root.overview
        totalCard.value = ov.total_records || "-"
        fiveStarCard.value = ov.five_star_count || "-"
        totalCard.note = ov.latest_time || "等待加载"
        fiveStarCard.note = ov.earliest_time ? ("从 " + ov.earliest_time) : "等待加载"
        // UP 歪率 / 大保底 / 保底预测（gacha.luck_analysis headline）
        var luck = (root.gachaData && root.gachaData.luck) || {}
        var head = luck.headline || {}
        if (head.win_rate_text && head.win_rate_text !== "-") {
            luckCard.value = head.win_rate_text
            var noteParts = [head.state_text || ""]
            if (head.expected_pulls != null) {
                noteParts.push("距下个金期望 " + head.expected_pulls + " 抽 / 最坏 " + head.worst_case + " 抽")
            }
            luckCard.note = noteParts.filter(function (s) { return s !== "" }).join(" · ")
        } else {
            luckCard.value = "-"
            luckCard.note = head.expected_pulls != null
                ? ("限定池暂无五星 · 距下个金期望 " + head.expected_pulls + " 抽")
                : "等待加载"
        }
    }

    ScrollView {
        anchors.fill: parent
        clip: true

        ColumnLayout {
            width: parent.width
            spacing: 14

            // Kujiequ sign section
            HusCard {
                Layout.fillWidth: true
                title: "🌊 库街区签到"
                padding: 16

                contentItem: ColumnLayout {
                    spacing: 10

                    CredentialInput {
                        id: wuwaTokenInput
                        Layout.fillWidth: true
                        placeholderText: "Token（已加密保存，留空沿用；填写则覆盖）..."
                        onEditingFinished: bridge.saveCredential("wuwa_kujiequ", "profile.token", text)
                    }

                    RowLayout {
                        spacing: 10
                        HusSelect {
                            clearEnabled: false
                            model: ListModel {
                                ListElement { label: "账号密码" }
                                ListElement { label: "Token" }
                            }
                            currentIndex: 0
                        }
                        HusButton { text: "登录"; type: HusButton.Type_Primary }
                        HusButton { text: "刷新信息"; type: HusButton.Type_Outlined }
                        HusButton {
                            text: "ADB 抓取 Token"
                            type: HusButton.Type_Outlined
                            enabled: !bridge.isRunning
                            onClicked: bridge.runWuwaKujiequAdbGrab()
                        }
                        HusButton {
                            text: "执行签到"
                            type: HusButton.Type_Outlined
                            enabled: !bridge.isRunning
                            onClicked: {
                                if (wuwaTokenInput.text.length > 0) {
                                    bridge.saveCredential("wuwa_kujiequ", "profile.token", wuwaTokenInput.text)
                                }
                                bridge.runWuwaKujiequSign()
                            }
                        }
                    }

                    HusCheckBox {
                        id: wuwaAutoSign
                        text: "启动时自动签到"
                        checked: false
                        onClicked: bridge.saveCredentialBool("wuwa_kujiequ", "auto_sign", checked)
                    }

                    HusText {
                        text: (root.tokenConfigured ? "Token: 已加密保存" : "Token: 未配置")
                              + "  |  " + (root.didConfigured ? "DID: 已加密保存" : "DID: 未配置")
                        font.pixelSize: Math.round(12 * root.fs)
                        color: (root.tokenConfigured && root.didConfigured) ? "#2f7d4a" : "#b04b44"
                    }
                }
            }

            // 库街区每日数据（体力 / 活跃度 / 周本 / 深塔）
            HusCard {
                Layout.fillWidth: true
                title: "📅 库街区每日数据"
                padding: 16

                contentItem: ColumnLayout {
                    spacing: 10

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8

                        HusText {
                            text: {
                                var d = root.dailyData || {}
                                if (!d.ok) return "库街区每日数据"
                                var name = d.role_name || ""
                                var srv = d.server_name || ""
                                return (name ? name : "库街区") + (srv ? (" · " + srv) : "")
                            }
                            font.pixelSize: Math.round(14 * root.fs)
                            font.bold: true
                            color: "#2c241d"
                            Layout.fillWidth: true
                        }

                        HusButton {
                            text: root.dailyLoading ? "刷新中..." : "刷新"
                            type: HusButton.Type_Outlined
                            enabled: !root.dailyLoading && !bridge.isRunning
                            onClicked: {
                                root.dailyLoading = true
                                bridge.refreshWuwaDaily()
                            }
                        }
                    }

                    // 缓存更新时间提示（与终末地一致）
                    HusText {
                        visible: !!(root.dailyData || {}).cached_at
                        text: {
                            var t = (root.dailyData || {}).cached_at
                            if (!t) return ""
                            var d = new Date(t * 1000)
                            function p(n) { return (n < 10 ? "0" : "") + n }
                            return "📦 更新于 " + p(d.getHours()) + ":" + p(d.getMinutes()) + " · 过期后自动刷新"
                        }
                        font.pixelSize: Math.round(11 * root.fs)
                        color: "#8a7f73"
                    }

                    // 签到状态
                    HusText {
                        visible: !!(root.dailyData || {}).ok
                        text: {
                            var d = root.dailyData || {}
                            return "签到：" + (d.has_signed ? (d.sign_text || "已签到") : (d.sign_text || "未签到"))
                        }
                        font.pixelSize: Math.round(12 * root.fs)
                        color: (root.dailyData || {}).has_signed ? "#2f7d4a" : "#b04b44"
                    }

                    // 空态 / 失败提示
                    HusText {
                        visible: !(root.dailyData || {}).ok
                        text: {
                            var d = root.dailyData || {}
                            return d.message ? ("获取失败：" + d.message) : "暂无数据，点击「刷新」获取"
                        }
                        font.pixelSize: Math.round(12 * root.fs)
                        color: "#8a7a68"
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }

                    // 数据项：名称 + 数值 + 进度条
                    Repeater {
                        model: (root.dailyData || {}).items || []

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 4

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 6

                                HusText {
                                    text: modelData.name || ""
                                    font.pixelSize: Math.round(13 * root.fs)
                                    color: "#2c241d"
                                    Layout.fillWidth: true
                                }

                                HusText {
                                    text: {
                                        var cur = modelData.cur
                                        var tot = modelData.total
                                        if (tot !== undefined && tot !== null && tot > 0) {
                                            var pct = Math.round(cur / tot * 100)
                                            return cur + " / " + tot + "  (" + pct + "%)"
                                        }
                                        return (cur !== undefined && cur !== null) ? String(cur) : "-"
                                    }
                                    font.pixelSize: Math.round(12 * root.fs)
                                    color: (modelData.total > 0 && modelData.cur >= modelData.total)
                                           ? "#2f7d4a" : "#6d6257"
                                }
                            }

                            Rectangle {
                                visible: modelData.total !== undefined
                                         && modelData.total !== null
                                         && modelData.total > 0
                                Layout.fillWidth: true
                                Layout.preferredHeight: 4
                                color: "#e8ddd0"
                                radius: 2

                                Rectangle {
                                    property real ratio: {
                                        var cur = modelData.cur || 0
                                        var tot = modelData.total || 1
                                        return tot > 0 ? Math.min(1.0, cur / tot) : 0
                                    }
                                    width: parent.width * ratio
                                    height: parent.height
                                    radius: 2
                                    color: {
                                        var cur = modelData.cur || 0
                                        var tot = modelData.total || 0
                                        return (tot > 0 && cur >= tot) ? "#2f7d4a" : "#c98a2e"
                                    }
                                }
                            }
                        }
                    }

                    // 活跃度滞后提示（官方限制：需进游戏才刷新）
                    HusText {
                        visible: !!(root.dailyData || {}).ok
                        text: (root.dailyData || {}).stale_hint || ""
                        font.pixelSize: Math.round(11 * root.fs)
                        color: "#8a7a68"
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                }
            }

            // Gacha analysis section (from DB)
            HusCard {
                Layout.fillWidth: true
                title: "📊 鸣潮抽卡分析"
                padding: 16

                contentItem: ColumnLayout {
                    spacing: 10

                    RowLayout {
                        spacing: 10
                        GachaMetricCard { id: totalCard; title: "总抽数"; value: "-"; note: "等待加载" }
                        GachaMetricCard { id: fiveStarCard; title: "5 星数"; value: "-"; note: "等待加载" }
                        GachaMetricCard { id: luckCard; title: "UP 歪率"; value: "-"; note: "等待加载" }
                    }

                    HusText {
                        text: root.pools.length === 0
                              ? "暂无鸣潮抽卡记录。"
                              : "UID: " + (root.gachaData.uid || "") + " | 已加载 " + root.pools.length + " 个卡池"
                        font.pixelSize: Math.round(13 * root.fs)
                        color: "#6d6257"
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }

                    RowLayout {
                        spacing: 10
                        HusButton {
                            text: "刷新"
                            type: HusButton.Type_Outlined
                            enabled: !bridge.isRunning
                            onClicked: bridge.loadWuwaGacha()
                        }
                        HusButton {
                            text: "导入 pool.json"
                            type: HusButton.Type_Outlined
                            enabled: !bridge.isRunning
                            onClicked: wuwaPoolJsonDialog.open()
                        }
                    }

                    Repeater {
                        model: root.pools

                        HusCard {
                            Layout.fillWidth: true
                            title: modelData.pool_name || "未知卡池"
                            padding: 14

                            contentItem: ColumnLayout {
                                spacing: 10

                                // 概览：GridLayout 4 列，防文字溢出
                                GridLayout {
                                    Layout.fillWidth: true
                                    columns: 4
                                    rowSpacing: 6
                                    columnSpacing: 12

                                    HusText {
                                        Layout.alignment: Qt.AlignLeft
                                        text: "总抽数: " + (modelData.total_pulls || 0)
                                        font.pixelSize: Math.round(13 * root.fs); color: "#2f4f4f"
                                    }
                                    HusText {
                                        Layout.alignment: Qt.AlignLeft
                                        text: "5★: " + (modelData.five_star_count || 0)
                                        font.pixelSize: Math.round(13 * root.fs); color: "#a8351f"
                                    }
                                    HusText {
                                        Layout.alignment: Qt.AlignLeft
                                        text: "出货率: " + (modelData.five_star_rate || "0%")
                                        font.pixelSize: Math.round(13 * root.fs); color: "#2f7d4a"
                                    }
                                }

                                HusText {
                                    text: "5★ 出货历史 (最新在前，共 " + (modelData.high_rarity_history || []).length + " 条)"
                                    font.pixelSize: Math.round(13 * root.fs)
                                    font.bold: true
                                    color: "#2f4f4f"
                                    Layout.topMargin: 6
                                }

                                // 5★ 历史 — Flet 风格横向行卡片列表
                                ListView {
                                    id: wuwaHistoryList
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: Math.min((modelData.high_rarity_history || []).length, 50) * 42 + 10
                                    visible: root.pageActive && (modelData.high_rarity_history || []).length > 0
                                    clip: true
                                    interactive: true
                                    spacing: 4

                                    // 倒序：最新在前；页面未激活时不创建 delegate
                                    model: {
                                        if (!root.pageActive) return []
                                        var list = (modelData.high_rarity_history || []).slice(0)
                                        return list.reverse()
                                    }

                                    delegate: Rectangle {
                                        width: wuwaHistoryList.width
                                        height: 38
                                        required property var modelData
                                        color: "#faf6f0"
                                        radius: 6
                                        border.color: "#e0d6c8"
                                        border.width: 1

                                        RowLayout {
                                            anchors.fill: parent
                                            anchors.leftMargin: 10
                                            anchors.rightMargin: 10
                                            spacing: 8

                                            // 时间 (muted, 固定窄宽度)
                                            HusText {
                                                text: (modelData.time || "").substring(0, 16)
                                                font.pixelSize: Math.round(12 * root.fs); color: "#999"
                                                Layout.preferredWidth: 110
                                            }
                                            // 5★ 标签
                                            HusText {
                                                text: "5★"
                                                font.pixelSize: Math.round(12 * root.fs); font.bold: true; color: "#d4a017"
                                                Layout.preferredWidth: 30
                                            }
                                            // 名字 (金色粗体, 可 tooltip, 弹性压缩+elide)
                                            HusText {
                                                text: modelData.name || "?"
                                                font.pixelSize: Math.round(14 * root.fs); font.bold: true; color: "#d4a017"
                                                Layout.fillWidth: true
                                                Layout.minimumWidth: 60
                                                elide: Text.ElideRight
                                                ToolTip.text: modelData.name || ""
                                                ToolTip.visible: wuwaItemHover.containsMouse
                                                ToolTip.delay: 200
                                                MouseArea {
                                                    id: wuwaItemHover
                                                    anchors.fill: parent
                                                    hoverEnabled: true
                                                    cursorShape: Qt.IBeamCursor
                                                }
                                            }
                                            // 第几抽 (绿色, 固定宽度, 确保优先显示)
                                            HusText {
                                                text: modelData.pity ? ("第 " + modelData.pity + " 抽") : ""
                                                font.pixelSize: Math.round(13 * root.fs); color: "#2f7d4a"; font.bold: true
                                                Layout.minimumWidth: 70
                                                Layout.preferredWidth: 80
                                                horizontalAlignment: Text.AlignRight
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }

                    HusEmpty {
                        visible: root.pools.length === 0
                        description: "暂无鸣潮抽卡数据"
                    }
                }
            }
        }
    }

    // 多选 JSON 文件对话框（用于导入 WutheringWavesTool 的 pool.json）
    FileDialog {
        id: wuwaPoolJsonDialog
        title: "选择 pool.json 文件（可多选）"
        fileMode: FileDialog.OpenFiles
        defaultSuffix: "json"
        nameFilters: ["JSON 文件 (*.json)", "所有文件 (*)"]
        onAccepted: {
            var paths = []
            for (var i = 0; i < currentFiles.length; i++) {
                paths.push(String(currentFiles[i].toString()).replace(/^file:\/\//, ""))
            }
            if (paths.length > 0) {
                bridge.importWuwaGachaFromJson(JSON.stringify(paths))
            }
        }
    }
}
