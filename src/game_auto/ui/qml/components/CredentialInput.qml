import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import HuskarUI.Basic

// 凭据输入框：默认掩码，右侧有眼睛按钮切换显隐
RowLayout {
    id: root

    // 对外暴露的属性（与 HusInput 兼容）
    // text 必须双向同步：纯绑定（property string text: __input.text）会让外部
    // 赋值（如 onCredentialsChanged 回填 xxx.text = "..."）只改 property 本身，
    // 内部输入框不更新——表现为"凭据明明存了，输入框却是空的"。
    property string text: ""
    onTextChanged: if (__input.text !== text) __input.text = text
    property string placeholderText: ""
    property bool readOnly: false
    property bool __visible: false

    // 提交信号
    signal editingFinished()

    function clear() { __input.text = "" }

    spacing: 6

    HusInput {
        id: __input
        Layout.fillWidth: true
        echoMode: root.__visible ? TextInput.Normal : TextInput.Password
        placeholderText: root.placeholderText
        readOnly: root.readOnly
        clearEnabled: true
        onEditingFinished: root.editingFinished()
        onTextChanged: if (root.text !== text) root.text = text
    }

    HusButton {
        Layout.preferredWidth: 50
        type: HusButton.Type_Outlined
        text: root.__visible ? "隐藏" : "显示"
        onClicked: root.__visible = !root.__visible
    }
}
