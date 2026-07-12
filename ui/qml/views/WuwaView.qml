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

    // 页面激活标志：仅在页面可见时创建 delegate，避免 width=0 时创建后不更新
    readonly property bool pageActive: bridge.currentView === "wuwa"

    Component.onCompleted: {
        bridge.loadWuwaGacha()
        bridge.loadCredentials()
    }

    Connections {
        target: bridge
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
                if (profile.token) wuwaTokenInput.text = profile.token
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
                        placeholderText: "Token (可选)..."
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
                            text: "执行签到"
                            type: HusButton.Type_Outlined
                            onClicked: {
                                if (wuwaTokenInput.text.length > 0) {
                                    bridge.saveCredential("wuwa_kujiequ", "profile.token", wuwaTokenInput.text)
                                }
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
                        text: "状态: 等待操作"
                        font.pixelSize: Math.round(12 * root.fs)
                        color: "#6d6257"
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
}
