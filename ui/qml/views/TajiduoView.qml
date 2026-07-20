import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import HuskarUI.Basic
import "../components"

// Tajiduo sign view
Item {
    id: root
    readonly property real fs: Application.window ? Application.window.fontScale : 1

    Component.onCompleted: bridge.loadCredentials()

    // 视图从隐藏→显示时重新灌一次凭据
    onVisibleChanged: {
        if (visible) bridge.loadCredentials()
    }

    Connections {
        target: bridge
        function onCredentialsChanged(jsonStr) {
            try {
                var creds = JSON.parse(jsonStr || "{}")
                var tj = creds.tajiduo_sign || {}
                if (tj.token) tajiduoTokenInput.text = tj.token
                if (tj.phone) tajiduoPhoneInput.text = tj.phone
                if (tj.password) tajiduoPasswordInput.text = tj.password
                bbsSignCheck.checked = !!tj.do_bbs_signin
                gameSignCheck.checked = !!tj.do_game_signin
                browseCheck.checked = !!tj.do_browse
                likeCheck.checked = !!tj.do_like
                shareCheck.checked = !!tj.do_share
                tajiduoStartupCheck.checked = !!tj.startup_sign
            } catch(e) { /* ignore */ }
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
                title: "🎯 塔吉多签到 (异环)"
                padding: 16

                contentItem: ColumnLayout {
                    spacing: 10

                    CredentialInput {
                        id: tajiduoTokenInput
                        Layout.fillWidth: true
                        placeholderText: "Token 或 Cookie..."
                        onEditingFinished: bridge.saveCredential("tajiduo_sign", "token", text)
                    }

                    HusInput {
                        id: tajiduoPhoneInput
                        Layout.fillWidth: true
                        placeholderText: "手机号 (Token 失效时用于自动重登)..."
                        onEditingFinished: bridge.saveCredential("tajiduo_sign", "phone", text)
                    }

                    CredentialInput {
                        id: tajiduoPasswordInput
                        Layout.fillWidth: true
                        placeholderText: "密码 (Token 失效时用于自动重登)..."
                        onEditingFinished: bridge.saveCredential("tajiduo_sign", "password", text)
                    }

                    RowLayout {
                        spacing: 10
                        HusButton {
                            text: "执行签到"
                            type: HusButton.Type_Primary
                            enabled: !bridge.isRunning
                            onClicked: {
                                if (tajiduoTokenInput.text.length > 0) {
                                    bridge.saveCredential("tajiduo_sign", "token", tajiduoTokenInput.text)
                                }
                                if (tajiduoPhoneInput.text.length > 0) {
                                    bridge.saveCredential("tajiduo_sign", "phone", tajiduoPhoneInput.text)
                                }
                                if (tajiduoPasswordInput.text.length > 0) {
                                    bridge.saveCredential("tajiduo_sign", "password", tajiduoPasswordInput.text)
                                }
                                bridge.runTajiduoSign()
                            }
                        }
                    }

                    Flow {
                        spacing: 10
                        Layout.fillWidth: true
                        HusCheckBox {
                            id: bbsSignCheck
                            text: "BBS 签到"
                            checked: true
                            onClicked: bridge.saveCredentialBool("tajiduo_sign", "do_bbs_signin", checked)
                        }
                        HusCheckBox {
                            id: gameSignCheck
                            text: "游戏签到"
                            checked: true
                            onClicked: bridge.saveCredentialBool("tajiduo_sign", "do_game_signin", checked)
                        }
                        HusCheckBox {
                            id: browseCheck
                            text: "浏览任务"
                            checked: false
                            onClicked: bridge.saveCredentialBool("tajiduo_sign", "do_browse", checked)
                        }
                        HusCheckBox {
                            id: likeCheck
                            text: "点赞任务"
                            checked: false
                            onClicked: bridge.saveCredentialBool("tajiduo_sign", "do_like", checked)
                        }
                        HusCheckBox {
                            id: shareCheck
                            text: "分享任务"
                            checked: false
                            onClicked: bridge.saveCredentialBool("tajiduo_sign", "do_share", checked)
                        }
                        HusCheckBox {
                            id: tajiduoStartupCheck
                            text: "启动时签到"
                            checked: true
                            onClicked: bridge.saveCredentialBool("tajiduo_sign", "startup_sign", checked)
                        }
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
