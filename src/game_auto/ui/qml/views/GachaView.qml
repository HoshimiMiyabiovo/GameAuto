import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import QtQuick.Dialogs
import HuskarUI.Basic
import "../components"

// HoYo Gacha Analysis + MiHoYo Sign view
Item {
    id: root
    readonly property real fs: Application.window ? Application.window.fontScale : 1

    property var accounts: []
    property var analysis: ({})
    property string selectedGameLabel: "原神"
    property string selectedUid: ""

    // 页面激活标志：仅在页面可见时创建 delegate，避免 width=0 时创建后不更新
    readonly property bool pageActive: bridge.currentView === "gacha"

    Component.onCompleted: {
        bridge.loadGachaAccounts(root.selectedGameLabel)
        bridge.loadCredentials()
    }

    Connections {
        target: bridge
        function onGachaAccountsChanged(jsonStr) {
            try { root.accounts = JSON.parse(jsonStr || "[]") } catch(e) { root.accounts = [] }
            uidSelectModel.clear()
            for (var i = 0; i < root.accounts.length; i++) {
                var acc = root.accounts[i]
                uidSelectModel.append({
                    label: acc.uid + " (" + acc.total_records + "条)",
                    uid: acc.uid
                })
            }
            if (root.accounts.length > 0 && !root.selectedUid) {
                root.selectedUid = root.accounts[0].uid
                uidSelect.currentIndex = 0
                bridge.loadGachaAnalysis(root.selectedGameLabel, root.selectedUid)
            }
        }
        function onGachaAnalysisChanged(jsonStr) {
            try { root.analysis = JSON.parse(jsonStr || "{}") } catch(e) { root.analysis = ({}) }
            root.updateDisplay()
        }
        function onCredentialsChanged(jsonStr) {
            try {
                var creds = JSON.parse(jsonStr || "{}")
                var mihoyo = creds.mihoyo_sign || {}
                if (mihoyo.cookie) mihoyoCookieInput.text = mihoyo.cookie
                var browser = String(mihoyo.browser || "edge")
                for (var bi = 0; bi < mihoyoBrowserSelect.model.count; bi++) {
                    if (mihoyoBrowserSelect.model.get(bi).value === browser) {
                        mihoyoBrowserSelect.currentIndex = bi
                        break
                    }
                }
                genshinSignCheck.checked = !!(mihoyo.enabled_games && mihoyo.enabled_games.genshin)
                honkaiSrSignCheck.checked = !!(mihoyo.enabled_games && mihoyo.enabled_games.honkai_sr)
                zzzSignCheck.checked = !!(mihoyo.enabled_games && mihoyo.enabled_games.zzz)
                mihoyoStartupCheck.checked = !!mihoyo.startup_sign
                var dev = mihoyo.device || null
                if (dev && (dev.fp || dev.deviceFingerprint)) {
                    var parts = ["已绑定"]
                    parts.push(dev.fp ? ("fp " + dev.fp.substring(0, 8) + "…") : "fp 待生成")
                    if (dev.deviceModel) parts.push(dev.deviceModel)
                    if (dev.registered) parts.push("已注册设备")
                    mihoyoDeviceStatus.text = parts.join(" · ")
                    mihoyoDeviceStatus.color = "#16a34a"
                } else {
                    mihoyoDeviceStatus.text = "未绑定"
                    mihoyoDeviceStatus.color = "#71717a"
                }
            } catch(e) { /* ignore */ }
        }
    }

    function updateDisplay() {
        var ov = root.analysis.overview || {}
        var pity = root.analysis.pity || {}
        totalCard.value = ov.total_pulls || "-"
        fiveStarCard.value = ov.high_rarity_count || "-"
        avgPityCard.value = ov.high_rarity_rate || "-"
        rateCard.value = ov.latest_import ? ov.latest_import.substring(0, 10) : "-"
        totalCard.note = ov.earliest_time ? ("从 " + ov.earliest_time.substring(0, 10)) : "等待导入"
        fiveStarCard.note = ov.latest_time ? ("至 " + ov.latest_time.substring(0, 10)) : "等待导入"
        avgPityCard.note = pity.avg_pity != null ? ("平均保底 " + pity.avg_pity) : "等待导入"
        rateCard.note = pity.current_pity != null ? ("当前 " + pity.current_pity + " 抽") : "等待导入"
        // UP 歪率 / 大保底 / 保底预测（gacha.luck_analysis headline）
        var luck = root.analysis.luck || {}
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
                : "等待导入"
        }
    }

    FileDialog {
        id: fileDialog
        title: "选择 UIGF JSON 文件"
        nameFilters: ["JSON 文件 (*.json)", "所有文件 (*)"]
        onAccepted: {
            var path = fileDialog.selectedFile.toString().replace("file:///", "")
            gachaPathInput.text = path
            bridge.importGachaFile(path)
        }
    }

    FileDialog {
        id: hgDbDialog
        title: "选择 HoYo.Gacha 数据库文件（exe 运行目录下的 HoYo.Gacha.v1.db）"
        nameFilters: ["SQLite 数据库 (*.db)", "所有文件 (*)"]
        onAccepted: {
            var raw = hgDbDialog.selectedFile.toString().replace(/^file:\/\/\//, "")
            var path = decodeURIComponent(raw)
            bridge.importHoyogachaDb(path, root.selectedGameLabel)
        }
    }

    ScrollView {
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth

        ColumnLayout {
            width: parent.width
            spacing: 14

            // HoYo Gacha section
            HusCard {
                Layout.fillWidth: true
                title: "🎲 HoYo 抽卡分析"
                padding: 16

                contentItem: ColumnLayout {
                    spacing: 12

                    // File input row
                    RowLayout {
                        spacing: 10
                        HusInput {
                            id: gachaPathInput
                            Layout.fillWidth: true
                            placeholderText: "选择 UIGF JSON 文件..."
                            readOnly: true
                        }
                        HusButton {
                            text: "浏览"
                            type: HusButton.Type_Outlined
                            onClicked: fileDialog.open()
                        }
                        HusButton {
                            text: "分析"
                            type: HusButton.Type_Primary
                            onClicked: {
                                if (gachaPathInput.text.length > 0) {
                                    bridge.importGachaFile(gachaPathInput.text)
                                }
                            }
                        }
                    }

                    // HoYo.Gacha 本地库直读（免导出 JSON）
                    RowLayout {
                        spacing: 10
                        HusText {
                            text: "已有 HoYo.Gacha？直接读它的数据库，三端（原神/星铁/绝区零）一次全进："
                            font.pixelSize: Math.round(12 * root.fs)
                            color: "#71717a"
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                        }
                        HusButton {
                            text: "选择 HoYo.Gacha 数据库"
                            type: HusButton.Type_Outlined
                            onClicked: hgDbDialog.open()
                        }
                    }

                    // Game selector
                    RowLayout {
                        spacing: 8
                        HusText { text: "游戏:"; font.pixelSize: Math.round(14 * root.fs) }
                        HusSelect {
                            id: gameSelect
                            clearEnabled: false
                            model: ListModel {
                                ListElement { label: "原神" }
                                ListElement { label: "星穹铁道" }
                                ListElement { label: "绝区零" }
                            }
                            currentIndex: 0
                            onActivated: {
                                root.selectedGameLabel = gameSelect.model.get(currentIndex).label
                                root.selectedUid = ""
                                bridge.loadGachaAccounts(root.selectedGameLabel)
                            }
                        }
                        HusText { text: "UID:"; font.pixelSize: Math.round(14 * root.fs) }
                        HusSelect {
                            id: uidSelect
                            clearEnabled: false
                            model: ListModel { id: uidSelectModel }
                            currentIndex: -1
                            onActivated: {
                                if (currentIndex >= 0 && currentIndex < uidSelectModel.count) {
                                    root.selectedUid = uidSelectModel.get(currentIndex).uid
                                    bridge.loadGachaAnalysis(root.selectedGameLabel, root.selectedUid)
                                }
                            }
                        }
                        HusButton {
                            text: "刷新列表"
                            type: HusButton.Type_Text
                            onClicked: bridge.loadGachaAccounts(root.selectedGameLabel)
                        }
                    }

                    HusText {
                        text: root.accounts.length === 0
                              ? "暂无已导入的抽卡记录，请选择 UIGF JSON 文件导入。"
                              : "已加载 " + root.accounts.length + " 个账号，选择 UID 查看分析。"
                        font.pixelSize: Math.round(13 * root.fs)
                        color: "#71717a"
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }

                    // Overview KPI 大卡片行
                    RowLayout {
                        spacing: 14
                        Layout.fillWidth: true
                        visible: !!root.analysis.overview

                        GachaMetricCard { id: totalCard; title: "总抽数"; value: "-"; note: "等待导入"; Layout.fillWidth: true }
                        GachaMetricCard { id: fiveStarCard; title: "5★ 数"; value: "-"; note: "等待导入"; Layout.fillWidth: true }
                        GachaMetricCard { id: avgPityCard; title: "出货率"; value: "-"; note: "等待导入"; Layout.fillWidth: true }
                        GachaMetricCard { id: rateCard; title: "最近导入"; value: "-"; note: "等待导入"; Layout.fillWidth: true }
                        GachaMetricCard { id: luckCard; title: "UP 歪率"; value: "-"; note: "等待导入"; Layout.fillWidth: true }
                    }

                    HusEmpty {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 160
                        visible: !root.analysis.overview
                        description: "请先导入抽卡数据或选择 UID"
                    }

                    // ── 卡池详情区：左卡池列表 + 右详情 ──
                    HusCard {
                        Layout.fillWidth: true
                        visible: (root.analysis.pools || []).length > 0
                        padding: 18
                        title: "📊 卡池详情"

                        contentItem: RowLayout {
                            spacing: 14

                            // ── 左侧：卡池列表 ──
                            ColumnLayout {
                                Layout.preferredWidth: 220
                                Layout.fillHeight: true
                                spacing: 8

                                // 背景板：池子少时列表内容不满，浅底让空白区有边界感
                                Rectangle {
                                    Layout.fillWidth: true
                                    Layout.fillHeight: true
                                    color: "#fafafa"
                                    radius: 8
                                    border.color: "#e4e4e7"
                                    border.width: 1

                                ListView {
                                    id: hoyoPoolListView
                                    anchors.fill: parent
                                    anchors.margins: 4
                                    clip: true
                                    spacing: 4
                                    model: root.analysis.pools || []
                                    currentIndex: hoyoPoolListView.selectedPool

                                    property int selectedPool: 0
                                    property var currentPool: (root.analysis.pools || [])[hoyoPoolListView.selectedPool] || ({})

                                    delegate: Rectangle {
                                        width: hoyoPoolListView.width
                                        height: 60
                                        required property var modelData
                                        required property int index
                                        color: hoyoPoolListView.selectedPool === index ? "#ecfdf5" : (poolHover.containsMouse ? "#fafafa" : "transparent")
                                        radius: 6
                                        border.width: hoyoPoolListView.selectedPool === index ? 2 : 0
                                        border.color: "#10b981"

                                        MouseArea {
                                            id: poolHover
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            cursorShape: Qt.PointingHandCursor
                                            onClicked: hoyoPoolListView.selectedPool = index
                                        }

                                        ColumnLayout {
                                            anchors.fill: parent
                                            anchors.margins: 8
                                            spacing: 2

                                            HusText {
                                                text: modelData.pool_name || "?"
                                                font.pixelSize: Math.round(14 * root.fs)
                                                font.bold: hoyoPoolListView.selectedPool === index
                                                color: "#27272a"
                                                Layout.fillWidth: true
                                                elide: Text.ElideRight
                                                ToolTip.text: modelData.pool_name || ""
                                                ToolTip.visible: poolNameHover.containsMouse && (modelData.pool_name || "").length > 10
                                                ToolTip.delay: 200
                                                MouseArea {
                                                    id: poolNameHover
                                                    anchors.fill: parent
                                                    hoverEnabled: true
                                                    cursorShape: Qt.PointingHandCursor
                                                    // 2026-11-17 修复：这个 MouseArea 只为 tooltip 悬停服务，
                                                    // 默认 acceptedButtons 会吞掉点击，导致点池名（卡片上半截）
                                                    // 无法切换卡池、只有点下半截抽数行才生效（用户实测）。
                                                    // NoButton = 不接任何按键事件，点击穿透到底层 poolHover，
                                                    // containsMouse 悬停跟踪不受影响。
                                                    acceptedButtons: Qt.NoButton
                                                }
                                            }
                                            RowLayout {
                                                spacing: 6
                                                HusText {
                                                    text: (modelData.total_pulls || 0) + " 抽"
                                                    font.pixelSize: Math.round(12 * root.fs)
                                                    color: "#71717a"
                                                }
                                                HusText {
                                                    text: "· 5★: " + (modelData.high_rarity_count || 0)
                                                    font.pixelSize: Math.round(12 * root.fs)
                                                    color: "#dc2626"
                                                }
                                            }
                                        }
                                    }
                                }   // ListView
                                }   // 背景 Rectangle

                                HusText {
                                    text: {
                                        var n = (root.analysis.pools || []).length
                                        return n > 0 ? ("共 " + n + " 个卡池，点击查看详情") : "暂无卡池数据"
                                    }
                                    font.pixelSize: Math.round(11 * root.fs)
                                    color: "#a1a1aa"
                                    wrapMode: Text.Wrap
                                    Layout.fillWidth: true
                                }
                            }

                            // ── 右侧：详情 ──
                            ColumnLayout {
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                spacing: 12

                                // 卡池标题 + 总抽数
                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 12

                                    HusText {
                                        text: hoyoPoolListView.currentPool.pool_name || "—"
                                        font.pixelSize: Math.round(22 * root.fs)
                                        font.bold: true
                                        color: "#27272a"
                                        Layout.fillWidth: true
                                        elide: Text.ElideRight
                                        ToolTip.text: hoyoPoolListView.currentPool.pool_name || ""
                                        ToolTip.visible: titleHover.containsMouse && (hoyoPoolListView.currentPool.pool_name || "").length > 12
                                        ToolTip.delay: 300
                                        MouseArea {
                                            id: titleHover
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            cursorShape: Qt.IBeamCursor
                                        }
                                    }
                                    HusText {
                                        text: (hoyoPoolListView.currentPool.total_pulls || 0)
                                        font.pixelSize: Math.round(36 * root.fs)
                                        font.bold: true
                                        color: "#16a34a"
                                    }
                                    HusText {
                                        text: "抽"
                                        font.pixelSize: Math.round(14 * root.fs)
                                        color: "#71717a"
                                        Layout.alignment: Qt.AlignBottom
                                        Layout.bottomMargin: 6
                                    }
                                }

                                // 关键指标横向卡片
                                Rectangle {
                                    Layout.fillWidth: true
                                    height: 70
                                    color: "#ffffff"
                                    radius: 8
                                    border.color: "#e4e4e7"
                                    border.width: 1

                                    RowLayout {
                                        anchors.fill: parent
                                        anchors.margins: 12
                                        spacing: 0

                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            spacing: 2
                                            HusText { text: "5★ 数"; font.pixelSize: Math.round(12 * root.fs); color: "#71717a"; Layout.alignment: Qt.AlignHCenter }
                                            HusText {
                                                text: (hoyoPoolListView.currentPool.high_rarity_count || 0)
                                                font.pixelSize: Math.round(22 * root.fs); font.bold: true; color: "#dc2626"
                                                Layout.alignment: Qt.AlignHCenter
                                            }
                                        }
                                        Rectangle { width: 1; height: 40; color: "#e4e4e7" }

                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            spacing: 2
                                            HusText { text: "出货率"; font.pixelSize: Math.round(12 * root.fs); color: "#71717a"; Layout.alignment: Qt.AlignHCenter }
                                            HusText {
                                                text: (hoyoPoolListView.currentPool.high_rarity_rate || "0%")
                                                font.pixelSize: Math.round(20 * root.fs); font.bold: true; color: "#16a34a"
                                                Layout.alignment: Qt.AlignHCenter
                                            }
                                        }
                                        Rectangle { width: 1; height: 40; color: "#e4e4e7" }

                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            spacing: 2
                                            HusText { text: "当前保底"; font.pixelSize: Math.round(12 * root.fs); color: "#71717a"; Layout.alignment: Qt.AlignHCenter }
                                            HusText {
                                                text: (hoyoPoolListView.currentPool.current_pity || 0) + " / 90"
                                                font.pixelSize: Math.round(18 * root.fs); font.bold: true; color: "#d97706"
                                                Layout.alignment: Qt.AlignHCenter
                                            }
                                        }
                                    }
                                }

                                // 5★ 出货历史标题
                                HusText {
                                    text: "5★ 出货历史 (最新在前，共 " + (hoyoPoolListView.currentPool.high_rarity_history || []).length + " 条)"
                                    font.pixelSize: Math.round(15 * root.fs)
                                    font.bold: true
                                    color: "#27272a"
                                    Layout.topMargin: 6
                                }

                                // 5★ 出货历史 — Flet 风格横向行卡片列表
                                ListView {
                                    id: hoyoFiveStarList
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: Math.min((hoyoPoolListView.currentPool.high_rarity_history || []).length, 60) * 42 + 10
                                    visible: root.pageActive && (hoyoPoolListView.currentPool.high_rarity_history || []).length > 0
                                    clip: true
                                    interactive: true
                                    spacing: 4

                                    // 倒序：最新在前；页面未激活时不创建 delegate
                                    model: {
                                        if (!root.pageActive) return []
                                        var list = (hoyoPoolListView.currentPool.high_rarity_history || []).slice(0)
                                        return list.reverse()
                                    }

                                    delegate: Rectangle {
                                        width: hoyoFiveStarList.width
                                        height: 38
                                        required property var modelData
                                        color: "#fafafa"
                                        radius: 6
                                        border.color: "#e4e4e7"
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
                                            // 名字 (金色粗体, 可 tooltip, 固定宽度+elide)
                                            HusText {
                                                text: modelData.name || "?"
                                                font.pixelSize: Math.round(14 * root.fs); font.bold: true; color: "#d4a017"
                                                Layout.preferredWidth: 120
                                                elide: Text.ElideRight
                                                ToolTip.text: modelData.name || ""
                                                ToolTip.visible: hoyoNameHover.containsMouse
                                                ToolTip.delay: 200
                                                MouseArea {
                                                    id: hoyoNameHover
                                                    anchors.fill: parent
                                                    hoverEnabled: true
                                                    cursorShape: Qt.IBeamCursor
                                                }
                                            }
                                            // 第几抽 (绿色, 固定宽度, 确保优先显示)
                                            HusText {
                                                text: modelData.pity ? ("第 " + modelData.pity + " 抽") : ""
                                                font.pixelSize: Math.round(13 * root.fs); color: "#16a34a"; font.bold: true
                                                Layout.minimumWidth: 70
                                                Layout.preferredWidth: 80
                                                horizontalAlignment: Text.AlignRight
                                            }
                                            // 卡池名 (muted, 可 tooltip, fillWidth 放最后)
                                            HusText {
                                                text: modelData.pool_name ? "[" + modelData.pool_name + "]" : ""
                                                font.pixelSize: Math.round(12 * root.fs); color: "#999"
                                                Layout.fillWidth: true
                                                Layout.minimumWidth: 0
                                                elide: Text.ElideRight
                                                ToolTip.text: modelData.pool_name || ""
                                                ToolTip.visible: hoyoBannerHover.containsMouse
                                                ToolTip.delay: 200
                                                MouseArea {
                                                    id: hoyoBannerHover
                                                    anchors.fill: parent
                                                    hoverEnabled: true
                                                    cursorShape: Qt.IBeamCursor
                                                }
                                            }
                                        }
                                    }
                                }

                                HusText {
                                    visible: (hoyoPoolListView.currentPool.high_rarity_history || []).length === 0
                                    text: "该卡池暂无 5★ 出货记录"
                                    font.pixelSize: Math.round(14 * root.fs); color: "#999"
                                    Layout.topMargin: 20
                                    Layout.alignment: Qt.AlignHCenter
                                }
                            }
                        }
                    }
                }
            }

            // MiHoYo Sign section
            HusCard {
                Layout.fillWidth: true
                title: "✅ 米游社签到"
                padding: 16

                contentItem: ColumnLayout {
                    spacing: 12

                    CredentialInput {
                        id: mihoyoCookieInput
                        Layout.fillWidth: true
                        placeholderText: "粘贴 Cookie 字符串..."
                        onEditingFinished: bridge.saveCredential("mihoyo_sign", "cookie", text)
                    }

                    RowLayout {
                        spacing: 10
                        HusSelect {
                            id: mihoyoBrowserSelect
                            Layout.preferredWidth: 150
                            clearEnabled: false
                            model: ListModel {
                                ListElement { label: "Microsoft Edge"; value: "edge" }
                                ListElement { label: "Google Chrome"; value: "chrome" }
                                ListElement { label: "Brave"; value: "brave" }
                            }
                            currentIndex: 0
                            onActivated: bridge.saveCredential("mihoyo_sign", "browser", model.get(currentIndex).value)
                        }
                        HusButton {
                            text: "打开登录页"
                            type: HusButton.Type_Outlined
                            onClicked: bridge.openMihoyoLoginPage()
                        }
                        HusButton {
                            text: "抓取 Cookie"
                            type: HusButton.Type_Outlined
                            enabled: !bridge.isRunning
                            onClicked: bridge.fetchMihoyoCookie(mihoyoBrowserSelect.model.get(mihoyoBrowserSelect.currentIndex).value)
                        }
                        HusButton {
                            text: "扫码登录"
                            type: HusButton.Type_Outlined
                            enabled: !bridge.isRunning
                            onClicked: bridge.runMihoyoQrLogin(mihoyoBrowserSelect.model.get(mihoyoBrowserSelect.currentIndex).value)
                        }
                        HusButton {
                            text: "执行签到"
                            type: HusButton.Type_Primary
                            enabled: !bridge.isRunning
                            onClicked: bridge.runMihoyoSign(mihoyoCookieInput.text)
                        }
                    }

                    Flow {
                        spacing: 10
                        Layout.fillWidth: true
                        HusCheckBox {
                            id: genshinSignCheck
                            text: "原神签到"
                            checked: true
                            onClicked: bridge.saveCredentialBool("mihoyo_sign", "enabled_games.genshin", checked)
                        }
                        HusCheckBox {
                            id: honkaiSrSignCheck
                            text: "星铁签到"
                            checked: true
                            onClicked: bridge.saveCredentialBool("mihoyo_sign", "enabled_games.honkai_sr", checked)
                        }
                        HusCheckBox {
                            id: zzzSignCheck
                            text: "绝区零签到"
                            checked: true
                            onClicked: bridge.saveCredentialBool("mihoyo_sign", "enabled_games.zzz", checked)
                        }
                        HusCheckBox {
                            id: mihoyoStartupCheck
                            text: "启动时签到"
                            checked: true
                            onClicked: bridge.saveCredentialBool("mihoyo_sign", "startup_sign", checked)
                        }
                    }

                    // ── 设备指纹绑定（可选，降低便笺查询 1034 风控）──
                    HusText {
                        text: "📱 设备指纹绑定（可选）"
                        font.pixelSize: Math.round(14 * root.fs)
                        font.bold: true
                    }
                    HusText {
                        text: "粘贴 copy-device-info APK 复制的字段集合（或抓包得到的 {\"fp\":…,\"device_id\":…}），程序自动识别字段并绑定，降低米游社便笺查询触发风控 (1034) 的概率。"
                        font.pixelSize: Math.round(12 * root.fs)
                        color: "#71717a"
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                    ScrollView {
                        Layout.fillWidth: true
                        Layout.preferredHeight: Math.round(76 * root.fs)
                        clip: true

                        TextArea {
                            id: mihoyoDeviceInput
                            wrapMode: TextArea.Wrap
                            font.family: "Consolas"
                            font.pixelSize: Math.round(12 * root.fs)
                            color: "#18181b"
                            placeholderText: "{\"deviceModel\":\"…\",\"androidVersion\":\"…\",\"deviceFingerprint\":\"…\",\"oaid\":\"…\"}"
                            background: Rectangle { color: "#ffffff"; border.color: "#e4e4e7"; radius: 6 }
                        }
                    }
                    RowLayout {
                        spacing: 10
                        Layout.fillWidth: true

                        HusButton {
                            text: "识别并保存"
                            type: HusButton.Type_Primary
                            enabled: !bridge.isRunning && mihoyoDeviceInput.text.length > 0
                            onClicked: bridge.saveMihoyoDevice(mihoyoDeviceInput.text)
                        }
                        HusButton {
                            text: "清除绑定"
                            type: HusButton.Type_Outlined
                            enabled: !bridge.isRunning
                            onClicked: bridge.clearMihoyoDevice()
                        }
                        HusText {
                            id: mihoyoDeviceStatus
                            text: "未绑定"
                            font.pixelSize: Math.round(12 * root.fs)
                            color: "#71717a"
                            Layout.fillWidth: true
                        }
                    }

                    HusText {
                        text: "签到状态: 等待操作"
                        font.pixelSize: Math.round(12 * root.fs)
                        color: "#71717a"
                    }
                }
            }
        }
    }
}
