import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import QtQuick.Dialogs
import HuskarUI.Basic
import "../components"

// Infinity Nikki Resonance view
Item {
    id: root
    readonly property real fs: Application.window ? Application.window.fontScale : 1

    property var gachaData: ({})
    property var overview: ({})
    property var pools: []
    property bool isPearpal: false

    Component.onCompleted: {
        console.log("[DBG] view onCompleted entered, bridge=", bridge)
        try {
            bridge.loadNikkiGacha()
            console.log("[DBG] loadNikkiGacha() returned")
        } catch(e) { console.log("[DBG] loadNikkiGacha THROW:", e) }
        bridge.loadCredentials()
    }

    Connections {
        target: bridge
        function onNikkiGachaChanged(jsonStr) {
            console.log("[DBG] onNikkiGachaChanged called, len=", (jsonStr||"").length)
            try { root.gachaData = JSON.parse(jsonStr || "{}") } catch(e) { console.log("[DBG] parse FAIL:", e); root.gachaData = ({}) }
            root.overview = root.gachaData.overview || {}
            root.pools = root.gachaData.pools || []
            root.isPearpal = (root.overview.mode === "pearpal")
            root.updateDisplay()
            console.log("[DBG] after update: pools=", root.pools.length, "mode=", root.overview.mode)
        }
        function onCredentialsChanged(jsonStr) {
            try {
                var creds = JSON.parse(jsonStr || "{}")
                var nikki = creds.nikki || {}
                if (nikki.momo_token) nikkiTokenInput.text = nikki.momo_token
                if (nikki.uid) nikkiUidInput.text = nikki.uid
            } catch(e) { /* ignore */ }
        }
    }

    function updateDisplay() {
        var ov = root.overview
        if (root.isPearpal) {
            // Pearpal summary mode
            totalCard.title = "估算总抽数"
            totalCard.value = ov.total_pulls || "-"
            totalCard.note = "事件数: " + (ov.total_records || 0)
            poolCard.title = "卡池数"
            poolCard.value = ov.pool_count || "-"
            poolCard.note = "4★: " + (ov.four_count || 0) + " | 5★: " + (ov.five_count || 0)
            timeCard.title = "数据模式"
            timeCard.value = "Pearpal 摘要"
            timeCard.note = "无精确时间戳"
        } else {
            // Raw records mode
            totalCard.title = "总记录"
            totalCard.value = ov.total_records || "-"
            totalCard.note = "等待加载"
            poolCard.title = "卡池数"
            poolCard.value = ov.pool_count || "-"
            poolCard.note = "等待加载"
            timeCard.title = "最新记录"
            timeCard.value = ov.latest_time || "-"
            timeCard.note = ov.earliest_time ? ("从 " + ov.earliest_time) : "等待加载"
        }
    }

    ScrollView {
        anchors.fill: parent
        clip: true

        ColumnLayout {
            width: parent.width
            spacing: 14

            HusCard {
                Layout.fillWidth: true
                title: "👗 无限暖暖共鸣分析"
                padding: 16

                contentItem: ColumnLayout {
                    spacing: 12

                    RowLayout {
                        spacing: 10
                        GachaMetricCard { id: totalCard; title: "总记录"; value: "-"; note: "等待加载" }
                        GachaMetricCard { id: poolCard; title: "卡池数"; value: "-"; note: "等待加载" }
                        GachaMetricCard { id: timeCard; title: "最新记录"; value: "-"; note: "等待加载" }
                    }

                    HusText {
                        text: root.pools.length === 0
                              ? "暂无抽卡记录，请先在 Flet 版本或 CLI 中导入，或使用下方在线抓取。"
                              : "UID: " + (root.gachaData.uid || "") + " | 已加载 " + root.pools.length + " 个卡池" + (root.isPearpal ? " (Pearpal 摘要模式)" : "")
                        font.pixelSize: Math.round(12 * root.fs)
                        color: "#6d6257"
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }

                    // ── 五星套装汇总（全局，按套装聚合）──
                    ColumnLayout {
                        visible: root.isPearpal && (root.overview.outfit_summary || []).length > 0
                        Layout.fillWidth: true
                        spacing: 6

                        HusText {
                            text: "★ 五星套装汇总（" + (root.overview.outfit_summary || []).length + " 个套装 / "
                                  + (root.overview.five_count || 0) + " 件五星）"
                            font.pixelSize: Math.round(15 * root.fs)
                            font.bold: true
                            color: "#a8741f"
                        }
                        Repeater {
                            model: root.overview.outfit_summary || []
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 2
                                RowLayout {
                                    spacing: 8
                                    HusText {
                                        text: "★ " + (modelData.outfit_name || "?") + "（" + (modelData.parts || []).length + " 件）"
                                        font.pixelSize: Math.round(13 * root.fs)
                                        font.bold: true
                                        color: "#d4a017"
                                    }
                                    HusText {
                                        visible: !!modelData.min_pity
                                        text: "最低 " + modelData.min_pity + " 抽出货"
                                        font.pixelSize: Math.round(11 * root.fs)
                                        color: "#2f7d4a"
                                    }
                                }
                                HusText {
                                    text: {
                                        var parts = modelData.parts || []
                                        var names = []
                                        for (var i = 0; i < parts.length; i++) names.push(parts[i].name || "?")
                                        return "　　" + names.join("、")
                                    }
                                    font.pixelSize: Math.round(12 * root.fs)
                                    color: "#6d6257"
                                    wrapMode: Text.Wrap
                                    Layout.fillWidth: true
                                    Layout.leftMargin: Math.round(14 * root.fs)
                                }
                            }
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

                                // 概览数据：用 GridLayout 防止文字挤出
                                GridLayout {
                                    Layout.fillWidth: true
                                    columns: 4
                                    rowSpacing: 6
                                    columnSpacing: 12

                                    HusText {
                                        Layout.alignment: Qt.AlignLeft
                                        text: root.isPearpal
                                              ? ("估算总抽数: " + (modelData.total_pulls || 0))
                                              : ("总抽数: " + (modelData.total_records || 0))
                                        font.pixelSize: Math.round(13 * root.fs)
                                        color: "#2f4f4f"
                                    }
                                    HusText {
                                        Layout.alignment: Qt.AlignLeft
                                        text: "不同物品: " + (modelData.unique_item_ids || 0)
                                        font.pixelSize: Math.round(13 * root.fs)
                                        color: "#2f4f4f"
                                    }
                                    HusText {
                                        Layout.alignment: Qt.AlignLeft
                                        visible: root.isPearpal
                                        text: "4★: " + (modelData.four_count || 0)
                                        font.pixelSize: Math.round(13 * root.fs)
                                        color: "#a8351f"
                                    }
                                    HusText {
                                        Layout.alignment: Qt.AlignLeft
                                        visible: root.isPearpal
                                        text: "5★: " + (modelData.five_count || 0)
                                        font.pixelSize: Math.round(13 * root.fs)
                                        color: "#a8351f"
                                    }
                                    HusText {
                                        Layout.alignment: Qt.AlignLeft
                                        visible: root.isPearpal
                                        text: "当前 4★ 保底: " + (modelData.current_four_pity || 0)
                                        font.pixelSize: Math.round(12 * root.fs)
                                        color: "#6d6257"
                                    }
                                    HusText {
                                        Layout.alignment: Qt.AlignLeft
                                        visible: root.isPearpal
                                        text: "当前 5★ 保底: " + (modelData.current_five_pity || 0)
                                        font.pixelSize: Math.round(12 * root.fs)
                                        color: "#6d6257"
                                    }
                                    HusText {
                                        Layout.alignment: Qt.AlignLeft
                                        visible: root.isPearpal && !!(modelData.average_five_gap)
                                        text: "平均 5★ 间隔: " + (modelData.average_five_gap || 0)
                                        font.pixelSize: Math.round(12 * root.fs)
                                        color: "#6d6257"
                                    }
                                }

                                HusText {
                                    visible: !root.isPearpal
                                    text: "时间范围: " + (modelData.earliest_time || "?") + " ~ " + (modelData.latest_time || "?")
                                    font.pixelSize: Math.round(12 * root.fs)
                                    color: "#6d6257"
                                    Layout.fillWidth: true
                                    wrapMode: Text.Wrap
                                }

                                // 本池 5★ 套装聚合（比逐件列表更直观）
                                ColumnLayout {
                                    visible: root.isPearpal && (modelData.outfit_summary || []).length > 0
                                    Layout.fillWidth: true
                                    spacing: 2
                                    HusText {
                                        text: "本池 5★ 套装（" + (modelData.outfit_summary || []).length + "）"
                                        font.pixelSize: Math.round(13 * root.fs)
                                        font.bold: true
                                        color: "#a8741f"
                                        Layout.topMargin: 4
                                    }
                                    Repeater {
                                        model: modelData.outfit_summary || []
                                        HusText {
                                            text: {
                                                var parts = modelData.parts || []
                                                var names = []
                                                for (var i = 0; i < parts.length; i++) {
                                                    names.push((parts[i].name || "?") + (parts[i].part_type ? "·" + parts[i].part_type : ""))
                                                }
                                                var pity = modelData.min_pity ? ("　最低 " + modelData.min_pity + " 抽") : ""
                                                return "★ " + (modelData.outfit_name || "?") + "（" + parts.length + "件）" + pity + "：" + names.join("、")
                                            }
                                            font.pixelSize: Math.round(12 * root.fs)
                                            color: "#6d6257"
                                            wrapMode: Text.Wrap
                                            Layout.fillWidth: true
                                            Layout.leftMargin: Math.round(10 * root.fs)
                                        }
                                    }
                                }

                                // 5★ 出货历史标题 (pearpal 模式才有 rarity 信息)
                                HusText {
                                    visible: root.isPearpal
                                    text: "5★ 出货历史 (共 " + (modelData.high_rarity_history || []).length + " 条)"
                                    font.pixelSize: Math.round(14 * root.fs)
                                    font.bold: true
                                    color: "#2f4f4f"
                                    Layout.topMargin: 6
                                }

                                ListView {
                                    id: nikkiFiveStarList
                                    visible: root.isPearpal
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: Math.min((modelData.high_rarity_history || []).length, 50) * 40 + 8
                                    clip: true
                                    interactive: true
                                    spacing: 4

                                    // 倒序：最新在前 (recent_records 已按 draw_index 倒序)
                                    model: (modelData.high_rarity_history || []).slice(0)

                                    delegate: Rectangle {
                                        width: nikkiFiveStarList.width
                                        height: 36
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

                                            // 第N抽 (替代时间戳，pearpal 无精确时间)
                                            HusText {
                                                text: modelData.draw_index ? ("第 " + modelData.draw_index + " 抽") : ""
                                                font.pixelSize: Math.round(12 * root.fs)
                                                color: "#999"
                                                Layout.preferredWidth: 100
                                                elide: Text.ElideRight
                                            }
                                            // 5★ 标签
                                            HusText {
                                                text: "5★"
                                                font.pixelSize: Math.round(12 * root.fs)
                                                font.bold: true
                                                color: "#d4a017"
                                                Layout.preferredWidth: 30
                                            }
                                            // 物品名 (金色粗体，固定宽度 + elide)
                                            HusText {
                                                text: modelData.name || "?"
                                                font.pixelSize: Math.round(14 * root.fs)
                                                font.bold: true
                                                color: "#d4a017"
                                                Layout.fillWidth: true
                                                Layout.minimumWidth: 60
                                                elide: Text.ElideRight
                                                ToolTip.text: modelData.name || ""
                                                ToolTip.visible: nikkiNameHover.containsMouse
                                                ToolTip.delay: 200
                                                MouseArea {
                                                    id: nikkiNameHover
                                                    anchors.fill: parent
                                                    hoverEnabled: true
                                                    cursorShape: Qt.IBeamCursor
                                                }
                                            }
                                            // 保底抽数 (绿色)
                                            HusText {
                                                text: modelData.pity ? (modelData.pity + " 抽保底") : ""
                                                font.pixelSize: Math.round(13 * root.fs)
                                                color: "#2f7d4a"
                                                font.bold: true
                                                Layout.preferredWidth: 100
                                                horizontalAlignment: Text.AlignRight
                                            }
                                        }
                                    }
                                }

                                HusText {
                                    visible: root.isPearpal && (modelData.high_rarity_history || []).length === 0
                                    text: "该卡池暂无 5★ 出货记录"
                                    font.pixelSize: Math.round(13 * root.fs)
                                    color: "#999"
                                    Layout.topMargin: 12
                                    Layout.alignment: Qt.AlignHCenter
                                }

                                // raw 模式提示：无法识别 5★，仅显示统计信息
                                HusText {
                                    visible: !root.isPearpal
                                    text: "提示: 原始记录模式无法区分物品稀有度，5★ 出货历史仅在 Pearpal 摘要模式下可用"
                                    font.pixelSize: Math.round(12 * root.fs)
                                    color: "#999"
                                    Layout.fillWidth: true
                                    wrapMode: Text.Wrap
                                    Layout.topMargin: 6
                                }
                            }
                        }
                    }

                    HusEmpty {
                        visible: root.pools.length === 0
                        description: "暂无无限暖暖抽卡数据"
                    }
                }
            }

            HusCard {
                Layout.fillWidth: true
                title: "🔐 在线抓取"
                padding: 16

                contentItem: ColumnLayout {
                    spacing: 10

                    CredentialInput {
                        id: nikkiTokenInput
                        Layout.fillWidth: true
                        placeholderText: "JWT Token..."
                        onEditingFinished: bridge.saveCredential("nikki", "momo_token", text)
                    }
                    HusInput {
                        id: nikkiUidInput
                        Layout.fillWidth: true
                        placeholderText: "UID (可选)..."
                        onEditingFinished: bridge.saveCredential("nikki", "uid", text)
                    }

                    RowLayout {
                        spacing: 10
                        HusButton {
                            text: "抓取全部记录"
                            type: HusButton.Type_Primary
                            onClicked: {
                                if (nikkiTokenInput.text.length > 0) {
                                    bridge.saveCredential("nikki", "momo_token", nikkiTokenInput.text)
                                }
                            }
                        }
                        HusButton { text: "抓取 Pearpal 摘要"; type: HusButton.Type_Outlined }
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
