import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import HuskarUI.Basic

// 凭据输入框：默认掩码，右侧有眼睛按钮切换显隐
RowLayout {
    id: root

    // 对外暴露的属性（与 HusInput 兼容）
    property string text: __input.text
    property string placeholderText: ""
    property bool readOnly: false
    property bool __visible: false

    // 提交信号
    signal editingFinished()

    spacing: 6

    HusInput {
        id: __input
        Layout.fillWidth: true
        echoMode: root.__visible ? TextInput.Normal : TextInput.Password
        placeholderText: root.placeholderText
        readOnly: root.readOnly
        clearEnabled: true
        onEditingFinished: root.editingFinished()
    }

    HusButton {
        Layout.preferredWidth: 50
        type: HusButton.Type_Outlined
        text: root.__visible ? "隐藏" : "显示"
        onClicked: root.__visible = !root.__visible
    }
}
