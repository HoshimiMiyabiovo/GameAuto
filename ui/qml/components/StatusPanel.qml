import QtQuick
import QtQuick.Layouts
import HuskarUI.Basic

// Reusable status + action panel for automation view
HusCard {
    id: root
    title: "开始运行"
    padding: 16

    // Public properties (set from outside, bound to bridge)
    property string summaryText: "还没有选择任务"
    property string statusTitleText: "准备就绪"
    property string statusBodyText: "先勾选任务，再点击开始运行。"
    property string badgeLabel: "待命中"
    property string badgeBgColor: "#dff1e4"
    property string badgeFgColor: "#2f7d4a"
    property bool showSpinner: false
    property bool running: false

    signal startClicked()
    signal stopClicked()

    contentItem: ColumnLayout {
        spacing: 14

        // Selection summary
        HusText {
            text: root.summaryText
            font.pixelSize: 20
            font.bold: true
        }

        HusText {
            text: "按照 app_config.json 中的顺序依次执行。"
            font.pixelSize: 13
            color: "#6d6257"
        }

        // Start / Stop buttons
        RowLayout {
            spacing: 12
            HusButton {
                text: "开始运行"
                type: HusButton.Type_Primary
                enabled: !root.running
                onClicked: root.startClicked()
            }
            HusButton {
                text: "停止"
                type: HusButton.Type_Outlined
                enabled: root.running
                onClicked: root.stopClicked()
            }
        }

        // Status indicator card
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: statusRow.height + 24
            color: "#e9f3fb"
            radius: 12

            RowLayout {
                id: statusRow
                anchors.centerIn: parent
                spacing: 12

                // Badge chip
                HusTag {
                    text: root.badgeLabel
                }

                // Spinner
                HusSpin {
                    visible: root.showSpinner
                    Layout.preferredWidth: 20
                    Layout.preferredHeight: 20
                }

                ColumnLayout {
                    spacing: 2
                    HusText {
                        text: root.statusTitleText
                        font.pixelSize: 16
                        font.bold: true
                    }
                    HusText {
                        text: root.statusBodyText
                        font.pixelSize: 13
                        color: "#6d6257"
                        Layout.fillWidth: true
                        wrapMode: Text.Wrap
                    }
                }
            }
        }
    }
}
