import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import QtQuick.Dialogs
import HuskarUI.Basic

// 磁盘管理：游戏本体 / 第三方脚本 / 本工具 data 三组占用统计。
// 打开即显上次缓存（getDiskManagerData），点扫描后台线程逐目标扫，
// 行级结果经 onDiskScanChanged 渐进刷新（bridge disk_scan 事件）。
Item {
    id: root
    objectName: "diskManagerRoot"
    readonly property real fs: Application.window ? Application.window.fontScale : 1

    property var scanData: ({ items: [], scanned_at: "", overview: [] })
    // 扫描中 = starting/item 阶段；done/idle/error 回空闲
    property bool scanning: false
    property var scanProgress: ({ done: 0, total: 0 })

    readonly property var sections: [
        { key: "games",   label: "游戏本体" },
        { key: "scripts", label: "第三方脚本" },
        { key: "data",    label: "本工具数据" }
    ]

    Component.onCompleted: reloadData()
    onVisibleChanged: if (visible) reloadData()

    Connections {
        target: bridge
        function onDiskScanChanged(json) {
            try {
                var d = JSON.parse(json)
                switch (d.phase) {
                    case "starting":
                        root.scanning = true
                        root.scanProgress = { done: 0, total: 0 }
                        break
                    case "item":
                        root.scanning = true
                        root.scanProgress = { done: d.done, total: d.total }
                        root.applyItem(d.item)
                        break
                    case "done":
                        root.scanning = false
                        root.scanData = { items: d.items || [], scanned_at: d.scanned_at || "",
                                          overview: d.overview || [] }
                        break
                    case "idle":
                    case "error":
                        root.scanning = false
                        break
                }
            } catch(e) { root.scanning = false }
        }
    }

    function reloadData() {
        try {
            root.scanData = JSON.parse(bridge.getDiskManagerData())
        } catch(e) {
            root.scanData = { items: [], scanned_at: "", overview: [] }
        }
    }

    function applyItem(item) {
        if (!item || !item.key) return
        var items = root.scanData.items || []
        for (var i = 0; i < items.length; i++) {
            if (items[i].key === item.key) {
                items[i].size = item.size
                items[i].exists = item.exists
                root.scanData.items = items.slice()  // 触发绑定刷新
                return
            }
        }
    }

    function itemsFor(groupKey) {
        return (root.scanData.items || []).filter(function(it) { return it.group === groupKey })
    }

    function groupMax(groupKey) {
        var m = 0
        var arr = itemsFor(groupKey)
        for (var i = 0; i < arr.length; i++) m = Math.max(m, Number(arr[i].size || 0))
        return m
    }

    function groupTotal(groupKey) {
        var s = 0
        var arr = itemsFor(groupKey)
        for (var i = 0; i < arr.length; i++) s += Number(arr[i].size || 0)
        return s
    }

    function grandTotal() {
        var s = 0
        var arr = root.scanData.items || []
        for (var i = 0; i < arr.length; i++) s += Number(arr[i].size || 0)
        return s
    }

    function fmtSize(bytes) {
        var n = Number(bytes || 0)
        if (n >= 1073741824) return (n / 1073741824).toFixed(2) + " GB"
        if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB"
        if (n >= 1024) return (n / 1024).toFixed(1) + " KB"
        return n + " B"
    }

    function filePathFromUrl(url) {
        var s = url.toString()
        if (s.startsWith("file:///")) s = s.substring(8)
        return decodeURIComponent(s)
    }

    FolderDialog {
        id: overrideDialog
        property string gameKey: ""
        title: "选择该游戏的扫描目录"
        onAccepted: {
            bridge.setDiskScanOverride(gameKey, root.filePathFromUrl(selectedFolder))
            root.reloadData()
        }
    }

    ScrollView {
        anchors.fill: parent
        clip: true

        ColumnLayout {
            width: parent.width
            spacing: 14

            // ── 头部：说明 + 扫描控制 + 总览 ──
            HusCard {
                Layout.fillWidth: true
                padding: 20

                contentItem: ColumnLayout {
                    spacing: 10

                    RowLayout {
                        spacing: 10
                        Layout.fillWidth: true

                        HusText {
                            text: "磁盘管理"
                            font.pixelSize: Math.round(22 * root.fs)
                            font.bold: true
                        }
                        Item { Layout.fillWidth: true }
                        HusText {
                            visible: root.scanData.scanned_at !== ""
                            text: "上次扫描: " + root.scanData.scanned_at
                            font.pixelSize: Math.round(12 * root.fs)
                            color: "#6d6257"
                        }
                        HusButton {
                            text: root.scanning ? "扫描中..." : "开始扫描"
                            type: HusButton.Type_Primary
                            enabled: !root.scanning
                            onClicked: bridge.startDiskScan()
                        }
                        HusButton {
                            visible: root.scanning
                            text: "停止"
                            type: HusButton.Type_Outlined
                            onClicked: bridge.cancelDiskScan()
                        }
                    }

                    HusText {
                        text: "统计各游戏本体、第三方脚本与本工具数据的磁盘占用（只读统计，不删除任何文件）。" +
                              "游戏目录按 exe 自动推导，扫小了/扫大了点对应行的「改目录」。"
                        font.pixelSize: Math.round(13 * root.fs)
                        color: "#6d6257"
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }

                    // 扫描进度条
                    ColumnLayout {
                        visible: root.scanning
                        spacing: 4
                        Layout.fillWidth: true

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: Math.round(6 * root.fs)
                            radius: 3
                            color: "#e8e0d5"

                            Rectangle {
                                width: parent.width * (root.scanProgress.total > 0
                                       ? root.scanProgress.done / root.scanProgress.total : 0)
                                height: parent.height
                                radius: 3
                                color: "#2f7d4a"
                            }
                        }
                        HusText {
                            text: "已扫描 " + root.scanProgress.done + " / " + root.scanProgress.total + " 个目标"
                            font.pixelSize: Math.round(12 * root.fs)
                            color: "#6d6257"
                        }
                    }

                    // 汇总 + 盘符剩余空间
                    RowLayout {
                        spacing: 16
                        Layout.fillWidth: true

                        HusText {
                            text: "合计占用 " + root.fmtSize(root.grandTotal())
                            font.pixelSize: Math.round(15 * root.fs)
                            font.bold: true
                        }
                        Repeater {
                            model: root.scanData.overview || []
                            delegate: HusText {
                                required property var modelData
                                text: modelData.drive + " 剩余 " + root.fmtSize(modelData.free) +
                                      " / " + root.fmtSize(modelData.total)
                                font.pixelSize: Math.round(12 * root.fs)
                                color: "#5a4d3f"
                            }
                        }
                        Item { Layout.fillWidth: true }
                    }
                }
            }

            // ── 三组清单 ──
            Repeater {
                model: root.sections

                delegate: HusCard {
                    id: groupCard
                    required property var modelData
                    readonly property string groupKey: modelData.key
                    readonly property real groupMax: root.groupMax(groupKey)
                    Layout.fillWidth: true
                    padding: 14

                    contentItem: ColumnLayout {
                        spacing: 8

                        RowLayout {
                            spacing: 8
                            Layout.fillWidth: true

                            HusText {
                                text: groupCard.modelData.label
                                font.pixelSize: Math.round(15 * root.fs)
                                font.bold: true
                            }
                            HusText {
                                text: root.fmtSize(root.groupTotal(groupCard.groupKey))
                                font.pixelSize: Math.round(13 * root.fs)
                                color: "#6d6257"
                            }
                            Item { Layout.fillWidth: true }
                        }

                        Repeater {
                            model: root.itemsFor(groupCard.groupKey)

                            delegate: ColumnLayout {
                                id: rowItem
                                required property var modelData
                                Layout.fillWidth: true
                                spacing: 3

                                RowLayout {
                                    spacing: 8
                                    Layout.fillWidth: true

                                    HusText {
                                        text: rowItem.modelData.name +
                                              (rowItem.modelData.group === "games" && !rowItem.modelData.exists
                                               ? "（路径待发现，实机运行一次后回填）" : "")
                                        font.pixelSize: Math.round(13 * root.fs)
                                        color: rowItem.modelData.exists ? "#3d332a" : "#8a7a68"
                                        elide: Text.ElideRight
                                        Layout.maximumWidth: Math.round(220 * root.fs)
                                    }
                                    Rectangle {
                                        Layout.fillWidth: true
                                        Layout.preferredHeight: Math.round(8 * root.fs)
                                        radius: 4
                                        color: "#efe8dd"

                                        Rectangle {
                                            width: parent.width * (groupCard.groupMax > 0
                                                   ? Number(rowItem.modelData.size || 0) / groupCard.groupMax : 0)
                                            height: parent.height
                                            radius: 4
                                            color: rowItem.modelData.exists ? "#4a7db5" : "#c4b8a8"
                                        }
                                    }
                                    HusText {
                                        text: rowItem.modelData.exists ? root.fmtSize(rowItem.modelData.size) : "—"
                                        font.pixelSize: Math.round(13 * root.fs)
                                        font.bold: true
                                        Layout.preferredWidth: Math.round(90 * root.fs)
                                        horizontalAlignment: Text.AlignRight
                                    }
                                    HusButton {
                                        visible: rowItem.modelData.path !== ""
                                        text: "打开"
                                        type: HusButton.Type_Outlined
                                        onClicked: Qt.openUrlExternally(
                                            "file:///" + rowItem.modelData.path.replace(/\\/g, "/"))
                                    }
                                    HusButton {
                                        visible: rowItem.modelData.group === "games"
                                        text: "改目录"
                                        type: HusButton.Type_Outlined
                                        enabled: !root.scanning
                                        onClicked: {
                                            overrideDialog.gameKey = rowItem.modelData.key
                                            overrideDialog.open()
                                        }
                                    }
                                }

                                HusText {
                                    text: rowItem.modelData.path || "（散文件，无目录）"
                                    font.pixelSize: Math.round(11 * root.fs)
                                    color: "#8a7a68"
                                    elide: Text.ElideMiddle
                                    Layout.fillWidth: true
                                    leftPadding: Math.round(4 * root.fs)
                                }
                            }
                        }
                    }
                }
            }

            Item { Layout.preferredHeight: 4 }
        }
    }
}
