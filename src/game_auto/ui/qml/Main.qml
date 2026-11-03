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
    property bool toolManagerEverLoaded: false
    property bool staminaConfigEverLoaded: false

    // ── 重页面内存回收（2026-11-01）──
    // 视图加载后常驻会累积：实测把 10 个页面切一遍 +142MB 且不回落。
    // 这里在「停止切页 viewReleaseDelayMs」之后，把**非当前**的重页面释放掉
    // （active=false → Loader 销毁 item），切回时重建（0.2~1s，滚动位置会丢）。
    // 只挑重页面（每日/抽卡/暖暖/鸣潮/刷本配置，实测这五个就 +97MB），
    // 轻页面保持常驻，保住切页手感。
    // 任务运行中不释放：DailyView 有轮询、GachaView 可能在导入，销毁会中断。
    //
    // 实测口径（离屏，2026-11-01）：Windows 堆不会把释放的内存立刻还给系统，
    // 所以「切一圈停住」时任务管理器看不出下降；收益体现在长期不再为不常用页面
    // 保留对象/纹理，以及后续分配可复用。**频繁来回切同一页反而更费**（反复重建），
    // 那种用法请把 viewReleaseDelayMs 设为 0（= 关闭回收，恢复旧的常驻行为）。
    // 重建实测要 1~2 秒（离屏软件渲染；真实 GPU 下通常更快），所以默认 3 分钟：
    // 只有「真的离开这一页很久」才释放，避免正常来回切页被拖慢。
    property int viewReleaseDelayMs: 180000

    function touchViewActivity() {
        viewReleaseTimer.restart()
    }

    function releaseInactiveHeavyViews() {
        if (viewReleaseDelayMs <= 0) return   // 0 = 关闭回收
        if (bridge.isRunning) {
            viewReleaseTimer.restart()
            return
        }
        var cur = bridge.currentView
        if (cur !== "daily") dailyEverLoaded = false
        if (cur !== "gacha") gachaEverLoaded = false
        if (cur !== "nikki") nikkiEverLoaded = false
        if (cur !== "wuwa") wuwaEverLoaded = false
        if (cur !== "stamina_config") staminaConfigEverLoaded = false
    }

    Timer {
        id: viewReleaseTimer
        interval: mainWindow.viewReleaseDelayMs
        repeat: false
        onTriggered: mainWindow.releaseInactiveHeavyViews()
    }

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
                        { key: "stamina_config",  label: "脚本刷本配置",   iconSource: HusIcon.SettingOutlined },
                        { key: "tool_manager",    label: "脚本管理",       iconSource: HusIcon.AppstoreOutlined },
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
                id: staminaConfigLoader
                objectName: "staminaConfigLoader"
                anchors.fill: parent
                asynchronous: true
                active: bridge.currentView === "stamina_config" || mainWindow.staminaConfigEverLoaded
                visible: bridge.currentView === "stamina_config"
                source: "views/StaminaConfigView.qml"
                onLoaded: mainWindow.staminaConfigEverLoaded = true
                onVisibleChanged: mainWindow.touchViewActivity()   // 切页后延迟释放非当前页
            }
            Loader {
                id: dailyLoader
                objectName: "dailyLoader"
                anchors.fill: parent
                asynchronous: true
                active: bridge.currentView === "daily" || mainWindow.dailyEverLoaded
                visible: bridge.currentView === "daily"
                source: "views/DailyView.qml"
                onLoaded: mainWindow.dailyEverLoaded = true
                onVisibleChanged: mainWindow.touchViewActivity()   // 切页后延迟释放非当前页
            }
            Loader {
                id: gachaLoader
                objectName: "gachaLoader"
                anchors.fill: parent
                asynchronous: true
                active: bridge.currentView === "gacha" || mainWindow.gachaEverLoaded
                visible: bridge.currentView === "gacha"
                source: "views/GachaView.qml"
                onLoaded: mainWindow.gachaEverLoaded = true
                onVisibleChanged: mainWindow.touchViewActivity()   // 切页后延迟释放非当前页
            }
            Loader {
                id: nikkiLoader
                objectName: "nikkiLoader"
                anchors.fill: parent
                asynchronous: true
                active: bridge.currentView === "nikki" || mainWindow.nikkiEverLoaded
                visible: bridge.currentView === "nikki"
                source: "views/NikkiView.qml"
                onLoaded: mainWindow.nikkiEverLoaded = true
                onVisibleChanged: mainWindow.touchViewActivity()   // 切页后延迟释放非当前页
            }
            Loader {
                id: wuwaLoader
                objectName: "wuwaLoader"
                anchors.fill: parent
                asynchronous: true
                active: bridge.currentView === "wuwa" || mainWindow.wuwaEverLoaded
                visible: bridge.currentView === "wuwa"
                source: "views/WuwaView.qml"
                onLoaded: mainWindow.wuwaEverLoaded = true
                onVisibleChanged: mainWindow.touchViewActivity()   // 切页后延迟释放非当前页
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
                id: toolManagerLoader
                anchors.fill: parent
                asynchronous: true
                active: bridge.currentView === "tool_manager" || mainWindow.toolManagerEverLoaded
                visible: bridge.currentView === "tool_manager"
                source: "views/ToolManagerView.qml"
                onLoaded: mainWindow.toolManagerEverLoaded = true
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
