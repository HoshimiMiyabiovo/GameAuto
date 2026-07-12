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

    // ── Parse bridge JSON when it changes ──
    Connections {
        target: bridge
        function onAppListChanged(jsonStr) {
            try { root.appList = JSON.parse(jsonStr || "[]") } catch(e) { root.appList = [] }
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

    // ── Load app list on startup ──
    Component.onCompleted: bridge.loadAppList()

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

                            HusText { text: "顺序"; Layout.preferredWidth: 60; font.pixelSize: Math.round(12 * root.fs); color: "#6d6257" }
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

                                // Order number
                                HusText {
                                    Layout.preferredWidth: 60
                                    text: model.orderNum || (index + 1)
                                    font.pixelSize: Math.round(13 * root.fs)
                                    color: "#6d6257"
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

                onStartClicked: bridge.startAutomation(root.collectSelectedApps())
                onStopClicked: bridge.stopAutomation()
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
}
