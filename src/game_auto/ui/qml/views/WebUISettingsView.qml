import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import HuskarUI.Basic
import "../components"

Item {
    id: root
    readonly property real fs: Application.window ? Application.window.fontScale : 1
    property var status: ({has_account:false, username:"", port:60171, url:"http://127.0.0.1:60171", running:false})

    function refresh(){
        try{
            var j = JSON.parse(bridge.getWebUIStatus() || "{}");
            root.status = j;
        }catch(e){ root.status = {has_account:false, username:"", port:60171, url:"http://127.0.0.1:60171", running:false} }
    }

    property double _lastRefreshTs: 0
    Component.onCompleted: { _lastRefreshTs = Date.now(); refresh() }
    onVisibleChanged: if(visible && Date.now() - _lastRefreshTs > 30000) { _lastRefreshTs = Date.now(); refresh() }

    Connections{
        target: bridge
        function onWebUIStatusChanged(jsonStr){
            try{ root.status = JSON.parse(jsonStr||"{}"); }catch(e){}
        }
    }

    ScrollView{
        anchors.fill: parent
        clip: true
        ColumnLayout{
            width: parent.width
            spacing: 14

            HusCard{
                Layout.fillWidth: true
                padding: 20
                contentItem: ColumnLayout{
                    spacing: 8
                    HusText{ text: "🌐 WebUI 远程控制"; font.pixelSize: Math.round(22*root.fs); font.bold:true }
                    HusText{
                        text: "WebUI 与桌面 GUI 共用同一自动化内核，端口 60171，仅本机可控。未设置账号时 WebUI 不会启动；设置后所有接口均需登录（DPAPI 加密落盘）。"
                        font.pixelSize: Math.round(13*root.fs); color:"#6d6257"; wrapMode: Text.Wrap; Layout.fillWidth:true
                    }
                    RowLayout{
                        spacing: 8
                        HusTag{ text: root.status.running ? "运行中" : "未运行"; tagState: root.status.running ? HusTag.State_Success : HusTag.State_Warning }
                        HusText{ text: root.status.url; font.pixelSize: Math.round(13*root.fs); color:"#295d86"; Layout.fillWidth:true; elide: Text.ElideRight }
                        HusButton{ text: "打开浏览器"; type: HusButton.Type_Primary; enabled: root.status.running; onClicked: bridge.openWebUIInBrowser() }
                        HusButton{ text: "刷新状态"; onClicked: refresh() }
                    }
                    HusText{
                        visible: !root.status.running
                        text: root.status.has_account
                              ? "提示：WebUI 随桌面 GUI 自动启动（后台线程 127.0.0.1:60171），若显示未运行请检查端口占用或重启 GUI。"
                              : "尚未设置账号：WebUI 未启动。保存账号后将立即启动，无需重启 GUI。"
                        font.pixelSize: Math.round(11*root.fs); color:"#b66f1f"; wrapMode: Text.Wrap; Layout.fillWidth:true
                    }
                }
            }

            HusCard{
                Layout.fillWidth: true
                title: "账号设置"
                padding: 16
                contentItem: ColumnLayout{
                    spacing: 10
                    HusText{ text: "未设置账号时 WebUI 不启动；设置后所有 /api/* 与 /ws/logs 均要求登录"; font.pixelSize: Math.round(12*root.fs); color:"#6d6257"; wrapMode: Text.Wrap; Layout.fillWidth:true }

                    RowLayout{
                        spacing: 8
                        HusText{ text: "当前账号："; font.pixelSize: Math.round(13*root.fs); color:"#2c241d" }
                        HusText{ text: root.status.has_account ? root.status.username : "未设置"; font.pixelSize: Math.round(13*root.fs); color: root.status.has_account ? "#2f7d4a" : "#b66f1f"; font.bold:true }
                        Item{ Layout.fillWidth:true }
                        HusTag{ visible: root.status.has_account; text: "已启用保护"; tagState: HusTag.State_Success }
                        HusTag{ visible: !root.status.has_account; text: "未设置（WebUI 未启动）"; tagState: HusTag.State_Warning }
                    }

                    HusText{ text: "账号"; font.pixelSize: Math.round(12*root.fs); color:"#6d6257" }
                    HusInput{ id: webUser; Layout.fillWidth:true; placeholderText: "3-32 字符，例：admin" }

                    HusText{ text: "密码"; font.pixelSize: Math.round(12*root.fs); color:"#6d6257" }
                    CredentialInput{ id: webPass; Layout.fillWidth:true; placeholderText: "6-64 字符，WebUI 登录用" }

                    HusText{
                        text: "密码经 Windows DPAPI 加密后存 app_config.json 的 webui_auth 段，跨用户不可解密；忘记可在本页重设。"
                        font.pixelSize: Math.round(11*root.fs); color:"#8a7a68"; wrapMode: Text.Wrap; Layout.fillWidth:true
                    }

                    RowLayout{
                        spacing: 8
                        HusButton{
                            text: "保存账号"
                            type: HusButton.Type_Primary
                            onClicked: {
                                if(!webUser.text.trim() || !webPass.text){ bridge.emit_event("toast",{message:"请填写账号与密码",color:"#b04b44"}); return; }
                                bridge.saveWebUIAccount(webUser.text, webPass.text);
                                webPass.text = "";
                            }
                        }
                        HusButton{
                            text: "清空"
                            onClicked: { webUser.text=""; webPass.text=""; }
                        }
                    }
                }
            }

            HusCard{
                Layout.fillWidth: true
                title: "网络安全说明"
                padding: 14
                contentItem: ColumnLayout{
                    spacing: 6
                    HusText{ text: "· 监听 127.0.0.1:60171，仅本机可控；CORS 仅放行 60171 端口；所有 /api/* 与 /ws/logs 均需登录"; font.pixelSize: Math.round(11*root.fs); color:"#6d6257"; wrapMode: Text.Wrap; Layout.fillWidth:true }
                    HusText{ text: "· 登录 60s 内限 5 次，失败 429；会话 12h 过期，支持 Bearer 与 HttpOnly Cookie 双通道"; font.pixelSize: Math.round(11*root.fs); color:"#6d6257"; wrapMode: Text.Wrap; Layout.fillWidth:true }
                    HusText{ text: "· WebUI 与 GUI 共用 AutomationController 单例，受 instance_lock 保护，不会多实例抢占"; font.pixelSize: Math.round(11*root.fs); color:"#6d6257"; wrapMode: Text.Wrap; Layout.fillWidth:true }
                }
            }
        }
    }
}
