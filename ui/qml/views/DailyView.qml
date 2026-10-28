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

    // ── 历史回看 / 完成率统计 ──
    property var historyData: ({ ok: false, date_key: "", games: [], message: "" })
    property var statsData: ({ ok: false, days: 30, stats: [], date_from: "", date_to: "" })
    property var dateList: []
    property int statsDays: 30
    property bool historyLoading: false

    // ── Game options ──
    property var gameOptions: [
        { key: "genshin",   name: "原神",       checked: true },
        { key: "honkai_sr", name: "星穹铁道",   checked: true },
        { key: "zzz",       name: "绝区零",     checked: true },
        { key: "wuwa",      name: "鸣潮",       checked: true },
        { key: "nikki",     name: "无限暖暖",   checked: true },
        { key: "endfield",  name: "终末地",     checked: true },
        { key: "nte",       name: "异环",       checked: true },
        { key: "baijing",   name: "白荆回廊",   checked: true }
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
        function onDailyDatesChanged(jsonStr) {
            try {
                var d = JSON.parse(jsonStr || "{}")
                root.dateList = d.dates || []
            } catch(e) {
                root.dateList = []
            }
            dateSelectModel.clear()
            for (var i = 0; i < root.dateList.length; i++) {
                dateSelectModel.append({ label: root.dateList[i] })
            }
            if (dateSelectModel.count > 0 && dateSelect.currentIndex < 0) {
                // 默认选中"昨天"，回看当天没意义（今日数据在主列表里）
                dateSelect.currentIndex = Math.min(1, dateSelectModel.count - 1)
            }
        }
        function onDailyHistoryChanged(jsonStr) {
            try {
                root.historyData = JSON.parse(jsonStr || "{}")
            } catch(e) {
                root.historyData = { ok: false, date_key: "", games: [], message: "解析失败" }
            }
            root.historyLoading = false
        }
        function onDailyStatsChanged(jsonStr) {
            try {
                root.statsData = JSON.parse(jsonStr || "{}")
            } catch(e) {
                root.statsData = { ok: false, days: root.statsDays, stats: [], message: "解析失败" }
            }
        }
        function onStatusTitleChanged() {
            if (bridge.statusTone === "running") {
                root.isLoading = true
                dailyStatusText.text = "正在查询..."
            } else {
                // 任何非 running 的全局状态（成功/失败/空闲）都解除本页的查询锁，
                // 防止异常路径下 isLoading 卡死导致按钮一直禁用。
                root.isLoading = false
            }
        }
    }

    function buildStatusText() {
        var games = root.progressData.games || []
        if (games.length === 0) return "暂无数据，请先刷新"
        // 计数口径与卡片徽标一致：只认 completion_state（关键指标口径），
        // 禁止 is_all_success/has_failed——绝区零"活跃度已满"但 5 个边角任务
        // 失败，旧口径把它计进"失败"不计"完成"；终末地日活跃 100/100 也因
        // "体力 pending"不计完成（2026-10-19 用户拍板：活跃度是唯一准绳，
        // 细枝末节不拖结论）。字段缺失的老数据才回退旧逻辑。
        var done = 0, failed = 0, pending = 0, unknown = 0
        for (var i = 0; i < games.length; i++) {
            var st = games[i].completion_state
            if (st === "completed") done++
            else if (st === "failed") failed++
            else if (st === "pending") pending++
            else if (st === "unknown") unknown++
            else if (games[i].is_all_success) done++
            else if (games[i].has_failed) failed++
            else pending++
        }
        var dateKey = root.progressData.date_key || "今日"
        var txt = dateKey + " 记录：" + games.length + " 款游戏，" + done + " 完成，" + failed + " 失败"
        if (pending > 0) txt += "，" + pending + " 未完成"
        if (unknown > 0) txt += "，" + unknown + " 未刷新"
        return txt
    }

    // 卡片头部失败计数：status=failed 任务数 + errors 数。
    // 用于替代会误导的"脚本 0%"（失败了却显示 0% 完成度，2026-10-19 薅羊毛集合事故）。
    function failedTaskCount(game) {
        var tasks = (game && game.tasks) || []
        var n = 0
        for (var i = 0; i < tasks.length; i++) {
            if (tasks[i] && tasks[i].status === "failed") n++
        }
        n += ((game && game.errors) || []).length
        return n
    }

    // 找卡片内第一个验证截图 URL（验证截图统一渲染到卡片底部附件位，
    // 不再插在任务清单中间把列表截断）。
    function findVerifyImage(tasks) {
        var list = tasks || []
        for (var i = 0; i < list.length; i++) {
            if (list[i] && list[i].verify_image_url) return list[i].verify_image_url
        }
        return ""
    }

    function collectSelectedGames() {
        var keys = []
        for (var i = 0; i < gameOptionsRepeater.count; i++) {
            var item = gameOptionsRepeater.itemAt(i)
            if (item && item.checked) keys.push(gameOptions[i].key)
        }
        return JSON.stringify(keys)
    }

    // ── Load persisted progress on startup + 切回视图时按需刷新（避免每次切页都重载）──
    property double _lastLoadTs: 0
    // ── 失败诊断记录（flow_failures）──
    property var failuresData: ({ days: 7, summary: [], items: [], error: "" })
    property int failuresDays: 7
    property bool failuresLoading: false
    property double _lastFailuresTs: 0

    function loadFailures() {
        if (root.failuresLoading) return
        root.failuresLoading = true
        root._lastFailuresTs = Date.now()
        var raw = bridge.getRecentFlowFailures(root.failuresDays)
        try {
            root.failuresData = raw ? JSON.parse(raw) : { days: root.failuresDays, summary: [], items: [], error: "" }
        } catch (e) {
            root.failuresData = { days: root.failuresDays, summary: [], items: [], error: "解析失败: " + e }
        }
        root.failuresLoading = false
    }

    Component.onCompleted: {
        _lastLoadTs = Date.now()
        bridge.loadDailyProgress()
        bridge.loadDailyDates()
        bridge.loadDailyStats(root.statsDays)
        loadFailures()
    }
    onVisibleChanged: {
        if (visible) {
            var now = Date.now()
            // 60s 内不重复加载，利用已有缓存
            if (now - _lastLoadTs > 60000 || (root.progressData.games || []).length === 0) {
                _lastLoadTs = now
                bridge.loadDailyProgress()
            }
            // 失败记录 60s 节流刷新
            if (now - _lastFailuresTs > 60000) loadFailures()
        }
    }

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
                        text: "统一查询原神 / 星铁 / 绝区零 / 鸣潮 / 暖暖 / 终末地 / 异环 / 白荆回廊的每日完成度（含白荆验证截图）。\n请先在下方勾选需要查询的游戏，然后点击刷新按钮。"
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
                            // 卡片底色只跟 completion_state（关键指标口径）走：
                            // 完成=绿、关键指标失败=红、活跃度未满/未刷新=米白。
                            // 俱乐部每日任务失败等细枝末节不染红、不漂绿
                            // （白荆 15/100+俱乐部失败曾被染成红底"有失败"，
                            // 2026-10-19 用户拍板：结论只认活跃度）。
                            // 字段缺失的老数据才回退旧逻辑。
                            color: {
                                var st = modelData.completion_state
                                if (st === "completed") return "#dff1e4"
                                if (st === "failed") return "#f7dfdd"
                                if (st === "pending") return "#fffaf2"
                                if (st === "unknown") return "#fffaf2"
                                return modelData.is_all_success ? "#dff1e4" : (modelData.has_failed ? "#f7dfdd" : "#fffaf2")
                            }
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
                                        // 徽标=卡片结论，只跟 completion_state（关键指标口径，
                                        // 如白荆只看活跃度、绝区零只看活跃度奖励）：
                                        // 活跃度满→全部完成，未满→未完成；俱乐部每日任务
                                        // 失败等细枝末节只留在任务明细，不拖卡片结论
                                        // （2026-10-19 用户拍板）。字段缺失的老数据才回退旧逻辑。
                                        text: {
                                            var st = modelData.completion_state
                                            if (st === "completed") return "全部完成"
                                            if (st === "failed") return "有失败"
                                            if (st === "unknown") return "未刷新"
                                            if (st === "pending") return "未完成"
                                            return modelData.is_all_success ? "全部完成" : (modelData.has_failed ? "有失败" : "进行中")
                                        }
                                        tagState: {
                                            var st = modelData.completion_state
                                            if (st === "completed") return HusTag.State_Success
                                            if (st === "failed") return HusTag.State_Error
                                            if (st === "pending") return HusTag.State_Warning
                                            if (st === "unknown") return HusTag.State_Warning
                                            return modelData.is_all_success ? HusTag.State_Success : (modelData.has_failed ? HusTag.State_Error : HusTag.State_Warning)
                                        }
                                    }

                                    HusText {
                                        // 头部右侧数字跟卡片结论分工：
                                        // 已完成→只显示完成度。边角失败不翻旧账——绝区零
                                        // 活跃度已满但 5 个边角任务失败，头顶"失败 5 项"
                                        // 会自己打脸（2026-10-19 用户拍板：细枝末节
                                        // 只在任务明细里看）。
                                        // 失败→失败计数（如"失败 3 项"），替代会误导的
                                        // "脚本 0%"（薅羊毛集合事故）。
                                        // 未完成→完成度为主，失败计数作后缀。
                                        // progress_source 标明百分比口径：有真实计数
                                        // （如白荆活跃度 15/100）时是游玩进度，
                                        // 否则是脚本子任务完成比例，加 "脚本" 前缀避免误读。
                                        text: {
                                            var st = modelData.completion_state
                                            var failed = root.failedTaskCount(modelData)
                                            var p = modelData.progress_percent || 0
                                            var src = modelData.progress_source
                                            var pctTxt = (src && src !== "script") ? (p + "%") : ("脚本 " + p + "%")
                                            if (st === "completed") return pctTxt
                                            if (st === "failed") return "失败 " + failed + " 项"
                                            if (st === undefined) {
                                                // 老数据无 completion_state：保持旧行为
                                                return failed > 0 ? ("失败 " + failed + " 项") : pctTxt
                                            }
                                            // pending / unknown：完成度为主，失败计数作后缀
                                            return failed > 0 ? (pctTxt + " · 失败 " + failed + " 项") : pctTxt
                                        }
                                        font.pixelSize: Math.round(13 * root.fs)
                                        color: {
                                            // 只有"失败"结论才染红；未完成卡片的失败计数
                                            // 只是明细提示，不抢结论的颜色
                                            var st = modelData.completion_state
                                            if (st === "failed") return "#b04b44"
                                            if (st === undefined && root.failedTaskCount(modelData) > 0) return "#b04b44"
                                            return "#6d6257"
                                        }
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
                                                    // 有真实计数且未满（如日常任务 2/15、活跃度 15/100）：
                                                    // 显示部分完成 ◐，不打 ✅——2026-10-19 白荆日常
                                                    // 2/15 被打成 ✅，用户以为做完了。
                                                    var cur = modelData.current
                                                    var tot = modelData.total
                                                    if (cur !== undefined && cur !== null &&
                                                        tot !== undefined && tot !== null && tot > 0 &&
                                                        cur < tot && (s === "success" || s === "already")) {
                                                        return "◐"
                                                    }
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
                                                    // 官方 API 未刷新（如鸣潮活跃度需进游戏才更新）
                                                    // 时 cur 为 0，不能当真实进度展示
                                                    var ex = modelData.extra
                                                    if (ex && ex.stale === true) {
                                                        return msg + "  （未刷新）"
                                                    }
                                                    if (cur !== undefined && cur !== null &&
                                                        tot !== undefined && tot !== null && tot > 0) {
                                                        var pct = Math.round(cur / tot * 100)
                                                        var frac = cur + "/" + tot + " (" + pct + "%)"
                                                        // 消息里已含该分数（如"活跃度 15/100"）则不重复拼接
                                                        if (msg.indexOf(cur + "/" + tot) === -1) {
                                                            return msg ? msg + "  " + frac : frac
                                                        }
                                                        return msg
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

                                // 验证截图：固定附件位（卡片底部），不再插在任务清单
                                // 中间把列表截断（2026-10-19 排版问题）
                                Rectangle {
                                    visible: !!root.findVerifyImage(modelData.tasks || [])
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: visible ? 170 : 0
                                    color: "#f4ece0"
                                    radius: 8
                                    border.color: "#dbcdbd"
                                    border.width: 1
                                    clip: true

                                    Image {
                                        anchors.fill: parent
                                        anchors.margins: 4
                                        source: root.findVerifyImage(modelData.tasks || [])
                                        fillMode: Image.PreserveAspectFit
                                        cache: false
                                        asynchronous: true
                                        smooth: true
                                    }
                                    MouseArea {
                                        anchors.fill: parent
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: bridge.openBaijingVerifyImage(root.findVerifyImage(modelData.tasks || []))
                                    }
                                    // 左上角附件说明
                                    Rectangle {
                                        anchors.top: parent.top
                                        anchors.left: parent.left
                                        anchors.margins: 4
                                        color: Qt.rgba(0,0,0,0.45)
                                        radius: 4
                                        width: capText.implicitWidth + 10
                                        height: capText.implicitHeight + 4
                                        visible: parent.visible
                                        HusText {
                                            id: capText
                                            anchors.centerIn: parent
                                            text: "📎 验证截图"
                                            font.pixelSize: Math.round(10 * root.fs)
                                            color: "white"
                                        }
                                    }
                                    // 右下角提示
                                    Rectangle {
                                        anchors.bottom: parent.bottom
                                        anchors.right: parent.right
                                        anchors.margins: 4
                                        color: Qt.rgba(0,0,0,0.45)
                                        radius: 4
                                        width: tipText.implicitWidth + 10
                                        height: tipText.implicitHeight + 4
                                        visible: parent.visible
                                        HusText {
                                            id: tipText
                                            anchors.centerIn: parent
                                            text: "点击查看原图"
                                            font.pixelSize: Math.round(10 * root.fs)
                                            color: "white"
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }

            // ── 完成率统计（最近 N 天）──
            HusCard {
                Layout.fillWidth: true
                title: "完成率统计"
                padding: 14

                contentItem: ColumnLayout {
                    spacing: 8
                    Layout.fillWidth: true

                    RowLayout {
                        spacing: 10
                        HusText { text: "最近"; font.pixelSize: Math.round(13 * root.fs) }
                        HusSelect {
                            id: statsDaysSelect
                            clearEnabled: false
                            model: ListModel {
                                ListElement { label: "7 天";  value: 7 }
                                ListElement { label: "30 天"; value: 30 }
                                ListElement { label: "90 天"; value: 90 }
                            }
                            currentIndex: 1
                            onActivated: {
                                root.statsDays = statsDaysSelect.model.get(currentIndex).value
                                bridge.loadDailyStats(root.statsDays)
                            }
                        }
                        HusText {
                            text: {
                                var s = root.statsData
                                if (!s.ok) return "统计失败"
                                if (!s.date_from) return "暂无记录"
                                return s.date_from + " ~ " + s.date_to
                            }
                            font.pixelSize: Math.round(12 * root.fs)
                            color: "#6d6257"
                        }
                        Item { Layout.fillWidth: true }
                    }

                    HusText {
                        visible: !root.statsData.ok || (root.statsData.stats || []).length === 0
                        text: "暂无统计数据"
                        font.pixelSize: Math.round(12 * root.fs)
                        color: "#8a7a68"
                    }

                    Repeater {
                        model: root.statsData.stats || []
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 3

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 8
                                HusText {
                                    text: modelData.game_name || modelData.game_key
                                    font.pixelSize: Math.round(13 * root.fs)
                                    Layout.preferredWidth: Math.round(120 * root.fs)
                                }
                                // 完成率进度条
                                Rectangle {
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 8
                                    radius: 4
                                    color: "#e8ddd0"
                                    Rectangle {
                                        anchors.left: parent.left
                                        anchors.top: parent.top
                                        anchors.bottom: parent.bottom
                                        width: Math.max(0, parent.width * (modelData.completion_rate || 0) / 100)
                                        radius: 4
                                        color: {
                                            var r = modelData.completion_rate || 0
                                            if (r >= 90) return "#2f7d4a"
                                            if (r >= 60) return "#b66f1f"
                                            return "#b04b44"
                                        }
                                    }
                                }
                                HusText {
                                    text: {
                                        var r = modelData.completion_rate || 0
                                        var unk = modelData.days_unknown || 0
                                        if (unk > 0 && (modelData.days_recorded - unk) <= 0) return "无有效数据"
                                        return r + "%"
                                    }
                                    font.pixelSize: Math.round(12 * root.fs)
                                    color: "#6d6257"
                                    Layout.preferredWidth: Math.round(70 * root.fs)
                                }
                            }
                            HusText {
                                text: {
                                    var unk = modelData.days_unknown || 0
                                    var eff = modelData.days_recorded - unk
                                    var base = "完成 " + modelData.days_completed + " / 有效 " + eff + " 天"
                                    if (unk > 0) {
                                        base += "　（另有 " + unk + " 天数据未刷新，不计入分母）"
                                    }
                                    return base
                                }
                                font.pixelSize: Math.round(11 * root.fs)
                                color: "#8a7a68"
                                Layout.leftMargin: Math.round(120 * root.fs)
                            }
                        }
                    }
                }
            }

            // ── 历史回看（按日期）──
            HusCard {
                Layout.fillWidth: true
                title: "历史回看"
                padding: 14

                contentItem: ColumnLayout {
                    spacing: 8
                    Layout.fillWidth: true

                    RowLayout {
                        spacing: 10
                        HusText { text: "日期:"; font.pixelSize: Math.round(13 * root.fs) }
                        HusSelect {
                            id: dateSelect
                            clearEnabled: false
                            model: ListModel { id: dateSelectModel }
                            currentIndex: -1
                        }
                        HusButton {
                            text: root.historyLoading ? "查询中..." : "查看这天"
                            type: HusButton.Type_Primary
                            enabled: !root.historyLoading && dateSelect.currentIndex >= 0
                            onClicked: {
                                if (dateSelect.currentIndex < 0) return
                                root.historyLoading = true
                                bridge.loadDailyHistory(dateSelect.model.get(dateSelect.currentIndex).label)
                            }
                        }
                        Item { Layout.fillWidth: true }
                    }

                    HusText {
                        visible: root.historyData.message !== undefined && root.historyData.message !== ""
                        text: root.historyData.message || ""
                        font.pixelSize: Math.round(12 * root.fs)
                        color: "#8a7a68"
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }

                    Repeater {
                        model: root.historyData.games || []
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 8
                                HusText {
                                    text: modelData.game_name || modelData.game_key
                                    font.pixelSize: Math.round(13 * root.fs)
                                    font.bold: true
                                    Layout.fillWidth: true
                                }
                                HusTag {
                                    text: {
                                        var st = modelData.completion_state
                                        if (st === "completed") return "已完成"
                                        if (st === "unknown") return "数据未刷新"
                                        if (st === "failed") return "有失败"
                                        return "未完成"
                                    }
                                    tagState: {
                                        var st = modelData.completion_state
                                        if (st === "completed") return HusTag.State_Success
                                        if (st === "failed") return HusTag.State_Error
                                        return HusTag.State_Warning
                                    }
                                }
                                HusText {
                                    text: (modelData.progress_percent || 0) + "%"
                                    font.pixelSize: Math.round(12 * root.fs)
                                    color: "#6d6257"
                                }
                            }
                            // 关键任务明细（只列带数值的，避免刷屏）
                            Repeater {
                                model: {
                                    var out = []
                                    var ts = modelData.tasks || []
                                    for (var i = 0; i < ts.length; i++) {
                                        if (ts[i].total > 0) out.push(ts[i])
                                    }
                                    return out
                                }
                                HusText {
                                    text: "  · " + modelData.task_name + " " + (modelData.message || "")
                                    font.pixelSize: Math.round(11 * root.fs)
                                    color: "#6d6257"
                                    Layout.fillWidth: true
                                }
                            }
                        }
                    }
                }
            }

            // ── 最近失败记录（失败取证：截图 + 当帧文字，事后定位"哪一步最常失败"）──
            HusCard {
                Layout.fillWidth: true
                title: "最近失败记录"
                padding: 14

                contentItem: ColumnLayout {
                    spacing: 8
                    Layout.fillWidth: true

                    RowLayout {
                        spacing: 10
                        HusText { text: "最近"; font.pixelSize: Math.round(13 * root.fs) }
                        HusSelect {
                            id: failuresDaysSelect
                            clearEnabled: false
                            model: ListModel {
                                ListElement { label: "7 天";  value: 7 }
                                ListElement { label: "30 天"; value: 30 }
                                ListElement { label: "90 天"; value: 90 }
                            }
                            currentIndex: 0
                            onActivated: {
                                root.failuresDays = failuresDaysSelect.model.get(currentIndex).value
                                root.loadFailures()
                            }
                        }
                        HusButton {
                            text: root.failuresLoading ? "加载中..." : "刷新"
                            enabled: !root.failuresLoading
                            onClicked: root.loadFailures()
                        }
                        HusButton {
                            text: "打开证据目录"
                            type: HusButton.Type_Outlined
                            onClicked: bridge.openFlowFailureDir()
                        }
                        Item { Layout.fillWidth: true }
                        HusText {
                            text: {
                                var d = root.failuresData
                                if (d && d.error) return d.error
                                var n = (d && d.items) ? d.items.length : 0
                                return n > 0 ? ("共 " + n + " 条") : "最近无失败记录 ✅"
                            }
                            font.pixelSize: Math.round(12 * root.fs)
                            color: "#6d6257"
                        }
                    }

                    // 高频失败聚合（次数最多的前几条：最该优化的步骤）
                    Repeater {
                        model: (root.failuresData.summary || []).slice(0, 5)
                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 26
                            radius: 6
                            color: "#fdf1ef"
                            border.color: "#e6c9c4"
                            border.width: 1

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: 8
                                anchors.rightMargin: 8
                                spacing: 8

                                HusTag { text: modelData.fail_count + " 次"; tagState: HusTag.State_Error }
                                HusText {
                                    text: (modelData.flow_key || "?") + " / " + (modelData.step || "?")
                                    font.pixelSize: Math.round(12 * root.fs)
                                    Layout.fillWidth: true
                                    elide: Text.ElideRight
                                }
                                HusText {
                                    text: modelData.has_image ? "有截图" : "无截图"
                                    font.pixelSize: Math.round(11 * root.fs)
                                    color: "#8a7a68"
                                }
                            }
                        }
                    }

                    HusEmpty {
                        visible: (root.failuresData.items || []).length === 0
                        description: "暂无失败记录"
                    }

                    // 明细：时间 / 流程·步骤 / 错误 / 当帧文字 / 截图（点击开原图）
                    Repeater {
                        model: (root.failuresData.items || []).slice(0, 30)
                        Rectangle {
                            id: failureRow
                            Layout.fillWidth: true
                            Layout.preferredHeight: failCol.implicitHeight + 16
                            radius: 8
                            color: "#fffaf2"
                            border.color: "#dbcdbd"
                            border.width: 1

                            ColumnLayout {
                                id: failCol
                                anchors.fill: parent
                                anchors.margins: 8
                                spacing: 4

                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 8
                                    HusText {
                                        text: modelData.time_text || ""
                                        font.pixelSize: Math.round(11 * root.fs)
                                        color: "#8a7a68"
                                    }
                                    HusText {
                                        text: (modelData.flow_key || "?") + " / " + (modelData.step || "?")
                                        font.pixelSize: Math.round(13 * root.fs)
                                        font.bold: true
                                        Layout.fillWidth: true
                                        elide: Text.ElideRight
                                    }
                                    HusTag { text: modelData.game_key || "-"; tagState: HusTag.State_Warning }
                                }

                                HusText {
                                    text: modelData.error || ""
                                    font.pixelSize: Math.round(12 * root.fs)
                                    color: "#b04b44"
                                    Layout.fillWidth: true
                                    wrapMode: Text.Wrap
                                }

                                HusText {
                                    visible: (modelData.ocr_texts || []).length > 0
                                    text: "当帧文字: " + (modelData.ocr_texts || []).join(" ｜ ")
                                    font.pixelSize: Math.round(11 * root.fs)
                                    color: "#6d6257"
                                    Layout.fillWidth: true
                                    wrapMode: Text.Wrap
                                    maximumLineCount: 2
                                    elide: Text.ElideRight
                                }

                                Rectangle {
                                    visible: !!modelData.image_url
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: visible ? 150 : 0
                                    color: "#f4ece0"
                                    radius: 8
                                    border.color: "#dbcdbd"
                                    border.width: 1
                                    clip: true

                                    Image {
                                        anchors.fill: parent
                                        anchors.margins: 4
                                        source: modelData.image_url || ""
                                        fillMode: Image.PreserveAspectFit
                                        cache: false
                                        asynchronous: true
                                        smooth: true
                                    }
                                    MouseArea {
                                        anchors.fill: parent
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: bridge.openFlowFailureImage(modelData.image_url || "")
                                    }
                                    Rectangle {
                                        anchors.bottom: parent.bottom
                                        anchors.right: parent.right
                                        anchors.margins: 4
                                        color: Qt.rgba(0,0,0,0.45)
                                        radius: 4
                                        width: failTip.implicitWidth + 10
                                        height: failTip.implicitHeight + 4
                                        visible: parent.visible
                                        HusText {
                                            id: failTip
                                            anchors.centerIn: parent
                                            text: "点击查看原图"
                                            font.pixelSize: Math.round(10 * root.fs)
                                            color: "white"
                                        }
                                    }
                                }

                                HusButton {
                                    visible: !modelData.image_url
                                    text: "打开证据目录"
                                    type: HusButton.Type_Outlined
                                    onClicked: bridge.openFlowFailureDir()
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
