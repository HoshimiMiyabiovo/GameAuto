import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import HuskarUI.Basic

HusWindow {
    id: mainWindow
    objectName: "mainWindow"

    width: 1400
    height: 900
    minimumWidth: 860
    minimumHeight: 600

    title: "游戏自动化助手"
    color: "#f6f1e8"

    // 全局字体缩放系数：窗口 1400px 时为 1.0，范围 [0.8, 1.25]
    property real fontScale: Math.max(0.8, Math.min(1.25, width / 1400))

    // ── 启用 Mica 毛玻璃特效 (Windows 11) ──────────────────────
    Component.onCompleted: {
        // 优先 Mica，失败回退 AcrylicMaterial，再失败保持默认
        if (!setSpecialEffect(HusWindow.Win_Mica)) {
            setSpecialEffect(HusWindow.Win_AcrylicMaterial)
        }
    }

    // ── Toast notification handler ────────────────────────────────
    Connections {
        target: bridge
        function onToastRequested(message, color) {
            // 根据 color 色调选择通知类型
            if (color === "#b04b44") {
                globalNotification.error("提示", message)
            } else if (color === "#b66f1f") {
                globalNotification.warning("提示", message)
            } else if (color === "#2f7d4a") {
                globalNotification.success("提示", message)
            } else {
                globalNotification.info("提示", message)
            }
        }
    }

    // ── Global notification overlay ───────────────────────────────
    HusNotification {
        id: globalNotification
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.margins: 20
        z: 999
    }

    // ── Root layout: sidebar + content (避开 captionBar) ─────────
    // captionBar 是 HusWindow 内置的窗口标题栏（高度 30），位于顶部 z=65535
    // 内容必须从 captionBar.bottom 开始，否则会被标题栏遮挡
    RowLayout {
        anchors.top: mainWindow.captionBar.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        spacing: 0

        // ==== SIDEBAR ====
        Rectangle {
            Layout.fillHeight: true
            Layout.preferredWidth: 240
            // 半透明侧边栏，让 Mica/Acrylic 毛玻璃透过
            color: Qt.rgba(255/255, 250/255, 242/255, 0.85)

            HusMenu {
                id: sidebarMenu
                anchors.fill: parent
                compactMode: HusMenu.Mode_Relaxed
                defaultMenuWidth: 240
                selectedKey: bridge.currentView

                initModel: [
                    { key: "automation",      label: "自动化运行",     iconSource: HusIcon.PlayCircleOutlined },
                    { key: "daily",            label: "每日进度",       iconSource: HusIcon.CalendarOutlined },
                    { key: "gacha",            label: "HoYo 抽卡分析",  iconSource: HusIcon.GiftOutlined },
                    { key: "nikki",            label: "无限暖暖共鸣",   iconSource: HusIcon.StarOutlined },
                    { key: "wuwa",             label: "鸣潮专项",       iconSource: HusIcon.ThunderboltOutlined },
                    { key: "tajiduo",          label: "塔吉多签到",     iconSource: HusIcon.CheckCircleOutlined },
                    { key: "endfield_center",  label: "终末地数据中心", iconSource: HusIcon.CloudServerOutlined }
                ]

                onClickMenu: function(deep, key, keyPath, data) {
                    if (data && data.key) {
                        bridge.switchView(data.key)
                    }
                }
            }
        }

        // ==== DIVIDER ====
        Rectangle {
            Layout.fillHeight: true
            Layout.preferredWidth: 1
            color: "#dbcdbd"
        }

        // ==== CONTENT AREA ====
        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.margins: 20

            // 所有视图保持加载，用 visible 切换，避免切换时丢失状态
            Loader {
                id: automationLoader
                anchors.fill: parent
                active: true
                visible: bridge.currentView === "automation" || bridge.currentView === ""
                source: "views/AutomationView.qml"
            }
            Loader {
                id: dailyLoader
                anchors.fill: parent
                active: true
                visible: bridge.currentView === "daily"
                source: "views/DailyView.qml"
            }
            Loader {
                id: gachaLoader
                anchors.fill: parent
                active: true
                visible: bridge.currentView === "gacha"
                source: "views/GachaView.qml"
            }
            Loader {
                id: nikkiLoader
                anchors.fill: parent
                active: true
                visible: bridge.currentView === "nikki"
                source: "views/NikkiView.qml"
            }
            Loader {
                id: wuwaLoader
                anchors.fill: parent
                active: true
                visible: bridge.currentView === "wuwa"
                source: "views/WuwaView.qml"
            }
            Loader {
                id: tajiduoLoader
                anchors.fill: parent
                active: true
                visible: bridge.currentView === "tajiduo"
                source: "views/TajiduoView.qml"
            }
            Loader {
                id: endfieldCenterLoader
                anchors.fill: parent
                active: true
                visible: bridge.currentView === "endfield_center"
                source: "views/EndfieldCenterView.qml"
            }
            Loader {
                id: setupWizardLoader
                anchors.fill: parent
                active: true
                visible: bridge.currentView === "setup_wizard"
                source: "views/SetupWizardView.qml"
            }
        }
    }
}
