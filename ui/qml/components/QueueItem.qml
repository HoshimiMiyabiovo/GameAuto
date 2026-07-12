import QtQuick
import QtQuick.Layouts
import HuskarUI.Basic

// A single queue / task run order item
Rectangle {
    id: root
    width: parent ? parent.width : 200
    height: rowLayout.height + 16
    color: "#fffdf9"
    radius: 12
    border.color: "#dbcdbd"
    border.width: 1

    property int indexNumber: 1
    property string taskName: ""
    property string scriptLabel: ""
    property string stateText: "待执行"
    property string stateColor: "#6d6257"
    property string stateBg: "#f4ecdf"

    RowLayout {
        id: rowLayout
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        anchors.leftMargin: 14
        anchors.rightMargin: 14
        spacing: 10

        // Number
        HusText {
            text: root.indexNumber
            font.pixelSize: 14
            font.bold: true
            Layout.preferredWidth: 24
        }

        // Name + script
        ColumnLayout {
            spacing: 2
            Layout.fillWidth: true
            HusText {
                text: root.taskName
                font.pixelSize: 14
                Layout.fillWidth: true
                elide: Text.ElideRight
            }
            HusText {
                text: root.scriptLabel
                font.pixelSize: 12
                color: "#6d6257"
                Layout.fillWidth: true
                elide: Text.ElideRight
            }
        }

        // Status badge
        Rectangle {
            radius: 10
            color: root.stateBg
            Layout.preferredWidth: stateTag.implicitWidth + 16
            Layout.preferredHeight: stateTag.implicitHeight + 8

            HusText {
                id: stateTag
                anchors.centerIn: parent
                text: root.stateText
                font.pixelSize: 11
                color: root.stateColor
            }
        }
    }
}
