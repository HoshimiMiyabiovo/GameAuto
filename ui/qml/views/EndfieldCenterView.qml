import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import QtQuick.Dialogs
import HuskarUI.Basic
import "../components"

// Endfield Data Center + Gacha Analysis + Skendfield Sign view
Item {
    id: root
    readonly property real fs: Application.window ? Application.window.fontScale : 1

    // ── Data center state ──
    property var queryResult: ({})

    // ── Gacha analysis state ──
    property var gachaData: ({})
    property var overview: ({})
    property var pools: []
    property int selectedPoolIndex: 0
    property var characterPools: []
    property var weaponPools: []
    property string currentCategory: "character"  // character | weapon
    readonly property int maxPity6: 99  // 6星保底阈值（终末地）
    readonly property bool pageActive: bridge.currentView === "endfield_center"
    property var currentPools: root.currentCategory === "weapon" ? root.weaponPools : root.characterPools
    property var currentPool: root.currentPools[root.selectedPoolIndex] || ({})

    Component.onCompleted: {
        bridge.loadEndfieldDefault()
        bridge.loadCredentials()
    }

    // 视图从隐藏→显示时重新灌一次凭据（应对外部修改 app_config.json 的场景）
    onVisibleChanged: {
        if (visible) bridge.loadCredentials()
    }

    function updateGachaDisplay() {
        var ov = root.overview
        kpiTotal.value = ov.total_records || "0"
        kpi6Star.value = ov.six_star_count || "0"
        kpi5Star.value = ov.five_star_count || "0"
        kpi4Star.value = ov.four_star_count || "0"

        var total = ov.total_records || 0
        var sixCount = ov.six_star_count || 0
        kpiTotal.note = total > 0 ? "6★ 占比 " + (sixCount / total * 100).toFixed(1) + "%" : "等待导入"
        kpi6Star.note = total > 0 ? "出货率 " + (sixCount / total * 100).toFixed(1) + "%" : "等待导入"
        kpi5Star.note = ov.latest_time ? "最新: " + ov.latest_time.substring(0, 10) : "等待导入"
        kpi4Star.note = ov.earliest_time ? "从 " + ov.earliest_time.substring(0, 10) : "等待导入"
    }

    Connections {
        target: bridge
        function onCredentialsChanged(jsonStr) {
            try {
                var creds = JSON.parse(jsonStr || "{}")
                var proto = creds.endfield_protocol || {}
                if (proto.api_key) apikeyInput.text = proto.api_key
                if (proto.framework_token) frameworkTokenInput.text = proto.framework_token
                if (proto.role_id) roleIdInput.text = proto.role_id
                if (proto.server_id) serverIdInput.text = proto.server_id

                var sk = creds.skendfield_sign || {}
                if (sk.token) skendfieldTokenInput.text = sk.token
                skendfieldStartupCheck.checked = !!sk.startup_sign
            } catch(e) { /* ignore */ }
        }
        function onEndfieldQueryResultChanged(jsonStr) {
            try { root.queryResult = JSON.parse(jsonStr || "{}") } catch(e) { root.queryResult = ({}) }
        }
        function onEndfieldGachaChanged(jsonStr) {
            try { root.gachaData = JSON.parse(jsonStr || "{}") } catch(e) { root.gachaData = ({}) }
            root.overview = root.gachaData.overview || {}
            root.pools = root.gachaData.pools || []
            // 按卡池类型分组：包含"武器"的归武器池，其他归角色池
            var cp = [], wp = []
            for (var i = 0; i < root.pools.length; i++) {
                var name = root.pools[i].pool_name || ""
                if (name.indexOf("武器") >= 0) wp.push(root.pools[i])
                else cp.push(root.pools[i])
            }
            root.characterPools = cp
            root.weaponPools = wp
            if (root.selectedPoolIndex >= root.currentPools.length) root.selectedPoolIndex = 0
            root.updateGachaDisplay()
        }
    }

    FileDialog {
        id: endfieldFileDialog
        title: "选择终末地抽卡 JSON 文件"
        nameFilters: ["JSON 文件 (*.json)", "所有文件 (*)"]
        onAccepted: {
            var path = endfieldFileDialog.selectedFile.toString().replace("file:///", "")
            endfieldPathInput.text = path
            bridge.importEndfieldGacha(path)
        }
    }

    ScrollView {
        anchors.fill: parent
        clip: true

        ColumnLayout {
            width: parent.width
            spacing: 14

            // ── 抽卡总览 KPI + 文件导入 ──
            HusCard {
                Layout.fillWidth: true
                title: "🏗️ 终末地抽卡总览"
                padding: 18

                contentItem: ColumnLayout {
                    spacing: 14

                    RowLayout {
                        spacing: 14
                        Layout.fillWidth: true

                        GachaMetricCard { id: kpiTotal;  title: "总抽数"; value: "0"; note: "等待导入"; Layout.fillWidth: true }
                        GachaMetricCard { id: kpi6Star;  title: "6★ 数"; value: "0"; note: "等待导入"; Layout.fillWidth: true }
                        GachaMetricCard { id: kpi5Star;  title: "5★ 数"; value: "0"; note: "等待导入"; Layout.fillWidth: true }
                        GachaMetricCard { id: kpi4Star;  title: "4★ 数"; value: "0"; note: "等待导入"; Layout.fillWidth: true }
                    }

                    // 文件导入区
                    RowLayout {
                        spacing: 10
                        Layout.fillWidth: true
                        HusInput {
                            id: endfieldPathInput
                            Layout.fillWidth: true
                            placeholderText: "选择 gachaData JSON 文件..."
                            readOnly: true
                        }
                        HusButton {
                            text: "浏览"
                            type: HusButton.Type_Outlined
                            onClicked: endfieldFileDialog.open()
                        }
                        HusButton {
                            text: "分析"
                            type: HusButton.Type_Primary
                            enabled: endfieldPathInput.text.length > 0
                            onClicked: bridge.importEndfieldGacha(endfieldPathInput.text)
                        }
                    }
                }
            }

            // ── 空状态 ──
            HusEmpty {
                Layout.fillWidth: true
                Layout.preferredHeight: 200
                visible: root.pools.length === 0
                description: "请先选择终末地抽卡数据文件导入"
            }

            // ── 卡池详情（角色池 / 武器池 Tab）──
            HusCard {
                Layout.fillWidth: true
                visible: root.pools.length > 0
                padding: 18
                title: "📊 卡池详情"

                contentItem: RowLayout {
                    spacing: 14

                    // ── 左侧：分类 + 卡池列表 ──
                    ColumnLayout {
                        Layout.preferredWidth: 220
                        Layout.fillHeight: true
                        spacing: 8

                        // 分类切换
                        RowLayout {
                            spacing: 4
                            Layout.fillWidth: true
                            HusButton {
                                Layout.fillWidth: true
                                text: "角色池 (" + root.characterPools.length + ")"
                                type: root.currentCategory === "character" ? HusButton.Type_Primary : HusButton.Type_Outlined
                                onClicked: {
                                    root.currentCategory = "character"
                                    root.selectedPoolIndex = 0
                                }
                            }
                            HusButton {
                                Layout.fillWidth: true
                                text: "武器池 (" + root.weaponPools.length + ")"
                                type: root.currentCategory === "weapon" ? HusButton.Type_Primary : HusButton.Type_Outlined
                                onClicked: {
                                    root.currentCategory = "weapon"
                                    root.selectedPoolIndex = 0
                                }
                            }
                        }

                        // 卡池列表
                        ListView {
                            id: efPoolListView
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            Layout.preferredHeight: 280
                            clip: true
                            spacing: 4
                            model: root.currentPools
                            currentIndex: root.selectedPoolIndex

                            delegate: Rectangle {
                                width: efPoolListView.width
                                height: 56
                                required property var modelData
                                required property int index
                                color: root.selectedPoolIndex === index ? "#fff1d6" : (efPoolItemHover.containsMouse ? "#faf3e8" : "transparent")
                                radius: 6
                                border.width: root.selectedPoolIndex === index ? 2 : 0
                                border.color: "#ffaa44"

                                MouseArea {
                                    id: efPoolItemHover
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: root.selectedPoolIndex = index
                                }

                                ColumnLayout {
                                    anchors.fill: parent
                                    anchors.margins: 8
                                    spacing: 2

                                    HusText {
                                        text: modelData.pool_name || "?"
                                        font.pixelSize: Math.round(14 * root.fs)
                                        font.bold: root.selectedPoolIndex === index
                                        color: "#2f4f4f"
                                        Layout.fillWidth: true
                                        elide: Text.ElideRight
                                    }
                                    RowLayout {
                                        spacing: 6
                                        HusText {
                                            text: (modelData.total_pulls || 0) + " 抽"
                                            font.pixelSize: Math.round(12 * root.fs)
                                            color: "#6d6257"
                                        }
                                        HusText {
                                            text: "· 6★: " + (modelData.six_star_count || 0)
                                            font.pixelSize: Math.round(12 * root.fs)
                                            color: "#a8351f"
                                        }
                                    }
                                }
                            }
                        }
                    }

                    // ── 右侧：选中卡池详情 ──
                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        spacing: 12

                        // 卡池标题 + 总抽数
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 12

                            HusText {
                                text: root.currentPool.pool_name || "—"
                                font.pixelSize: Math.round(22 * root.fs)
                                font.bold: true
                                color: "#2f4f4f"
                                Layout.fillWidth: true
                                elide: Text.ElideRight
                                ToolTip.text: root.currentPool.pool_name || ""
                                ToolTip.visible: efTitleHover.containsMouse && (root.currentPool.pool_name || "").length > 12
                                ToolTip.delay: 300

                                MouseArea {
                                    id: efTitleHover
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.IBeamCursor
                                }
                            }
                            HusText {
                                text: (root.currentPool.total_pulls || 0)
                                font.pixelSize: Math.round(36 * root.fs)
                                font.bold: true
                                color: "#2f7d4a"
                            }
                            HusText {
                                text: "抽"
                                font.pixelSize: Math.round(14 * root.fs)
                                color: "#6d6257"
                                Layout.alignment: Qt.AlignBottom
                                Layout.bottomMargin: 6
                            }
                        }

                        // 关键指标横向卡片
                        Rectangle {
                            Layout.fillWidth: true
                            height: 70
                            color: "#fffaf2"
                            radius: 8
                            border.color: "#dbcdbd"
                            border.width: 1

                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: 12
                                spacing: 0

                                // 6★ 数
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 2
                                    HusText {
                                        text: "6★ 数"
                                        font.pixelSize: Math.round(12 * root.fs)
                                        color: "#6d6257"
                                        Layout.alignment: Qt.AlignHCenter
                                    }
                                    HusText {
                                        text: (root.currentPool.six_star_count || 0)
                                        font.pixelSize: Math.round(22 * root.fs)
                                        font.bold: true
                                        color: "#a8351f"
                                        Layout.alignment: Qt.AlignHCenter
                                    }
                                }
                                Rectangle { width: 1; height: 40; color: "#dbcdbd" }

                                // 6★ 出货率
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 2
                                    HusText {
                                        text: "6★ 出货率"
                                        font.pixelSize: Math.round(12 * root.fs)
                                        color: "#6d6257"
                                        Layout.alignment: Qt.AlignHCenter
                                    }
                                    HusText {
                                        text: (root.currentPool.six_star_rate || "0%")
                                        font.pixelSize: Math.round(20 * root.fs)
                                        font.bold: true
                                        color: "#2f7d4a"
                                        Layout.alignment: Qt.AlignHCenter
                                    }
                                }
                                Rectangle { width: 1; height: 40; color: "#dbcdbd" }

                                // 当前保底
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 2
                                    HusText {
                                        text: "当前保底"
                                        font.pixelSize: Math.round(12 * root.fs)
                                        color: "#6d6257"
                                        Layout.alignment: Qt.AlignHCenter
                                    }
                                    HusText {
                                        text: (root.overview.current_pity || 0) + " / " + root.maxPity6
                                        font.pixelSize: Math.round(18 * root.fs)
                                        font.bold: true
                                        color: "#b66f1f"
                                        Layout.alignment: Qt.AlignHCenter
                                    }
                                }
                            }
                        }

                        // 6★ 出货历史标题
                        HusText {
                            text: "6★ 出货历史 (最新在前，共 " + (root.currentPool.high_rarity_history || []).length + " 条)"
                            font.pixelSize: Math.round(15 * root.fs)
                            font.bold: true
                            color: "#2f4f4f"
                            Layout.topMargin: 6
                        }

                        // 6★ 出货历史 — Flet 风格横向行卡片列表
                        ListView {
                            id: efSixStarList
                            Layout.fillWidth: true
                            Layout.preferredHeight: Math.min((root.currentPool.high_rarity_history || []).length, 50) * 42 + 10
                            visible: root.pageActive && (root.currentPool.high_rarity_history || []).length > 0
                            clip: true
                            interactive: true
                            spacing: 4

                            // 倒序：最新在前；页面未激活时不创建 delegate
                            model: {
                                if (!root.pageActive) return []
                                var list = (root.currentPool.high_rarity_history || []).slice(0)
                                return list.reverse()
                            }

                            delegate: Rectangle {
                                width: efSixStarList.width
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
                                        font.pixelSize: Math.round(12 * root.fs)
                                        color: "#999"
                                        Layout.preferredWidth: 110
                                    }
                                    // 6★ 标签
                                    HusText {
                                        text: "6★"
                                        font.pixelSize: Math.round(12 * root.fs)
                                        font.bold: true
                                        color: "#d4a017"
                                        Layout.preferredWidth: 30
                                    }
                                    // 名字 (金色粗体, 可 tooltip, 固定宽度+elide)
                                    HusText {
                                        text: modelData.name || "?"
                                        font.pixelSize: Math.round(14 * root.fs)
                                        font.bold: true
                                        color: "#d4a017"
                                        Layout.preferredWidth: 120
                                        elide: Text.ElideRight
                                        ToolTip.text: modelData.name || ""
                                        ToolTip.visible: efNameHover.containsMouse
                                        ToolTip.delay: 200
                                        MouseArea {
                                            id: efNameHover
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            cursorShape: Qt.IBeamCursor
                                        }
                                    }
                                    // 第几抽 (绿色, 固定宽度, 确保优先显示)
                                    HusText {
                                        text: modelData.pity ? ("第 " + modelData.pity + " 抽") : ""
                                        font.pixelSize: Math.round(13 * root.fs)
                                        color: "#2f7d4a"
                                        font.bold: true
                                        Layout.minimumWidth: 70
                                        Layout.preferredWidth: 80
                                        horizontalAlignment: Text.AlignRight
                                    }
                                    // 卡池名 (muted, 可 tooltip, fillWidth 放最后)
                                    HusText {
                                        text: modelData.pool_name ? "[" + modelData.pool_name + "]" : ""
                                        font.pixelSize: Math.round(12 * root.fs)
                                        color: "#999"
                                        Layout.fillWidth: true
                                        Layout.minimumWidth: 0
                                        elide: Text.ElideRight
                                        ToolTip.text: modelData.pool_name || ""
                                        ToolTip.visible: efBannerHover.containsMouse
                                        ToolTip.delay: 200
                                        MouseArea {
                                            id: efBannerHover
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            cursorShape: Qt.IBeamCursor
                                        }
                                    }
                                }
                            }
                        }

                        HusText {
                            visible: (root.currentPool.high_rarity_history || []).length === 0
                            text: "该卡池暂无 6★ 出货记录"
                            font.pixelSize: Math.round(14 * root.fs)
                            color: "#999"
                            Layout.topMargin: 20
                            Layout.alignment: Qt.AlignHCenter
                        }
                    }
                }
            }

            // ── 数据中心查询 ──
            HusCard {
                Layout.fillWidth: true
                title: "📡 终末地数据中心"
                padding: 16

                contentItem: ColumnLayout {
                    spacing: 10

                    CredentialInput {
                        id: apikeyInput
                        Layout.fillWidth: true
                        placeholderText: "API Key..."
                        onEditingFinished: bridge.saveCredential("endfield_protocol", "api_key", text)
                    }
                    CredentialInput {
                        id: frameworkTokenInput
                        Layout.fillWidth: true
                        placeholderText: "Framework Token..."
                        onEditingFinished: bridge.saveCredential("endfield_protocol", "framework_token", text)
                    }

                    RowLayout {
                        spacing: 10
                        HusInput {
                            id: roleIdInput
                            Layout.fillWidth: true
                            placeholderText: "Role ID..."
                            onEditingFinished: bridge.saveCredential("endfield_protocol", "role_id", text)
                        }
                        HusInput {
                            id: serverIdInput
                            Layout.fillWidth: true
                            placeholderText: "Server ID..."
                            onEditingFinished: bridge.saveCredential("endfield_protocol", "server_id", text)
                        }
                    }

                    // 查询按钮区 (按钮映射到 query_type)
                    GridLayout {
                        Layout.fillWidth: true
                        columns: 3
                        rowSpacing: 6
                        columnSpacing: 6

                        HusButton {
                            Layout.fillWidth: true
                            text: "便笺查询"; type: HusButton.Type_Outlined
                            onClicked: bridge.queryEndfieldCenter("note")
                        }
                        HusButton {
                            Layout.fillWidth: true
                            text: "理智查询"; type: HusButton.Type_Outlined
                            onClicked: bridge.queryEndfieldCenter("stamina")
                        }
                        HusButton {
                            Layout.fillWidth: true
                            text: "干员查询"; type: HusButton.Type_Outlined
                            onClicked: bridge.queryEndfieldCenter("search_chars")
                        }
                        HusButton {
                            Layout.fillWidth: true
                            text: "建设查询"; type: HusButton.Type_Outlined
                            onClicked: bridge.queryEndfieldCenter("spaceship")
                        }
                        HusButton {
                            Layout.fillWidth: true
                            text: "抽卡查询"; type: HusButton.Type_Outlined
                            onClicked: bridge.queryEndfieldCenter("gacha_records")
                        }
                        HusButton {
                            Layout.fillWidth: true
                            text: "成就查询"; type: HusButton.Type_Outlined
                            onClicked: bridge.queryEndfieldCenter("achieve")
                        }
                    }

                    // 查询结果区
                    Rectangle {
                        Layout.fillWidth: true
                        visible: root.queryResult.query_type !== undefined
                        height: resultColumn.height + 20
                        color: root.queryResult.success ? "#dff1e4" : "#f7dfdd"
                        radius: 6
                        border.color: root.queryResult.success ? "#2f7d4a" : "#b04b44"
                        border.width: 1

                        ColumnLayout {
                            id: resultColumn
                            anchors.fill: parent
                            anchors.margins: 10
                            spacing: 6

                            RowLayout {
                                spacing: 8
                                HusText {
                                    text: root.queryResult.success ? "✓ " + (root.queryResult.query_type || "") + " 查询成功"
                                          : "✗ " + (root.queryResult.query_type || "") + " 查询失败"
                                    font.pixelSize: Math.round(13 * root.fs); font.bold: true
                                    color: root.queryResult.success ? "#2f7d4a" : "#b04b44"
                                }
                            }

                            HusText {
                                text: root.queryResult.message || ""
                                font.pixelSize: Math.round(12 * root.fs)
                                color: "#2f4f4f"
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                                visible: !!(root.queryResult.message)
                            }

                            ScrollView {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 280
                                visible: root.queryResult.data !== null && root.queryResult.data !== undefined

                                HusText {
                                    text: {
                                        try {
                                            var d = root.queryResult.data
                                            if (d === null || d === undefined) return ""
                                            return JSON.stringify(d, null, 2)
                                        } catch(e) {
                                            return String(root.queryResult.data || "")
                                        }
                                    }
                                    font.family: "Consolas, Monaco, monospace"
                                    font.pixelSize: Math.round(11 * root.fs)
                                    color: "#2f4f4f"
                                    wrapMode: Text.WrapAnywhere
                                }
                            }
                        }
                    }
                }
            }

            // ── 森空岛签到 ──
            HusCard {
                Layout.fillWidth: true
                title: "✅ 森空岛签到"
                padding: 16

                contentItem: ColumnLayout {
                    spacing: 10

                    CredentialInput {
                        id: skendfieldTokenInput
                        Layout.fillWidth: true
                        placeholderText: "粘贴 Token..."
                        onEditingFinished: bridge.saveCredential("skendfield_sign", "token", text)
                    }

                    RowLayout {
                        spacing: 10
                        HusSelect {
                            id: skendfieldServerSelect
                            clearEnabled: false
                            model: ListModel {
                                ListElement { label: "国服 (CN)"; value: "cn" }
                                ListElement { label: "国际服"; value: "global" }
                            }
                            currentIndex: 0
                            onActivated: {
                                var v = model.get(currentIndex).value
                                bridge.saveCredential("skendfield_sign", "server", v)
                            }
                        }
                        HusButton {
                            text: "执行签到"
                            type: HusButton.Type_Primary
                            enabled: !bridge.isRunning
                            onClicked: {
                                if (skendfieldTokenInput.text.length > 0) {
                                    bridge.saveCredential("skendfield_sign", "token", skendfieldTokenInput.text)
                                }
                                bridge.runSkendfieldSign()
                            }
                        }
                    }

                    HusCheckBox {
                        id: skendfieldStartupCheck
                        text: "启动时自动签到"
                        checked: false
                        onClicked: bridge.saveCredentialBool("skendfield_sign", "startup_sign", checked)
                    }

                    HusText {
                        text: "状态: 等待操作"
                        font.pixelSize: Math.round(12 * root.fs)
                        color: "#6d6257"
                    }
                }
            }
        }
    }
}
