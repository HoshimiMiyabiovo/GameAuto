import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import HuskarUI.Basic
import "../components"

// Tajiduo sign + daily task data view
Item {
    id: root
    readonly property real fs: Application.window ? Application.window.fontScale : 1

    // ── 每日任务数据 ──
    property var dailyData: ({})
    property bool dailyLoading: false

    Component.onCompleted: bridge.loadCredentials()

    property double _lastCredTs: 0
    onVisibleChanged: {
        if (visible && Date.now() - _lastCredTs > 30000) {
            _lastCredTs = Date.now()
            bridge.loadCredentials()
        }
    }

    Connections {
        target: bridge
        function onCredentialsChanged(jsonStr) {
            try {
                var creds = JSON.parse(jsonStr || "{}")
                var tj = creds.tajiduo_sign || {}
                if (tj.token) tajiduoTokenInput.text = tj.token
                if (tj.deviceid) tajiduoDeviceIdInput.text = tj.deviceid
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
        function onTajiduoDailyChanged(jsonStr) {
            try { root.dailyData = JSON.parse(jsonStr || "{}") }
            catch(e) { root.dailyData = ({}) }
            root.dailyLoading = false
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

                    // Device ID 是签到与数据查询的必需参数（Flet 版有此输入框，QML 之前漏了）
                    CredentialInput {
                        id: tajiduoDeviceIdInput
                        Layout.fillWidth: true
                        placeholderText: "Device ID（必填，抓包或 app_config.json 中已有时会自动带入）..."
                        onEditingFinished: bridge.saveCredential("tajiduo_sign", "deviceid", text)
                    }

                    CredentialInput {
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

            // ── 每日任务数据（复用签到 token + deviceid）──
            HusCard {
                Layout.fillWidth: true
                title: "📊 每日任务数据"
                padding: 16

                contentItem: ColumnLayout {
                    spacing: 10

                    HusText {
                        text: "复用签到的 Token + Device ID 查询社区每日任务进度，无需额外凭据。"
                        font.pixelSize: Math.round(12 * root.fs)
                        color: "#6d6257"
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }

                    HusButton {
                        Layout.fillWidth: true
                        text: root.dailyLoading ? "查询中..." : "查询每日任务"
                        type: HusButton.Type_Primary
                        enabled: !bridge.isRunning && !root.dailyLoading
                        onClicked: {
                            root.dailyLoading = true
                            bridge.queryTajiduoDaily()
                        }
                    }

                    // 失败提示
                    Rectangle {
                        Layout.fillWidth: true
                        visible: root.dailyData.ok === false
                        height: errColumn.height + 20
                        color: "#f7dfdd"
                        radius: 6
                        border.color: "#b04b44"
                        border.width: 1
                        ColumnLayout {
                            id: errColumn
                            anchors.fill: parent
                            anchors.margins: 10
                            spacing: 4
                            HusText {
                                text: "✗ 查询失败"
                                font.pixelSize: Math.round(13 * root.fs)
                                font.bold: true
                                color: "#b04b44"
                            }
                            HusText {
                                text: root.dailyData.message || ""
                                font.pixelSize: Math.round(12 * root.fs)
                                color: "#2f4f4f"
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }
                        }
                    }

                    // 任务列表（带进度条）
                    Repeater {
                        model: root.dailyData.ok === true ? (root.dailyData.items || []) : []

                        delegate: ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 4
                            required property var modelData

                            RowLayout {
                                Layout.fillWidth: true
                                HusText {
                                    text: (modelData.done ? "✓ " : "○ ") + modelData.name
                                    font.pixelSize: Math.round(13 * root.fs)
                                    color: modelData.done ? "#2f7d4a" : "#2f4f4f"
                                    Layout.fillWidth: true
                                }
                                HusText {
                                    text: modelData.current + "/" + modelData.total
                                    font.pixelSize: Math.round(13 * root.fs)
                                    font.bold: true
                                    color: modelData.done ? "#2f7d4a" : "#b66f1f"
                                }
                            }

                            Rectangle {
                                Layout.fillWidth: true
                                height: 6
                                radius: 3
                                color: "#ece5da"
                                Rectangle {
                                    width: modelData.total > 0
                                           ? parent.width * modelData.current / modelData.total
                                           : 0
                                    height: parent.height
                                    radius: 3
                                    color: modelData.done ? "#2f7d4a" : "#e0a33e"
                                }
                            }
                        }
                    }

                    HusText {
                        visible: root.dailyData.ok === true
                                 && (root.dailyData.items || []).length === 0
                        text: "未获取到每日任务（可能未配置或接口无数据）"
                        font.pixelSize: Math.round(12 * root.fs)
                        color: "#8a7f72"
                    }

                    HusText {
                        visible: root.dailyData.ok === true && root.dailyData.all_done === true
                        text: "每日任务已全部完成"
                        font.pixelSize: Math.round(13 * root.fs)
                        font.bold: true
                        color: "#2f7d4a"
                    }
                }
            }
        }
    }
}
