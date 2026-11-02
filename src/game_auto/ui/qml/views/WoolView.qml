import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import HuskarUI.Basic

// 薅羊毛集合：子流程执行列表勾选 + 一键运行
Item {
    id: root
    readonly property real fs: Application.window ? Application.window.fontScale : 1

    property var woolFlows: []

    function reload() {
        try {
            root.woolFlows = JSON.parse(bridge.getWoolFlows() || "[]")
        } catch(e) {
            root.woolFlows = []
        }
    }

    property double _lastReloadTs: 0
    Component.onCompleted: { _lastReloadTs = Date.now(); root.reload() }
    onVisibleChanged: { if (visible && Date.now() - _lastReloadTs > 30000) { _lastReloadTs = Date.now(); root.reload() } }

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
                    HusText { text: "💰 薅羊毛集合"; font.pixelSize: Math.round(22 * root.fs); font.bold: true }
                    HusText {
                        text: "按顺序自动执行勾选的签到/领取流程：启动程序 → 匹配点击 → 确认结果 → 关闭程序。\n在下方勾选要参与的流程；取消勾选的流程在一条龙里运行时也会自动跳过。"
                        font.pixelSize: Math.round(13 * root.fs)
                        color: "#6d6257"
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                    RowLayout {
                        spacing: 12
                        HusButton {
                            text: "▶ 运行勾选的流程"
                            type: HusButton.Type_Primary
                            enabled: !bridge.isRunning
                            onClicked: bridge.runWoolCollection()
                        }
                        HusText {
                            text: bridge.isRunning ? "有任务正在运行…" : "空闲"
                            font.pixelSize: Math.round(12 * root.fs)
                            color: bridge.isRunning ? "#b66f1f" : "#6d6257"
                        }
                    }
                }
            }

            // Execution list
            HusCard {
                Layout.fillWidth: true
                title: "执行列表"
                padding: 14

                contentItem: ColumnLayout {
                    spacing: 10
                    Layout.fillWidth: true

                    HusEmpty {
                        visible: root.woolFlows.length === 0
                        description: "暂无薅羊毛流程"
                    }

                    Repeater {
                        model: root.woolFlows

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 76
                            radius: 10
                            color: modelData.enabled ? "#fffdf9" : "#f4ecdf"
                            border.color: modelData.enabled ? "#dbcdbd" : "#e5dccb"
                            border.width: 1

                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: 12
                                spacing: 12

                                HusCheckBox {
                                    checked: modelData.enabled
                                    onClicked: {
                                        bridge.saveWoolFlowEnabled(modelData.key, checked)
                                        root.reload()
                                    }
                                }
                                ColumnLayout {
                                    spacing: 2
                                    Layout.fillWidth: true
                                    HusText {
                                        text: modelData.name
                                        font.pixelSize: Math.round(14 * root.fs)
                                        font.bold: true
                                        color: "#2c241d"
                                    }
                                    HusText {
                                        text: modelData.key + " · " + (modelData.close_on_finish ? "完成后关闭" : "完成后保留") + " · " + (modelData.exe_path || "程序路径未配置")
                                        font.pixelSize: Math.round(11 * root.fs)
                                        font.family: "Consolas"
                                        color: "#6d6257"
                                        elide: Text.ElideMiddle
                                        Layout.fillWidth: true
                                    }
                                }
                                ColumnLayout {
                                    spacing: 2
                                    HusCheckBox {
                                        text: "完成后关闭程序"
                                        checked: modelData.close_on_finish
                                        onClicked: {
                                            bridge.saveWoolFlowCloseOnFinish(modelData.key, checked)
                                            root.reload()
                                        }
                                    }
                                    HusTag {
                                        text: modelData.exe_ready ? "✓ 程序就绪" : "⚠ 未配置路径"
                                        tagState: modelData.exe_ready ? HusTag.State_Success : HusTag.State_Warning
                                    }
                                }
                            }
                        }
                    }

                    HusText {
                        text: "未配置路径的流程请到「路径配置」页填写程序位置，运行时会被跳过并报错。"
                        font.pixelSize: Math.round(12 * root.fs)
                        color: "#6d6257"
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                }
            }
        }
    }
}
