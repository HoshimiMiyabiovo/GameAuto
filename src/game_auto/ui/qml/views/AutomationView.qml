import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import HuskarUI.Basic
import "../components"

// Automation View — Task selection, run/stop, log output
Item {
    id: root
    readonly property real fs: Application.window ? Application.window.fontScale : 1

    // ── Local state ──
    property var appList: []          // parsed from bridge.appListJson
    property var queueItems: []       // parsed from bridge.queueListJson
    property bool shutdownAfterFinish: false  // 一条龙：完成后自动关机
    property bool emailAfterFinish: false     // 一条龙：完成后发送每日进度邮件
    property bool forceRerun: false           // 强制重跑：跳过"今日已完成"检查
    property var schedTasks: []               // 定时启动任务列表（bridge.scheduledTasksJson）

    // ── Email config dialog state ──
    property bool emailDialogOpen: false
    property var emailDraft: ({})
    property string emailDialogStatus: ""        // 表单底部提示
    property color emailDialogStatusColor: "#6d6257"

    function openEmailConfigDialog() {
        try {
            var raw = bridge.getEmailConfig()
            var cfg = JSON.parse(raw || "{}")
        } catch(e) { cfg = {} }
        root.emailDraft = {
            smtp_host: cfg.smtp_host || "",
            smtp_port: cfg.smtp_port || 465,
            use_ssl: cfg.use_ssl !== false,
            sender: cfg.sender || "",
            password: "",
            has_password: !!cfg.has_password,
            recipients: (cfg.recipients || []).join(", "),
            subject_prefix: cfg.subject_prefix || "[GameAuto]"
        }
        root.emailDialogStatus = cfg.has_password
            ? "已保存授权码（加密）；如需更换请在下方重新输入"
            : "首次配置：QQ/163/Gmail 等需在邮箱后台开启 SMTP 并使用授权码"
        root.emailDialogStatusColor = "#6d6257"
        root.emailDialogOpen = true
        passwordField.clear()
    }

    function submitEmailConfig() {
        // 直接从输入框读值，不依赖 emailDraft 对象内部属性（QML property var 修改内部属性不可靠）
        var smtpHost = smtpHostField.text || ""
        var sender = senderField.text || ""
        var recipientsRaw = recipientsField.text || ""
        var recipients = recipientsRaw.split(/[,，;；\n]/).map(function(s){ return s.trim() }).filter(function(s){ return s.length > 0 })
        var password = passwordField.text || ""
        var smtpPort = parseInt(smtpPortField.text) || 465
        var useSsl = useSslField.checked
        var subjectPrefix = subjectPrefixField.text || "[GameAuto]"

        if (!smtpHost || !sender || recipients.length === 0) {
            root.emailDialogStatus = "SMTP 主机、发件人、收件人不能为空"
            root.emailDialogStatusColor = "#b04b44"
            return
        }
        if (!root.emailDraft.has_password && !password) {
            root.emailDialogStatus = "请填写 SMTP 授权码（或关闭弹窗保持现有配置）"
            root.emailDialogStatusColor = "#b04b44"
            return
        }
        var payload = {
            enabled: true,
            smtp_host: smtpHost,
            smtp_port: smtpPort,
            use_ssl: useSsl,
            sender: sender,
            password: password,  // 空 = 沿用旧密码
            recipients: recipients,
            subject_prefix: subjectPrefix
        }
        bridge.saveEmailConfig(JSON.stringify(payload))
        root.emailDialogOpen = false
    }

    // ── Parse bridge JSON when it changes ──
    Connections {
        target: bridge
        function onAppListChanged(jsonStr) {
            try { root.appList = JSON.parse(jsonStr || "[]") } catch(e) { root.appList = [] }
            // 同步到定时启动的任务下拉框
            schedTaskModel.clear()
            for (var j = 0; j < root.appList.length; j++) {
                schedTaskModel.append({ label: root.appList[j].display_name || root.appList[j].app_key })
            }
            taskListModel.clear()
            for (var i = 0; i < root.appList.length; i++) {
                var app = root.appList[i]
                taskListModel.append({
                    app_key: app.app_key,
                    taskName: app.display_name,
                    enabled: app.enabled,
                    script_choice: app.script_choice,
                    launcherLabelsJson: JSON.stringify(app.launcher_labels || []),
                    launcherValuesJson: JSON.stringify(app.launcher_values || []),
                    taskType: app.task_type || "常规任务",
                    description: app.description || "",
                    orderNum: app.order
                })
            }
            root.updateSelectionSummary()
        }
        function onQueueListChanged(jsonStr) {
            try { root.queueItems = JSON.parse(jsonStr || "[]") } catch(e) { root.queueItems = [] }
            queueListModel.clear()
            for (var i = 0; i < root.queueItems.length; i++) {
                var item = root.queueItems[i]
                queueListModel.append({
                    index: i + 1,
                    taskName: item.task_name || "",
                    scriptLabel: item.script_label || "",
                    state: item.state || "待执行"
                })
            }
        }
        function onLogTextChanged() {
            logTextArea.text = bridge.logText
        }
        function onScheduledTasksChanged(jsonStr) {
            try { root.schedTasks = JSON.parse(jsonStr || "[]") } catch(e) { root.schedTasks = [] }
        }
    }

    function updateSelectionSummary() {
        var count = 0
        for (var i = 0; i < taskListModel.count; i++) {
            if (taskListModel.get(i).enabled) count++
        }
        bridge.selectionSummary = count > 0 ? ("已选择 " + count + " 个任务") : "还没有选择任务"
    }

    function collectSelectedApps() {
        var targets = []
        for (var i = 0; i < taskListModel.count; i++) {
            var item = taskListModel.get(i)
            if (item.enabled) {
                targets.push({ app_key: item.app_key, script_choice: item.script_choice || "" })
            }
        }
        return JSON.stringify(targets)
    }

    function collectPreferences() {
        var prefs = []
        for (var i = 0; i < taskListModel.count; i++) {
            var item = taskListModel.get(i)
            prefs.push({ app_key: item.app_key, enabled: item.enabled, script_choice: item.script_choice || "" })
        }
        return JSON.stringify(prefs)
    }

    // 当前列表的完整 app_key 顺序（用于持久化任务排序）
    function collectFullOrder() {
        var keys = []
        for (var i = 0; i < taskListModel.count; i++) {
            keys.push(taskListModel.get(i).app_key)
        }
        return JSON.stringify(keys)
    }

    // 上移/下移：交换 ListModel 中相邻任务并重排序号，随后持久化顺序
    function moveTask(index, dir) {
        var j = index + dir
        if (j < 0 || j >= taskListModel.count) return
        taskListModel.move(index, j, 1)
        for (var k = 0; k < taskListModel.count; k++) {
            taskListModel.setProperty(k, "orderNum", k + 1)
        }
        bridge.reorderApps(root.collectFullOrder())
    }

    function _pad2(n) { return (n < 10 ? "0" : "") + n }

    // ── Load app list on startup ──
    Component.onCompleted: {
        bridge.loadAppList()
        try {
            var prefs = JSON.parse(bridge.loadPostRunPrefs() || "{}")
            root.emailAfterFinish = !!prefs.email
            root.shutdownAfterFinish = !!prefs.shutdown
        } catch(e) {}
        try { root.schedTasks = JSON.parse(bridge.getScheduledTasks() || "[]") } catch(e) { root.schedTasks = [] }
        // 定时启动的小时/分钟下拉：默认选中当前时间，不用手填
        try {
            for (var h = 0; h < 24; h++) schedHourModel.append({ label: root._pad2(h) })
            for (var m = 0; m < 60; m++) schedMinModel.append({ label: root._pad2(m) })
            var now = new Date()
            schedHourSelect.currentIndex = now.getHours()
            schedMinSelect.currentIndex = now.getMinutes()
        } catch(e) {}
    }

    // ── Layout: left (task config) + right (status/queue/tools) ──
    RowLayout {
        anchors.fill: parent
        spacing: 14

        // ============================================================
        // LEFT COLUMN: Task table, script paths, discovery cache
        // ============================================================
        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 12

            // ── Task selection panel ──────────────────────────────
            HusCard {
                Layout.fillWidth: true
                Layout.fillHeight: true
                title: "任务选择"
                padding: 14

                contentItem: ColumnLayout {
                    spacing: 10

                    // Header row
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8

                        HusText {
                            text: "勾选需要运行的任务，可在下方调整顺序和启动方式。"
                            font.pixelSize: Math.round(13 * root.fs)
                            color: "#6d6257"
                            Layout.fillWidth: true
                        }

                        HusButton { text: "全选"; type: HusButton.Type_Text; onClicked: {
                            for (var i = 0; i < taskListModel.count; i++) taskListModel.setProperty(i, "enabled", true)
                            root.updateSelectionSummary()
                        }}
                        HusButton { text: "清空"; type: HusButton.Type_Text; onClicked: {
                            for (var i = 0; i < taskListModel.count; i++) taskListModel.setProperty(i, "enabled", false)
                            root.updateSelectionSummary()
                        }}
                        HusButton { text: "保存设置"; type: HusButton.Type_Text; onClicked: bridge.saveLaunchPreferences(root.collectPreferences()) }
                        HusButton { text: "重新读取"; type: HusButton.Type_Text; onClicked: bridge.reloadConfig() }
                    }

                    // Table header
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 36
                        color: "#f4ecdf"
                        radius: 10

                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 10
                            anchors.rightMargin: 10
                            spacing: 4

                            HusText { text: "顺序"; Layout.preferredWidth: 96; font.pixelSize: Math.round(12 * root.fs); color: "#6d6257" }
                            HusText { text: "启用"; Layout.preferredWidth: 40; font.pixelSize: Math.round(12 * root.fs); color: "#6d6257" }
                            HusText { text: "任务"; Layout.preferredWidth: 140; font.pixelSize: Math.round(12 * root.fs); color: "#6d6257" }
                            HusText { text: "启动方式"; Layout.preferredWidth: 180; font.pixelSize: Math.round(12 * root.fs); color: "#6d6257" }
                            HusText { text: "类型"; Layout.preferredWidth: 100; font.pixelSize: Math.round(12 * root.fs); color: "#6d6257" }
                            HusText { text: "说明 / 当前设置"; Layout.fillWidth: true; font.pixelSize: Math.round(12 * root.fs); color: "#6d6257" }
                        }
                    }

                    // Task rows
                    ListView {
                        id: taskListView
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true

                        model: ListModel { id: taskListModel }

                        delegate: Rectangle {
                            id: delegateRect
                            width: taskListView.width
                            height: 52
                            color: index % 2 === 0 ? "#fffdf9" : "#fffaf2"
                            radius: 8
                            border.color: "#dbcdbd"
                            border.width: 1

                            // 捕获 delegate 数据，避免被 HusSelect.model 遮蔽
                            property string appKey: model.app_key || ""
                            property bool isEnabled: model.enabled || false
                            property string scriptChoice: model.script_choice || ""
                            property string launcherLabelsJson: model.launcherLabelsJson || "[]"
                            property string launcherValuesJson: model.launcherValuesJson || "[]"

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: 10
                                anchors.rightMargin: 10
                                spacing: 4

                                // Order number + move buttons（自绘，避免 HusButton Type_Text 在委托内透明不可见）
                                RowLayout {
                                    Layout.preferredWidth: 96
                                    spacing: 3

                                    HusText {
                                        Layout.preferredWidth: 26
                                        text: model.orderNum || (index + 1)
                                        font.pixelSize: Math.round(13 * root.fs)
                                        color: "#6d6257"
                                    }

                                    Rectangle {
                                        id: upBtn
                                        Layout.preferredWidth: 30
                                        Layout.preferredHeight: 28
                                        radius: 6
                                        color: upArea.containsMouse ? "#e7dccb" : "#f1e8d8"
                                        border.color: "#d8c9b4"
                                        border.width: 1
                                        enabled: index > 0 && !bridge.isRunning
                                        MouseArea {
                                            id: upArea
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            enabled: parent.enabled
                                            onClicked: root.moveTask(index, -1)
                                        }
                                        HusText {
                                            anchors.centerIn: parent
                                            text: "↑"
                                            font.pixelSize: Math.round(14 * root.fs)
                                            color: (index > 0 && !bridge.isRunning) ? "#5b5147" : "#bcae9c"
                                        }
                                    }

                                    Rectangle {
                                        id: downBtn
                                        Layout.preferredWidth: 30
                                        Layout.preferredHeight: 28
                                        radius: 6
                                        color: downArea.containsMouse ? "#e7dccb" : "#f1e8d8"
                                        border.color: "#d8c9b4"
                                        border.width: 1
                                        enabled: index < taskListModel.count - 1 && !bridge.isRunning
                                        MouseArea {
                                            id: downArea
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            enabled: parent.enabled
                                            onClicked: root.moveTask(index, 1)
                                        }
                                        HusText {
                                            anchors.centerIn: parent
                                            text: "↓"
                                            font.pixelSize: Math.round(14 * root.fs)
                                            color: (index < taskListModel.count - 1 && !bridge.isRunning) ? "#5b5147" : "#bcae9c"
                                        }
                                    }
                                }

                                // Checkbox
                                HusCheckBox {
                                    Layout.preferredWidth: 40
                                    checked: delegateRect.isEnabled
                                    onClicked: {
                                        taskListModel.setProperty(index, "enabled", checked)
                                        root.updateSelectionSummary()
                                    }
                                }

                                // Task name
                                HusText {
                                    Layout.preferredWidth: 140
                                    text: model.taskName || ""
                                    font.pixelSize: Math.round(14 * root.fs)
                                }

                                // Launcher
                                HusSelect {
                                    id: launcherSelect
                                    Layout.preferredWidth: 180
                                    Layout.preferredHeight: 36
                                    clearEnabled: false
                                    textRole: "label"
                                    model: ListModel { id: launcherModel }
                                    currentIndex: {
                                        var sc = delegateRect.scriptChoice
                                        var vals = JSON.parse(delegateRect.launcherValuesJson)
                                        for (var i = 0; i < vals.length; i++) {
                                            if (vals[i] === sc) return i
                                        }
                                        return 0
                                    }
                                    Component.onCompleted: {
                                        launcherModel.clear()
                                        var labels = JSON.parse(delegateRect.launcherLabelsJson)
                                        for (var i = 0; i < labels.length; i++) {
                                            launcherModel.append({label: labels[i]})
                                        }
                                    }
                                    onActivated: {
                                        var vals = JSON.parse(delegateRect.launcherValuesJson)
                                        if (currentIndex >= 0 && currentIndex < vals.length) {
                                            taskListModel.setProperty(index, "script_choice", vals[currentIndex])
                                        }
                                    }
                                }

                                // Type badge
                                HusTag {
                                    Layout.preferredWidth: 100
                                    text: model.taskType || "常规任务"
                                }

                                // Description
                                HusText {
                                    Layout.fillWidth: true
                                    text: model.description || ""
                                    font.pixelSize: Math.round(12 * root.fs)
                                    color: "#6d6257"
                                    elide: Text.ElideRight
                                }
                            }
                        }
                    }
                }
            }
        }

        // ============================================================
        // RIGHT SIDEBAR: Status, queue, tools, results
        // ============================================================
        ColumnLayout {
            Layout.preferredWidth: 360
            Layout.fillHeight: true
            spacing: 12

            // ── Status panel ─────────────────────────────────────
            StatusPanel {
                id: statusPanel
                Layout.fillWidth: true

                running: bridge.isRunning
                summaryText: bridge.selectionSummary
                statusTitleText: bridge.statusTitle
                statusBodyText: bridge.statusBody
                badgeLabel: bridge.statusBadge
                showSpinner: bridge.progressVisible

                onStartClicked: bridge.startAutomation(root.collectSelectedApps(), root.shutdownAfterFinish, root.emailAfterFinish, root.forceRerun)
                onStopClicked: bridge.stopAutomation()
            }

            // ── Force rerun option ──────────────────────────────
            HusCard {
                Layout.fillWidth: true
                title: "运行选项"
                padding: 14

                contentItem: ColumnLayout {
                    spacing: 8

                    HusCheckBox {
                        text: "强制重跑（忽略\"今日已完成\"，所有选中任务重新执行）"
                        checked: root.forceRerun
                        enabled: !bridge.isRunning
                        onClicked: root.forceRerun = checked
                    }
                }
            }

            // ── Post-run options ─────────────────────────────────
            HusCard {
                Layout.fillWidth: true
                title: "完成后操作"
                padding: 14

                contentItem: ColumnLayout {
                    spacing: 8

                    HusCheckBox {
                        text: "完成后发送每日进度邮件（失败不影响自动关机）"
                        checked: root.emailAfterFinish
                        enabled: !bridge.isRunning
                        onClicked: {
                            root.emailAfterFinish = checked
                            bridge.savePostRunPrefs(root.emailAfterFinish, root.shutdownAfterFinish)
                        }
                    }

                    HusText {
                        text: bridge.emailConfigured
                            ? "邮件配置已就绪（如需修改授权码，点配置邮件覆盖）"
                            : "邮件未配置：请点击下方配置邮件填写 SMTP 账号"
                        font.pixelSize: Math.round(12 * root.fs)
                        color: bridge.emailConfigured ? "#2f7d4a" : "#b66f1f"
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }

                    HusButton {
                        text: "配置邮件账号"
                        type: HusButton.Type_Outlined
                        Layout.fillWidth: true
                        enabled: !bridge.isRunning && bridge.dpapiAvailable
                        onClicked: root.openEmailConfigDialog()
                    }

                    HusText {
                        visible: !bridge.dpapiAvailable
                        text: "⚠ 当前环境无 win32crypt（DPAPI），无法安全保存密码；请安装 pywin32 后重启 UI。"
                        font.pixelSize: Math.round(12 * root.fs)
                        color: "#b04b44"
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }

                    HusCheckBox {
                        text: "一条龙：完成后无论成败都自动关机（60 秒延迟，可取消）"
                        checked: root.shutdownAfterFinish
                        enabled: !bridge.isRunning
                        onClicked: {
                            root.shutdownAfterFinish = checked
                            bridge.savePostRunPrefs(root.emailAfterFinish, root.shutdownAfterFinish)
                        }
                    }

                    HusButton {
                        text: "取消计划中的关机"
                        type: HusButton.Type_Outlined
                        Layout.fillWidth: true
                        visible: bridge.shutdownScheduled
                        onClicked: bridge.cancelShutdown()
                    }

                    HusText {
                        text: "勾选邮件后，任务结束时会尝试发送本轮报告 + 今日进度；勾选关机后无论邮件是否发送成功，60 秒后都会自动关机。运行中无法修改这两项。"
                        font.pixelSize: Math.round(12 * root.fs)
                        color: "#6d6257"
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                }
            }

            // ── 定时启动 ─────────────────────────────────────────
            HusCard {
                Layout.fillWidth: true
                title: "⏰ 定时启动"
                padding: 14

                contentItem: ColumnLayout {
                    spacing: 8

                    HusText {
                        text: "每天到设定时间自动启动所选任务（GUI 需保持运行）。邮件 / 自动关机跟随上方「完成后操作」的勾选。"
                        font.pixelSize: Math.round(12 * root.fs)
                        color: "#6d6257"
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }

                    RowLayout {
                        spacing: 8
                        HusText { text: "任务:"; font.pixelSize: Math.round(13 * root.fs) }
                        HusSelect {
                            id: schedTaskSelect
                            clearEnabled: false
                            model: ListModel { id: schedTaskModel }
                            currentIndex: -1
                            Layout.preferredWidth: Math.round(220 * root.fs)
                        }
                        HusText { text: "时间:"; font.pixelSize: Math.round(13 * root.fs) }
                        HusSelect {
                            id: schedHourSelect
                            clearEnabled: false
                            textRole: "label"
                            model: ListModel { id: schedHourModel }
                            Layout.preferredWidth: Math.round(70 * root.fs)
                        }
                        HusText { text: ":"; font.pixelSize: Math.round(13 * root.fs) }
                        HusSelect {
                            id: schedMinSelect
                            clearEnabled: false
                            textRole: "label"
                            model: ListModel { id: schedMinModel }
                            Layout.preferredWidth: Math.round(70 * root.fs)
                        }
                        HusButton {
                            text: "添加"
                            type: HusButton.Type_Primary
                            onClicked: {
                                var idx = schedTaskSelect.currentIndex
                                if (idx < 0) {
                                    schedErrorText.text = "请先选择要定时启动的任务"
                                    return
                                }
                                var app = root.appList[idx] || {}
                                if (!app.app_key) return
                                var hh = root._pad2(schedHourSelect.currentIndex)
                                var mm = root._pad2(schedMinSelect.currentIndex)
                                var okFlag = bridge.addScheduledTask(JSON.stringify({
                                    name: (app.display_name || app.app_key) + " @ " + hh + ":" + mm,
                                    time: hh + ":" + mm,
                                    apps: [{ app_key: app.app_key, script_choice: app.script_choice || null }],
                                    enabled: true,
                                    skip_if_done_today: schedSkipDoneCheck.checked
                                }))
                                if (okFlag) { schedErrorText.text = "" }
                            }
                        }
                        Item { Layout.fillWidth: true }
                    }

                    RowLayout {
                        spacing: 8
                        HusCheckBox {
                            id: schedSkipDoneCheck
                            text: "仅补跑今日未完成（已成功则跳过）"
                        }
                        Item { Layout.fillWidth: true }
                    }

                    Repeater {
                        model: root.schedTasks
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8
                            HusText {
                                text: modelData.time
                                font.pixelSize: Math.round(15 * root.fs)
                                font.bold: true
                                color: modelData.enabled ? "#2c241d" : "#b0a898"
                                Layout.preferredWidth: Math.round(64 * root.fs)
                            }
                            HusText {
                                text: modelData.name || ""
                                font.pixelSize: Math.round(13 * root.fs)
                                color: modelData.enabled ? "#2c241d" : "#8a7a68"
                                Layout.fillWidth: true
                                elide: Text.ElideRight
                            }
                            HusText {
                                visible: modelData.skip_if_done_today === true
                                text: "仅补跑"
                                font.pixelSize: Math.round(11 * root.fs)
                                color: "#2f7d4a"
                            }
                            HusText {
                                visible: modelData.last_fired_date === Qt.formatDate(new Date(), "yyyy-MM-dd")
                                text: "今日已触发"
                                font.pixelSize: Math.round(11 * root.fs)
                                color: "#2f7d4a"
                            }
                            HusCheckBox {
                                text: "启用"
                                checked: modelData.enabled
                                onClicked: bridge.toggleScheduledTask(modelData.id, checked)
                            }
                            HusButton {
                                text: "删除"
                                type: HusButton.Type_Outlined
                                onClicked: bridge.removeScheduledTask(modelData.id)
                            }
                        }
                    }

                    HusText {
                        visible: root.schedTasks.length === 0
                        text: "暂无定时任务"
                        font.pixelSize: Math.round(12 * root.fs)
                        color: "#8a7a68"
                    }

                    HusText {
                        id: schedErrorText
                        text: ""
                        visible: text !== ""
                        font.pixelSize: Math.round(12 * root.fs)
                        color: "#b04b44"
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                }
            }

            // ── Queue order panel ────────────────────────────────
            HusCard {
                Layout.fillWidth: true
                Layout.preferredHeight: 180
                title: "本轮顺序"
                padding: 12

                contentItem: ListView {
                    id: queueListView
                    clip: true
                    spacing: 6

                    model: ListModel { id: queueListModel }

                    delegate: QueueItem {
                        width: queueListView.width
                        indexNumber: model.index
                        taskName: model.taskName || ""
                        scriptLabel: model.scriptLabel || ""
                        stateText: model.state || "待执行"
                        stateColor: {
                            switch (model.state) {
                                case "运行中": return "#295d86"
                                case "已完成": return "#2f7d4a"
                                case "失败":   return "#b04b44"
                                case "已停止": return "#b66f1f"
                                default:      return "#6d6257"
                            }
                        }
                        stateBg: {
                            switch (model.state) {
                                case "运行中": return "#e9f3fb"
                                case "已完成": return "#dff1e4"
                                case "失败":   return "#f7dfdd"
                                case "已停止": return "#faebd7"
                                default:      return "#f4ecdf"
                            }
                        }
                    }
                }
            }

            // ── Tools panel ───────────────────────────────────────
            HusCard {
                Layout.fillWidth: true
                title: "工具箱"

                contentItem: ColumnLayout {
                    spacing: 8
                    HusButton { text: "打开配置文件"; type: HusButton.Type_Outlined; Layout.fillWidth: true; onClicked: bridge.openConfigFile() }
                    HusButton { text: "打开日志目录"; type: HusButton.Type_Outlined; Layout.fillWidth: true; onClicked: bridge.openLogDir() }
                }
            }

            // ── Log panel ────────────────────────────────────────
            HusCard {
                Layout.fillWidth: true
                Layout.fillHeight: true
                title: "运行日志"
                padding: 8

                contentItem: ScrollView {
                    clip: true

                    TextArea {
                        id: logTextArea
                        readOnly: true
                        wrapMode: TextArea.Wrap
                        font.family: "Consolas"
                        font.pixelSize: Math.round(12 * root.fs)
                        color: "#2c241d"
                        background: Rectangle { color: "#fffdf9" }
                        text: bridge.logText
                    }
                }
            }
        }
    }

    // ── Email config modal dialog ────────────────────────────
    Item {
        id: emailDialogBackdrop
        anchors.fill: parent
        visible: root.emailDialogOpen
        z: 999
        // 阻止点击穿透到下层控件
        MouseArea { anchors.fill: parent; onWheel: function(w) { w.accepted = true } }

        Rectangle {
            anchors.fill: parent
            color: "#000000"
            opacity: 0.45
        }

        Rectangle {
            id: emailDialogCard
            anchors.centerIn: parent
            width: Math.min(parent.width - 48, 540)
            implicitHeight: emailDialogCol.implicitHeight + 32
            color: "#fffdf9"
            radius: 12
            border.color: "#dbcdbd"
            border.width: 1

            ColumnLayout {
                id: emailDialogCol
                anchors.fill: parent
                anchors.margins: 16
                spacing: 10

                HusText {
                    text: "配置邮件账号"
                    font.pixelSize: Math.round(18 * root.fs)
                    font.bold: true
                }

                HusText {
                    text: "密码经 Windows DPAPI 加密后落盘到 app_config.json，跨用户/跨机器不可读。"
                    font.pixelSize: Math.round(11 * root.fs)
                    color: "#6d6257"
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }

                // SMTP host
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    HusText { text: "SMTP 主机"; Layout.preferredWidth: 96 }
                    HusInput {
                        id: smtpHostField
                        Layout.fillWidth: true
                        text: root.emailDraft.smtp_host || ""
                        placeholderText: "smtp.qq.com"
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    HusText { text: "端口"; Layout.preferredWidth: 96 }
                    HusInput {
                        id: smtpPortField
                        Layout.preferredWidth: 120
                        text: String(root.emailDraft.smtp_port || 465)
                    }
                    HusCheckBox {
                        id: useSslField
                        text: "SSL/TLS"
                        checked: root.emailDraft.use_ssl !== false
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    HusText { text: "发件人"; Layout.preferredWidth: 96 }
                    HusInput {
                        id: senderField
                        Layout.fillWidth: true
                        text: root.emailDraft.sender || ""
                        placeholderText: "your@qq.com"
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    HusText { text: "授权码"; Layout.preferredWidth: 96 }
                    CredentialInput {
                        id: passwordField
                        Layout.fillWidth: true
                        placeholderText: root.emailDraft.has_password
                            ? "（已留空时沿用旧授权码，重新输入则覆盖）"
                            : "SMTP 授权码（非登录密码）"
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    HusText { text: "收件人"; Layout.preferredWidth: 96 }
                    HusInput {
                        id: recipientsField
                        Layout.fillWidth: true
                        text: root.emailDraft.recipients || ""
                        placeholderText: "a@example.com, b@example.com"
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    HusText { text: "主题前缀"; Layout.preferredWidth: 96 }
                    HusInput {
                        id: subjectPrefixField
                        Layout.fillWidth: true
                        text: root.emailDraft.subject_prefix || "[GameAuto]"
                    }
                }

                HusText {
                    text: root.emailDialogStatus
                    font.pixelSize: Math.round(11 * root.fs)
                    color: root.emailDialogStatusColor
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    Item { Layout.fillWidth: true }
                    HusButton {
                        text: "取消"
                        type: HusButton.Type_Outlined
                        onClicked: root.emailDialogOpen = false
                    }
                    HusButton {
                        text: "保存"
                        type: HusButton.Type_Primary
                        onClicked: root.submitEmailConfig()
                    }
                }
            }
        }
    }
}
