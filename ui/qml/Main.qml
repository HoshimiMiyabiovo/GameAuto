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

    // ── 视图懒加载标志：切到过一次后保持 active，避免状态丢失 ──
    // 启动时只有当前视图加载，其他视图首次切换才加载，降低首屏内存占用
    property bool woolEverLoaded: false
    property bool dailyEverLoaded: false
    property bool gachaEverLoaded: false
    property bool nikkiEverLoaded: false
    property bool wuwaEverLoaded: false
    property bool tajiduoEverLoaded: false
    property bool endfieldCenterEverLoaded: false
    property bool setupWizardEverLoaded: false
    property bool clickSequenceEverLoaded: false
    property bool webUIEverLoaded: false

    // ── 性能优先：默认关闭 Mica/Acrylic 毛玻璃（5700X 切页卡顿），仅保留主题色 ──
    Component.onCompleted: {
        // 仅安装主题色，特效默认关闭减少 GPU 合成开销；需要毛玻璃可手动开启
        // if (!setSpecialEffect(HusWindow.Win_Mica)) { setSpecialEffect(HusWindow.Win_AcrylicMaterial) }
        HusTheme.installThemePrimaryColorBase("#18a05f")
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

            ColumnLayout {
                anchors.fill: parent
                spacing: 0

                // 品牌区
                Item {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 64

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 18
                        anchors.rightMargin: 12
                        spacing: 10

                        Rectangle {
                            Layout.preferredWidth: 34
                            Layout.preferredHeight: 34
                            radius: 10
                            gradient: Gradient {
                                GradientStop { position: 0; color: "#22b06a" }
                                GradientStop { position: 1; color: "#0e7a48" }
                            }
                            HusText {
                                anchors.centerIn: parent
                                text: "G"
                                color: "#ffffff"
                                font.pixelSize: 18
                                font.bold: true
                            }
                        }
                        ColumnLayout {
                            spacing: 0
                            HusText {
                                text: "GameAuto"
                                font.pixelSize: 16
                                font.bold: true
                                color: "#2c241d"
                            }
                            HusText {
                                text: "游戏自动化助手"
                                font.pixelSize: 11
                                color: "#6d6257"
                            }
                        }
                    }
                }

                // 品牌区分隔线
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 1
                    color: "#33dbcdbd"
                }

                HusMenu {
                    id: sidebarMenu
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    compactMode: HusMenu.Mode_Relaxed
                    defaultMenuWidth: 240
                    selectedKey: bridge.currentView

                    initModel: [
                        { key: "automation",      label: "自动化运行",     iconSource: HusIcon.PlayCircleOutlined },
                        { key: "wool",             label: "薅羊毛集合",     iconSource: HusIcon.WalletOutlined },
                        { key: "daily",            label: "每日进度",       iconSource: HusIcon.CalendarOutlined },
                        { key: "gacha",            label: "HoYo 抽卡分析",  iconSource: HusIcon.GiftOutlined },
                        { key: "nikki",            label: "无限暖暖共鸣",   iconSource: HusIcon.StarOutlined },
                        { key: "wuwa",             label: "鸣潮专项",       iconSource: HusIcon.ThunderboltOutlined },
                        { key: "tajiduo",          label: "塔吉多签到",     iconSource: HusIcon.CheckCircleOutlined },
                        { key: "endfield_center",  label: "终末地数据中心", iconSource: HusIcon.CloudServerOutlined },
                        { key: "click_sequence",   label: "点击录制",       iconSource: HusIcon.VideoCameraOutlined },
                        { key: "webui",            label: "WebUI 远程",     iconSource: HusIcon.LinkOutlined },
                        { key: "setup_wizard",     label: "路径配置",       iconSource: HusIcon.SettingOutlined }
                    ]

                    onClickMenu: function(deep, key, keyPath, data) {
                        if (data && data.key) {
                            bridge.switchView(data.key)
                        }
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

            // 视图懒加载：active 仅当"当前视图"或"曾经加载过"时为 true
            // 首屏只实例化当前视图，其他视图首次切换才加载；加载后保持 active 避免状态丢失
            Loader {
                id: automationLoader
                anchors.fill: parent
                active: true
                asynchronous: true
                visible: bridge.currentView === "automation" || bridge.currentView === ""
                source: "views/AutomationView.qml"
            }
            Loader {
                id: woolLoader
                anchors.fill: parent
                asynchronous: true
                active: bridge.currentView === "wool" || mainWindow.woolEverLoaded
                visible: bridge.currentView === "wool"
                source: "views/WoolView.qml"
                onLoaded: mainWindow.woolEverLoaded = true
            }
            Loader {
                id: dailyLoader
                anchors.fill: parent
                asynchronous: true
                active: bridge.currentView === "daily" || mainWindow.dailyEverLoaded
                visible: bridge.currentView === "daily"
                source: "views/DailyView.qml"
                onLoaded: mainWindow.dailyEverLoaded = true
            }
            Loader {
                id: gachaLoader
                anchors.fill: parent
                asynchronous: true
                active: bridge.currentView === "gacha" || mainWindow.gachaEverLoaded
                visible: bridge.currentView === "gacha"
                source: "views/GachaView.qml"
                onLoaded: mainWindow.gachaEverLoaded = true
            }
            Loader {
                id: nikkiLoader
                anchors.fill: parent
                asynchronous: true
                active: bridge.currentView === "nikki" || mainWindow.nikkiEverLoaded
                visible: bridge.currentView === "nikki"
                source: "views/NikkiView.qml"
                onLoaded: mainWindow.nikkiEverLoaded = true
            }
            Loader {
                id: wuwaLoader
                anchors.fill: parent
                asynchronous: true
                active: bridge.currentView === "wuwa" || mainWindow.wuwaEverLoaded
                visible: bridge.currentView === "wuwa"
                source: "views/WuwaView.qml"
                onLoaded: mainWindow.wuwaEverLoaded = true
            }
            Loader {
                id: tajiduoLoader
                anchors.fill: parent
                asynchronous: true
                active: bridge.currentView === "tajiduo" || mainWindow.tajiduoEverLoaded
                visible: bridge.currentView === "tajiduo"
                source: "views/TajiduoView.qml"
                onLoaded: mainWindow.tajiduoEverLoaded = true
            }
            Loader {
                id: endfieldCenterLoader
                anchors.fill: parent
                asynchronous: true
                active: bridge.currentView === "endfield_center" || mainWindow.endfieldCenterEverLoaded
                visible: bridge.currentView === "endfield_center"
                source: "views/EndfieldCenterView.qml"
                onLoaded: mainWindow.endfieldCenterEverLoaded = true
            }
            Loader {
                id: clickSequenceLoader
                anchors.fill: parent
                active: bridge.currentView === "click_sequence" || mainWindow.clickSequenceEverLoaded
                visible: bridge.currentView === "click_sequence"
                source: "views/ClickSequenceView.qml"
                onLoaded: mainWindow.clickSequenceEverLoaded = true
            }
            Loader {
                id: setupWizardLoader
                anchors.fill: parent
                asynchronous: true
                active: bridge.currentView === "setup_wizard" || mainWindow.setupWizardEverLoaded
                visible: bridge.currentView === "setup_wizard"
                source: "views/SetupWizardView.qml"
                onLoaded: mainWindow.setupWizardEverLoaded = true
            }
            Loader {
                id: webUILoader
                anchors.fill: parent
                asynchronous: true
                active: bridge.currentView === "webui" || mainWindow.webUIEverLoaded
                visible: bridge.currentView === "webui"
                source: "views/WebUISettingsView.qml"
                onLoaded: mainWindow.webUIEverLoaded = true
            }
        }
    }
}
