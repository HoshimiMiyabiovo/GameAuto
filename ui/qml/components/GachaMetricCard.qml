import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import HuskarUI.Basic

// Reusable metric card for gacha overview stats
HusCard {
    id: root
    property string title: ""
    property string value: "-"
    property string note: ""
    readonly property real fs: Application.window ? Application.window.fontScale : 1

    Layout.fillWidth: true
    Layout.minimumWidth: 140
    padding: 12

    contentItem: ColumnLayout {
        spacing: 4
        HusText {
            text: root.title
            font.pixelSize: Math.round(12 * root.fs)
            color: "#6d6257"
        }
        HusText {
            text: root.value
            font.pixelSize: Math.round(24 * root.fs)
            font.bold: true
        }
        HusText {
            text: root.note
            font.pixelSize: Math.round(11 * root.fs)
            color: "#6d6257"
            wrapMode: Text.Wrap
            Layout.fillWidth: true
        }
    }
}
