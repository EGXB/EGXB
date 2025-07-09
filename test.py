import os
import sys
import tempfile
import subprocess
import asyncio
import requests
import json
import serial
import threading
import time

from PySide6.QtGui import QPixmap
from PySide6.QtCore import Qt, Signal, QDate, QTimer, QDateTime, QTime
from PySide6.QtGui import QFont, QIcon, QTextCursor, QTextCharFormat, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QStackedWidget, QVBoxLayout,
    QHBoxLayout, QPushButton, QLabel, QCheckBox, QFrame, QGroupBox,
    QGridLayout, QCalendarWidget, QDateEdit, QListWidget, QListWidgetItem,
    QMessageBox, QInputDialog, QTextEdit, QLineEdit, QDialog, QDialogButtonBox,
    QFormLayout, QTimeEdit
)

# 全局配置
CONFIG = {
    'poll_interval': 1,                 # 云指令轮询间隔(秒)
    'cmd_timeout': 60,                  # 系统命令执行超时时间(秒)
    'cloud_enabled': True,              # 是否启用云服务
    'serial_enabled': True,             # 是否启用串口监听
    'serial_port': '/dev/ttyS9',        # 串口设备
    'serial_baudrate': 9600,            # 串口波特率

    'wx_cloud': {                       # 微信云开发配置
        'env_id': 'cloud1-5gxtsztod863880c',
        'access_token': '',             # 访问令牌(动态刷新)
        'api_url': 'https://api.weixin.qq.com/tcb/databasequery',
        'appid': '',
        'secret': ''
    }
}

# 设备状态管理
device_state = {
    'last_command': {                   # 最近执行的指令
        'id': None,                     # 指令ID
        'content': None,                # 指令内容
        'timestamp': 0                  # 执行时间戳
    }
}

def create_styled_button(text, color, hover_color, width=None,height=None):
    """创建统一风格的按钮"""
    btn = QPushButton(text)
    btn.setStyleSheet(f"""
        QPushButton {{
            background-color:  {color};
            color: white;
            border: none;
            padding: 8px;
            border-radius: 4px;
            font-weight: bold;
        }}
        QPushButton:hover {{
            background-color: {hover_color};
        }}
        QPushButton:pressed {{
            background-color: {color};
            border: 2px solid rgba(255, 255, 255, 0.5);
        }}
    """)
    if width:
        btn.setFixedWidth(width)
    if height:
        btn.setFixedHeight(height)
    return btn
def create_transparent_button(text, width=None):
    """创建透明按钮 - 用于桌面主界面"""
    btn = QPushButton(text)
    btn.setStyleSheet("""
        QPushButton {
            background-color: transparent;
            color: white;
            border: none;
            padding: 8px;
            border-radius: 4px;
            font-weight: bold;
        }
        QPushButton:hover {
            background-color: rgba(255, 255, 255, 0.2);
        }
    """)
    if width:
        btn.setFixedWidth(width)
    return btn

class BackgroundService:
    """后台服务管理类"""
    def __init__(self):
        self.running = True
        self.serial_thread = None
        self.cloud_thread = None
        
    def start(self):
        """启动后台服务"""
        self.running = True
        
        # 启动串口监听线程
        self.serial_thread = threading.Thread(target=self.run_serial_listener, daemon=True)
        self.serial_thread.start()
        
        # 启动云服务线程
        self.cloud_thread = threading.Thread(target=self.run_cloud_service, daemon=True)
        self.cloud_thread.start()
        
    def stop(self):
        """停止后台服务"""
        self.running = False
        
    def run_serial_listener(self):
        """运行串口监听"""
        print("串口监听服务已启动")
        try:
            ser = serial.Serial(
                port=CONFIG['serial_port'],
                baudrate=CONFIG['serial_baudrate'],
                timeout=1  # 设置超时时间避免阻塞
            )
            
            while self.running and CONFIG['serial_enabled']:
                if ser.in_waiting > 0:
                    data = ser.readline().decode('utf-8', errors='ignore').strip()
                    print(f"收到串口数据: {data}")
                    
                    if data == 'YYTH':
                        # 启动语音通话脚本
                        script_path = '/home/elf/main/voice_communicate.py'
                        try:
                            subprocess.Popen(
                                [sys.executable, script_path],
                                cwd='/home/elf/main'
                            )
                            print(f"已启动语音通话脚本: {script_path}")
                        except Exception as e:
                            print(f"启动语音通话脚本失败: {str(e)}")
                    elif data == 'ZTJC':
                        # 启动坐姿检测脚本
                        script_path = '/home/elf/main/微信小程序+语音+坐姿1 .py'
                        try:
                            subprocess.Popen(
                                [sys.executable, script_path],
                                cwd='/home/elf/main'
                            )
                            print(f"已启动坐姿检测脚本: {script_path}")
                        except Exception as e:
                            print(f"启动坐姿检测脚本失败: {str(e)}")
                
                time.sleep(0.1)  # 避免过高CPU占用
                
        except serial.SerialException as e:
            print(f"串口错误: {str(e)}")
        except Exception as e:
            print(f"串口监听异常: {str(e)}")
        finally:
            if 'ser' in locals() and ser.is_open:
                ser.close()
                print("串口已关闭")
    
    def run_cloud_service(self):
        """运行云服务"""
        print("云指令轮询服务已启动")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.cloud_service_loop())
    
    async def cloud_service_loop(self):
        """云服务主循环"""
        while self.running and CONFIG['cloud_enabled']:
            commands = await self.poll_cloud_commands()
            for cmd in commands:
                await self.execute_command(cmd)
            await asyncio.sleep(CONFIG['poll_interval'])
    
    async def refresh_token(self):
        """刷新微信云开发访问令牌"""
        try:
            res = requests.get(
                "https://api.weixin.qq.com/cgi-bin/token",
                params={
                    'grant_type': 'client_credential',
                    'appid': CONFIG['wx_cloud']['appid'],
                    'secret': CONFIG['wx_cloud']['secret']
                }
            )
            res.raise_for_status()
            CONFIG['wx_cloud']['access_token'] = res.json()['access_token']
            print(f"Token刷新成功")
        except Exception as e:
            print(f"Token刷新失败: {e}")
            CONFIG['wx_cloud']['access_token'] = ''
    
    async def poll_cloud_commands(self):
        """从微信云开发查询最新指令"""
        if not CONFIG['wx_cloud']['access_token']:
            await self.refresh_token()
    
        try:
            # 查询最新指令(按时间戳倒序)
            query = {
                "env": CONFIG['wx_cloud']['env_id'],
                "query": "db.collection('commands')"
                         ".orderBy('timestamp', 'desc')"
                         ".limit(1)"
                         ".get()"
            }
    
            res = requests.post(
                f"{CONFIG['wx_cloud']['api_url']}?access_token={CONFIG['wx_cloud']['access_token']}",
                json=query,
                timeout=10
            )
            res.raise_for_status()
    
            # 解析查询结果
            data = res.json().get('data', [])
            if not data:
                return []
    
            # 处理查询结果格式
            latest = json.loads(data[0]) if isinstance(data[0], str) else data[0]
            return [latest] if isinstance(latest, dict) else []
    
        except Exception as e:
            print(f"查询指令失败: {e}")
            return []
    
    async def mark_command_executed(self, cmd_id):
        """标记云指令为已执行状态"""
        try:
            requests.post(
                f"{CONFIG['wx_cloud']['api_url']}?access_token={CONFIG['wx_cloud']['access_token']}",
                json={
                    "env": CONFIG['wx_cloud']['env_id'],
                    "query": f"db.collection('commands').doc('{cmd_id}').update({{data:{{status:'completed'}}}})"
                }
            )
        except Exception:
            pass
    
    def is_duplicate_command(self, command_data: dict) -> bool:
        """检查是否为重复指令"""
        current_cmd_id = command_data.get('_id', '')
        current_cmd = command_data.get('command', '')
        
        # 如果指令ID和内容都与上次相同，则认为是重复指令
        if (current_cmd_id == device_state['last_command']['id'] and 
            current_cmd == device_state['last_command']['content']):
            print(f"跳过重复指令: {current_cmd}")
            return True
        return False
    
    async def execute_command(self, command_data):
        """执行Python脚本指令"""
        command = command_data.get('command', '')
        cmd_id = command_data.get('_id', '')
        
        # 检查是否为重复指令
        if self.is_duplicate_command(command_data):
            return
    
        # 只处理Python脚本指令
        if command.startswith("python:"):
            script_path = command[7:].strip()
            try:
                # 关键修改：直接让子进程继承父进程的标准输出/错误
                process = subprocess.Popen(
                    [sys.executable, script_path],
                    cwd = '/home/elf/main'
                )
                print(f"开始执行Python脚本: {script_path}")
                
                # 等待进程结束（带超时）
                try:
                    process.wait(timeout=CONFIG['cmd_timeout'])
                    returncode = process.poll()
                    if returncode == 0:
                        print(f"Python脚本执行成功: {script_path}")
                    else:
                        print(f"Python脚本执行失败，返回码: {returncode}")
                except subprocess.TimeoutExpired:
                    process.kill()
                    print("Python脚本执行超时，已终止")
            except Exception as e:
                print(f"执行Python脚本出错: {str(e)}")
            
            # 更新最后执行的指令信息
            device_state['last_command'] = {
                'id': cmd_id,
                'content': command,
                'timestamp': asyncio.get_event_loop().time()
            }
    
            # 标记云指令为已执行
            if cmd_id:
                await self.mark_command_executed(cmd_id)

class DesktopApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("智能桌面系统")
        
        # 创建临时文档目录
        self.documents_dir = tempfile.mkdtemp(prefix="desktop_docs_")

        # 创建堆叠窗口管理不同界面
        self.stacked_widget = QStackedWidget()
        self.setCentralWidget(self.stacked_widget)

        # 创建各个界面
        self.desktop_screen = DesktopScreen(self)
        self.app_screen = AppScreen(self, self.documents_dir)
        self.settings_screen = SettingsScreen(self)

        # 添加界面到堆叠
        self.stacked_widget.addWidget(self.desktop_screen)
        self.stacked_widget.addWidget(self.app_screen)
        self.stacked_widget.addWidget(self.settings_screen)

        # 连接信号
        self.desktop_screen.appClicked.connect(self.show_app_screen)
        self.desktop_screen.settingsClicked.connect(self.show_settings_screen)
        self.app_screen.backClicked.connect(self.show_desktop_screen)
        self.settings_screen.backClicked.connect(self.show_desktop_screen)
        
        # 启动后台服务
        self.background_service = BackgroundService()
        self.background_service.start()

    def show_desktop_screen(self):
        self.stacked_widget.setCurrentWidget(self.desktop_screen)

    def show_app_screen(self, app_name):
        self.app_screen.set_app_name(app_name)
        self.stacked_widget.setCurrentWidget(self.app_screen)

    def show_settings_screen(self):
        self.stacked_widget.setCurrentWidget(self.settings_screen)

    def closeEvent(self, event):
        """清理临时文件目录并停止后台服务"""
        import shutil
        try:
            shutil.rmtree(self.documents_dir)
        except Exception as e:
            print(f"清理文档目录错误: {e}")
            
        # 停止后台服务
        self.background_service.stop()
        event.accept()


class DesktopScreen(QWidget):
    appClicked = Signal(str)
    settingsClicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # 初始化实例属性
        self.time_label = None
        self.timer = None
        self.setup_ui()

    def open_posture_detector(self):
        """打开坐姿检测系统外部文件"""
        try:
            # 使用绝对路径
            script_path = '/home/elf/main/微信小程序+语音+坐姿1 .py'
            subprocess.Popen([sys.executable, script_path], cwd='/home/elf/main')
            print(f"已启动坐姿检测脚本: {script_path}")
        except Exception as e:
            print(f"启动坐姿检测脚本失败: {str(e)}")

    def open_voice_assistant(self):
        """打开语音AI助手外部文件"""
        try:
            # 使用绝对路径
            script_path = '/home/elf/main/voice_communicate.py'
            subprocess.Popen([sys.executable, script_path], cwd='/home/elf/main')
            print(f"已启动语音通话脚本: {script_path}")
        except Exception as e:
            print(f"启动语音通话脚本失败: {str(e)}")

    def setup_ui(self):
        main_layout = QVBoxLayout()
        main_layout.setSpacing(0)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # 使用 QLabel 作为背景容器
        background_label = QLabel()
        background_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # 加载背景图片
        background_image = QPixmap("/home/elf/main/屏幕背景.jpg")
        background_label.setPixmap(background_image.scaled(
            self.size(), 
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation
        ))
        background_label.setScaledContents(True)  # 自动缩放填充
        # 创建桌面内容容器（透明背景）
        desktop_container = QWidget()
        desktop_container.setStyleSheet("background-color: transparent;")
        desktop_layout = QGridLayout(desktop_container)
        desktop_layout.setContentsMargins(30, 30, 30, 30)
        desktop_layout.setVerticalSpacing(20)
        desktop_layout.setHorizontalSpacing(20)

        # 添加大型时间显示
        self.time_label = QLabel()
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.time_label.setStyleSheet("""
            color: black;
            font-size: 40px;
            font-weight: bold;
            padding: 12px;
            background-color: transparent;
            border-radius: 12px;
        """)
        desktop_layout.addWidget(self.time_label, 0, 0, 1, 3, Qt.AlignmentFlag.AlignCenter)  # 跨3列

        # 桌面图标区域 - 使用网格布局，每行3个图标
        icons_layout = QGridLayout()
        icons_layout.setContentsMargins(0, 0, 0, 0)
        icons_layout.setVerticalSpacing(20)  # 减少垂直间距
        icons_layout.setHorizontalSpacing(20)  # 减少水平间距

        # 添加桌面图标 - 使用不透明按钮
        self.add_desktop_icon(icons_layout, 0, 0, "日历", "📅", "日历")
        self.add_desktop_icon(icons_layout, 0, 1, "文档", "📄", "文档")
        self.add_desktop_icon(icons_layout, 0, 2, "记事本", "📝", "记事本")
        self.add_desktop_icon(icons_layout, 1, 0, "语音AI助手", "🎤", "智能语音助手")
        self.add_desktop_icon(icons_layout, 1, 1, "坐姿检测", "🧘", "坐姿检测系统")
        self.add_desktop_icon(icons_layout, 1, 2, "设置", "⚙️", "系统设置", is_settings=True)

        # 添加到桌面布局
        desktop_layout.addLayout(icons_layout, 1, 0, 1, 3, Qt.AlignmentFlag.AlignCenter)  # 跨3列

        # 将内容容器添加到背景
        overlay_layout = QVBoxLayout(background_label)
        overlay_layout.addWidget(desktop_container)

        main_layout.addWidget(background_label)
        self.setLayout(main_layout)

        # 更新时间
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_time)
        self.timer.start(1000)  # 每秒更新
        self.update_time()  # 初始时间



        self.setLayout(main_layout)

    def update_time(self):
        """更新时间显示"""
        current_time = QDateTime.currentDateTime().toString("yyyy年MM月dd日 dddd\nhh:mm:ss")
        self.time_label.setText(current_time)

    def add_desktop_icon(self, layout, row, col, name, icon, label, is_settings=False):
        # 使用按钮作为图标容器
        btn = QPushButton()
        btn.setFixedSize(90, 90)  # 缩小图标尺寸
        btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: white;
                border: none;
                border-radius: 8px;
                font-size: 15px;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 0.2);
            }
        """)

        # 创建容器并设置布局
        container = QWidget(btn)
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 5, 0, 5)
        container_layout.setSpacing(0)

        # 添加图标
        icon_label = QLabel(f"<div style='font-size: 24px;'>{icon}</div>")
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setStyleSheet("background: transparent;")
        container_layout.addWidget(icon_label, 0, Qt.AlignmentFlag.AlignCenter)

        # 添加标签
        text_label = QLabel(label)
        text_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text_label.setWordWrap(True)
        text_label.setStyleSheet("""
            color: black; 
            font-size: 15px;
            background-color: transparent;
            padding: 2px;
        """)
        container_layout.addWidget(text_label, 0, Qt.AlignmentFlag.AlignCenter)

        # 设置按钮布局
        btn.setLayout(container_layout)

        if name == "坐姿检测":
            # 特殊处理坐姿检测按钮
            btn.clicked.connect(self.open_posture_detector)
        elif name == "语音AI助手":
            btn.clicked.connect(self.open_voice_assistant)
        elif is_settings:
            btn.clicked.connect(self.settingsClicked.emit)
        else:
            btn.clicked.connect(lambda: self.appClicked.emit(name))

        layout.addWidget(btn, row, col, Qt.AlignmentFlag.AlignCenter)


class AppScreen(QWidget):
    backClicked = Signal()

    def __init__(self, parent=None, documents_dir=None):
        super().__init__(parent)
        self.documents_dir = documents_dir

        # 初始化所有实例属性
        self.title_label = None
        self.content_area = None
        self.content_layout = None
        self.content_label = None
        self.calendar_container = None
        self.date_selector = None
        self.calendar = None
        self.documents_container = None
        self.new_file_btn = None
        self.open_file_btn = None
        self.delete_file_btn = None
        self.refresh_btn = None
        self.file_list = None
        self.file_preview = None
        self.voice_container = None
        self.sample_btn = None
        self.notepad_container = None  # 新增记事本容器
        self.notepad_text = None  # 新增记事本文本编辑区
        self.save_btn = None  # 新增保存按钮

        # 日历应用相关属性
        self.event_list = None
        self.events = {}
        self.reminder_timer = None

        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 10, 20, 20)
        # 应用标题栏
        title_bar = QHBoxLayout()
        # 修改返回按钮 - 使用带颜色的样式
        back_btn = create_styled_button("← 返回桌面", "#3498db", "#2980b9", height=35)
        back_btn.clicked.connect(self.backClicked.emit)

        self.title_label = QLabel()
        self.title_label.setFont(QFont("Arial", 16, QFont.Weight.Bold))
        self.title_label.setStyleSheet("color: #2c3e50;")

        title_bar.addWidget(back_btn)
        title_bar.addWidget(self.title_label)
        title_bar.addStretch()

        # 应用内容区域
        self.content_area = QFrame()
        self.content_area.setStyleSheet("""
            background-color: white;
            border-radius: 8px;
            padding: 20px;
            border: 1px solid #bdc3c7;
        """)
        self.content_layout = QVBoxLayout(self.content_area)

        # 默认内容
        self.content_label = QLabel()
        self.content_label.setWordWrap(True)
        self.content_label.setFont(QFont("Arial", 12))
        self.content_layout.addWidget(self.content_label)

        # 添加到主布局
        layout.addLayout(title_bar)
        layout.addWidget(self.content_area, 1)

        self.setLayout(layout)
        self.setStyleSheet("background-color: #ecf0f1;")

        # 预创建应用组件
        self.create_calendar_app()
        self.create_documents_app()
        self.create_notepad_app()  # 新增记事本应用

    def create_calendar_app(self):
        """创建日历应用组件"""
        self.calendar_container = QWidget()
        calendar_layout = QVBoxLayout(self.calendar_container)

        # 日期选择器
        self.date_selector = QDateEdit()
        self.date_selector.setDate(QDate.currentDate())
        self.date_selector.setCalendarPopup(True)
        self.date_selector.setStyleSheet("""
            QDateEdit {
                padding: 8px;
                font-size: 14px;
                border: 1px solid #bdc3c7;
                border-radius: 4px;
            }
        """)

        # 日历控件
        self.calendar = QCalendarWidget()
        self.calendar.setGridVisible(True)
        self.calendar.setStyleSheet("""
            QCalendarWidget {
                border: 1px solid #bdc3c7;
                border-radius: 4px;
            }
            QCalendarWidget QToolButton {
                height: 30px;
                font-size: 14px;
                color: #2c3e50;
            }
            QCalendarWidget QMenu {
                background-color: white;
            }
            QCalendarWidget QSpinBox {
                height: 30px;
                font-size: 14px;
                color: #2c3e50;
            }
            QCalendarWidget QWidget { 
                alternate-background-color: white;
            }
            QCalendarWidget QAbstractItemView:enabled 
            {
                font-size: 12px;
                color: #2c3e50;
                background-color:white; 
                selection-background-color: #3498db;
                selection-color: black;
            }
        """)

        # 连接日期选择器与日历
        self.date_selector.dateChanged.connect(self.calendar.setSelectedDate)
        self.calendar.clicked.connect(self.date_selector.setDate)

        # 事件列表
        self.event_list = QListWidget()
        self.event_list.setStyleSheet("""
            QListWidget {
                border: 1px solid #bdc3c7;
                border-radius: 4px;
                background-color: #ffffff;
                font-size: 14px;
                max-height: 200px;
            }
            QListWidget::item {
                padding: 8px;
                border-bottom: 1px solid #ecf0f1;
            }
            QListWidget::item:selected {
                background-color: #3498db;
                color: white;
            }
        """)
        self.event_list.itemDoubleClicked.connect(self.edit_event)

        # 添加事件按钮
        event_btn = create_styled_button("添加事件", "#2ecc71", "#27ae60", 150)
        delete_btn = create_styled_button("删除事件", "#e74c3c", "#c0392b", 150)

        # 按钮容器
        buttons_layout = QHBoxLayout()
        buttons_layout.addWidget(event_btn)
        buttons_layout.addWidget(delete_btn)
        buttons_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # 添加到日历布局
        calendar_layout.addWidget(self.date_selector, 0, Qt.AlignmentFlag.AlignCenter)
        calendar_layout.addWidget(self.calendar, 3)
        calendar_layout.addWidget(QLabel("事件列表:"))
        calendar_layout.addWidget(self.event_list, 2)
        calendar_layout.addLayout(buttons_layout)

        # 提醒定时器
        self.reminder_timer = QTimer(self)
        self.reminder_timer.timeout.connect(self.check_reminders)
        self.reminder_timer.start(10000)  # 每10秒检查一次提醒

        # 连接日历选择变化事件
        self.calendar.selectionChanged.connect(self.update_event_list)

        # 初始更新事件列表
        self.update_event_list()

        # 连接按钮信号
        event_btn.clicked.connect(self.add_event_dialog)
        delete_btn.clicked.connect(self.delete_selected_event)

    def add_event_dialog(self):
        """添加事件对话框"""
        dialog = QDialog(self)
        dialog.setWindowTitle("添加事件")
        layout = QFormLayout(dialog)

        # 事件标题
        title_edit = QLineEdit()
        title_edit.setPlaceholderText("输入事件标题")
        layout.addRow("标题:", title_edit)

        # 事件日期
        date_edit = QDateEdit(self.calendar.selectedDate())
        date_edit.setCalendarPopup(True)
        layout.addRow("日期:", date_edit)

        # 事件时间
        time_edit = QTimeEdit()
        time_edit.setTime(QTime.currentTime().addSecs(3600))  # 默认一小时后
        layout.addRow("时间:", time_edit)

        # 事件描述
        desc_edit = QTextEdit()
        desc_edit.setPlaceholderText("输入事件描述...")
        desc_edit.setMaximumHeight(100)
        layout.addRow("描述:", desc_edit)

        # 提醒选项
        reminder_check = QCheckBox("设置提醒")
        reminder_check.setChecked(True)
        layout.addRow(reminder_check)

        # 按钮盒
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        layout.addRow(button_box)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            event_date = date_edit.date()
            event = {
                "title": title_edit.text().strip(),
                "date": event_date,
                "time": time_edit.time(),
                "description": desc_edit.toPlainText().strip(),
                "reminder": reminder_check.isChecked(),
                "reminded": False  # 新增：是否已经提醒过
            }

            if not event["title"]:
                QMessageBox.warning(self, "输入错误", "事件标题不能为空")
                return

            # 添加到事件字典
            date_str = event_date.toString(Qt.DateFormat.ISODate)
            if date_str not in self.events:
                self.events[date_str] = []

            self.events[date_str].append(event)

            # 按时间排序
            self.events[date_str].sort(key=lambda e: e["time"])

            # 更新事件列表
            self.update_event_list()

            # 标记日历日期
            self.mark_date(event_date)

            QMessageBox.information(self, "成功", "事件已添加")

    def update_event_list(self):
        """更新事件列表显示"""
        self.event_list.clear()
        selected_date = self.calendar.selectedDate()
        date_str = selected_date.toString(Qt.DateFormat.ISODate)

        if date_str in self.events:
            for idx, event in enumerate(self.events[date_str]):
                time_str = event["time"].toString("hh:mm")
                reminder_icon = "🔔" if event["reminder"] else ""
                item = QListWidgetItem(f"{idx + 1}. {time_str} {event['title']} {reminder_icon}")
                item.setData(Qt.ItemDataRole.UserRole, event)
                self.event_list.addItem(item)

    def mark_date(self, date):
        """标记有事件的日期"""
        date_str = date.toString(Qt.DateFormat.ISODate)
        fmt = self.calendar.dateTextFormat(date)

        if date_str in self.events:
            # 设置金色背景标记有事件
            fmt.setBackground(QColor("#FFD700"))  # 金色
            fmt.setFontWeight(QFont.Weight.Bold)
        else:
            # 恢复默认格式
            fmt = self.calendar.dateTextFormat(QDate())

        self.calendar.setDateTextFormat(date, fmt)

    def delete_selected_event(self):
        """删除选中的事件"""
        selected_items = self.event_list.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "未选择", "请选择要删除的事件")
            return

        item = selected_items[0]
        event = item.data(Qt.ItemDataRole.UserRole)
        date_str = self.calendar.selectedDate().toString(Qt.DateFormat.ISODate)

        if date_str in self.events:
            # 从事件列表中移除
            self.events[date_str] = [e for e in self.events[date_str] if e != event]

            # 如果该日期没有事件了，移除日期
            if not self.events[date_str]:
                del self.events[date_str]
                # 恢复日期格式
                date = self.calendar.selectedDate()
                fmt = self.calendar.dateTextFormat(QDate())
                self.calendar.setDateTextFormat(date, fmt)

            self.update_event_list()

    def edit_event(self, item):
        """编辑事件"""
        event = item.data(Qt.ItemDataRole.UserRole)
        dialog = QDialog(self)
        dialog.setWindowTitle("编辑事件")
        layout = QFormLayout(dialog)

        # 事件标题
        title_edit = QLineEdit(event["title"])
        layout.addRow("标题:", title_edit)

        # 事件日期
        date_edit = QDateEdit(event["date"])
        date_edit.setCalendarPopup(True)
        layout.addRow("日期:", date_edit)

        # 事件时间
        time_edit = QTimeEdit(event["time"])
        layout.addRow("时间:", time_edit)

        # 事件描述
        desc_edit = QTextEdit(event["description"])
        desc_edit.setMaximumHeight(100)
        layout.addRow("描述:", desc_edit)

        # 提醒选项
        reminder_check = QCheckBox("设置提醒")
        reminder_check.setChecked(event["reminder"])
        layout.addRow(reminder_check)

        # 按钮盒
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        layout.addRow(button_box)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            event["title"] = title_edit.text().strip()
            event["date"] = date_edit.date()
            event["time"] = time_edit.time()
            event["description"] = desc_edit.toPlainText().strip()
            event["reminder"] = reminder_check.isChecked()

            if not event["title"]:
                QMessageBox.warning(self, "输入错误", "事件标题不能为空")
                return

            # 更新事件列表
            self.update_event_list()

            QMessageBox.information(self, "成功", "事件已更新")

    def check_reminders(self):
        """检查并显示提醒"""
        current_datetime = QDateTime.currentDateTime()

        # 检查所有事件
        for date_str, events in list(self.events.items()):
            for event in events:
                if event["reminder"] and not event["reminded"]:
                    # 创建完整的事件时间
                    event_datetime = QDateTime(event["date"], event["time"])

                    # 如果事件时间在当前时间之后5分钟内
                    secs_to_event = current_datetime.secsTo(event_datetime)
                    if 0 <= secs_to_event <= 300:  # 5分钟内
                        # 显示提醒
                        self.show_reminder(event)
                        # 标记为已提醒
                        event["reminded"] = True

    def show_reminder(self, event):
        """显示提醒窗口"""
        # 创建完整的事件时间
        event_datetime = QDateTime(event["date"], event["time"])

        # 创建自定义提醒窗口
        reminder_dialog = QDialog(self)
        reminder_dialog.setWindowTitle("事件提醒")
        reminder_dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        reminder_dialog.setFixedSize(400, 300)

        layout = QVBoxLayout(reminder_dialog)

        # 标题
        title_label = QLabel(f"<h2>{event['title']}</h2>")
        title_label.setStyleSheet("color: #2980b9;")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title_label)

        # 时间信息
        time_label = QLabel(
            f"<b>时间:</b> {event_datetime.toString('yyyy-MM-dd hh:mm')}<br>"
            f"<b>剩余时间:</b> 即将开始"
        )
        time_label.setStyleSheet("font-size: 14px; padding: 10px;")
        layout.addWidget(time_label)

        # 分隔线
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(separator)

        # 描述
        desc_label = QLabel(f"<b>描述:</b><br>{event['description'] if event['description'] else '无描述'}")
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet("padding: 10px; background-color: #f9f9f9; border-radius: 5px;")
        desc_label.setMinimumHeight(100)
        layout.addWidget(desc_label)

        # 按钮
        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        btn_box.accepted.connect(reminder_dialog.accept)
        layout.addWidget(btn_box)

        # 设置自动关闭（30秒后）
        close_timer = QTimer(reminder_dialog)
        close_timer.timeout.connect(reminder_dialog.accept)
        close_timer.start(30000)  # 30秒后关闭

        # 播放提示音
        try:
            import winsound
            winsound.MessageBeep()
        except Exception:
            pass

        reminder_dialog.exec()

    def create_documents_app(self):
        """创建文档应用组件"""
        self.documents_container = QWidget()
        docs_layout = QVBoxLayout(self.documents_container)

        # 文件操作按钮
        file_actions = QHBoxLayout()

        self.new_file_btn = create_styled_button("新建文件", "#3498db", "#2980b9")
        self.open_file_btn = create_styled_button("打开", "#2ecc71", "#27ae60")
        self.delete_file_btn = create_styled_button("删除", "#e74c3c", "#c0392b")
        self.refresh_btn = create_styled_button("刷新", "#9b59b6", "#8e44ad")

        file_actions.addWidget(self.new_file_btn)
        file_actions.addWidget(self.open_file_btn)
        file_actions.addWidget(self.delete_file_btn)
        file_actions.addWidget(self.refresh_btn)
        file_actions.addStretch()

        # 文件列表
        self.file_list = QListWidget()
        self.file_list.setStyleSheet("""
            QListWidget {
                border: 1px solid #bdc3c7;
                border-radius: 4px;
                background-color: #ffffff;
                font-size: 14px;
            }
            QListWidget::item {
                padding: 8px;
                border-bottom: 1px solid #ecf0f1;
            }
            QListWidget::item:selected {
                background-color: #3498db;
                color: white;
            }
        """)

        # 文件预览区域
        self.file_preview = QTextEdit()
        self.file_preview.setReadOnly(True)
        self.file_preview.setStyleSheet("""
            QTextEdit {
                border: 1px solid #bdc3c7;
                border-radius: 4px;
                background-color: #ffffff;
                font-family: monospace;
                font-size: 13px;
            }
        """)

        # 添加到文档布局
        docs_layout.addLayout(file_actions)
        docs_layout.addWidget(self.file_list, 3)
        docs_layout.addWidget(QLabel("文件预览:"))
        docs_layout.addWidget(self.file_preview, 2)

        # 连接信号
        self.new_file_btn.clicked.connect(self.create_new_file)
        self.open_file_btn.clicked.connect(self.open_selected_file)
        self.delete_file_btn.clicked.connect(self.delete_selected_file)
        self.refresh_btn.clicked.connect(self.refresh_file_list)
        self.file_list.itemDoubleClicked.connect(self.open_selected_file)
        self.file_list.itemSelectionChanged.connect(self.preview_selected_file)

    def create_notepad_app(self):
        """创建记事本应用组件"""
        self.notepad_container = QWidget()
        notepad_layout = QVBoxLayout(self.notepad_container)

        # 文本编辑区
        self.notepad_text = QTextEdit()
        self.notepad_text.setStyleSheet("""
            QTextEdit {
                border: 1px solid #bdc3c7;
                border-radius: 4px;
                background-color: #ffffff;
                font-family: monospace;
                font-size: 13px;
                min-height: 300px;
            }
        """)
        notepad_layout.addWidget(self.notepad_text, 1)

        # 工具栏
        toolbar = QHBoxLayout()

        # 保存按钮
        self.save_btn = create_styled_button("保存", "#2ecc71", "#27ae60")
        new_btn = create_styled_button("新建", "#3498db", "#2980b9")
        bold_btn = create_styled_button("粗体", "#9b59b6", "#8e44ad")

        toolbar.addWidget(new_btn)
        toolbar.addWidget(self.save_btn)
        toolbar.addWidget(bold_btn)
        toolbar.addStretch()

        notepad_layout.addLayout(toolbar)

        # 连接信号
        new_btn.clicked.connect(self.clear_notepad)
        self.save_btn.clicked.connect(self.save_notepad)
        bold_btn.clicked.connect(self.toggle_bold)

    def clear_notepad(self):
        """清空记事本内容"""
        self.notepad_text.clear()

    def save_notepad(self):
        """保存记事本内容到文件"""
        content = self.notepad_text.toPlainText()
        if not content.strip():
            QMessageBox.warning(self, "空内容", "记事本内容为空，无需保存")
            return

        filename, ok = QInputDialog.getText(
            self,
            "保存文件",
            "输入文件名:",
            QLineEdit.EchoMode.Normal,
            "笔记.txt"
        )

        if ok and filename:
            # 确保文件名有效
            if not filename.strip():
                QMessageBox.warning(self, "无效名称", "文件名不能为空。")
                return

            # 检查文件是否已存在
            filepath = os.path.join(self.documents_dir, filename)
            if os.path.exists(filepath):
                reply = QMessageBox.question(
                    self,
                    "文件已存在",
                    "已存在同名文件，是否覆盖?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return

            try:
                # 保存文件
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(content)
                QMessageBox.information(self, "保存成功", f"文件已保存: {filename}")
            except Exception as e:
                QMessageBox.critical(self, "保存错误", f"无法保存文件: {e}")

    def toggle_bold(self):
        """切换选中文本的粗体格式"""
        cursor = self.notepad_text.textCursor()
        if cursor.hasSelection():
            # 获取当前格式
            fmt = cursor.charFormat()

            # 切换粗体状态
            new_weight = QFont.Weight.Normal if fmt.fontWeight() > QFont.Weight.Normal else QFont.Weight.Bold

            # 创建新格式
            new_fmt = QTextCharFormat()
            new_fmt.setFontWeight(new_weight)

            # 应用新格式
            cursor.mergeCharFormat(new_fmt)
            self.notepad_text.setCurrentCharFormat(new_fmt)

    def set_app_name(self, name):
        self.title_label.setText(f"{name}应用")

        # 清除当前内容
        for i in reversed(range(self.content_layout.count())):
            widget = self.content_layout.itemAt(i).widget()
            if widget:
                widget.setParent(None)

        if name == "日历":
            # 显示日历应用
            self.content_layout.addWidget(self.calendar_container)
        elif name == "文档":
            # 显示文档应用
            self.content_layout.addWidget(self.documents_container)
            self.refresh_file_list()
        elif name == "记事本":
            # 显示记事本应用
            self.content_layout.addWidget(self.notepad_container)
            self.notepad_text.setFocus()
        elif name == "语音AI助手":
            # 显示语音AI助手
            self.content_layout.addWidget(self.voice_container)
        else:
            # 显示默认应用内容
            self.content_label = QLabel()
            self.content_label.setWordWrap(True)
            self.content_label.setFont(QFont("Arial", 12))
            self.content_layout.addWidget(self.content_label)

            self.sample_btn = create_styled_button("示例操作", "#3498db", "#2980b9", 200)
            self.content_layout.addWidget(self.sample_btn, alignment=Qt.AlignmentFlag.AlignCenter)

            self.content_label.setText(f"""
                <h2 style='color:#3498db;'>欢迎使用{name}应用</h2>
                <p>这是{name}应用的模拟界面。</p>
                <p>功能介绍:</p>
                <ul>
                    <li>响应式桌面布局</li>
                    <li>现代化UI组件</li>
                    <li>可定制内容区域</li>
                    <li>简易导航系统</li>
                </ul>
            """)

    def refresh_file_list(self):
        """刷新文件列表"""
        self.file_list.clear()

        # 获取文档目录中的所有文件
        try:
            files = os.listdir(self.documents_dir)
            for file in files:
                if os.path.isfile(os.path.join(self.documents_dir, file)):
                    # 创建带图标的列表项
                    item = QListWidgetItem(file)
                    item.setIcon(QIcon.fromTheme("text-x-generic"))
                    self.file_list.addItem(item)
        except Exception as e:
            QMessageBox.critical(self, "错误", f"无法列出文件: {e}")

    def create_new_file(self):
        """创建新文件"""
        filename, ok = QInputDialog.getText(
            self,
            "新建文件",
            "输入文件名:",
            QLineEdit.EchoMode.Normal,
            "新文件.txt"
        )

        if ok and filename:
            # 确保文件名有效
            if not filename.strip():
                QMessageBox.warning(self, "无效名称", "文件名不能为空。")
                return

            # 检查文件是否已存在
            filepath = os.path.join(self.documents_dir, filename)
            if os.path.exists(filepath):
                QMessageBox.warning(self, "文件已存在", "已存在同名文件。")
                return

            try:
                # 创建空文件
                with open(filepath, 'w') as _:
                    pass
                self.refresh_file_list()

                # 自动选择新创建的文件
                items = self.file_list.findItems(filename, Qt.MatchFlag.MatchExactly)
                if items:
                    self.file_list.setCurrentItem(items[0])
            except Exception as e:
                QMessageBox.critical(self, "错误", f"无法创建文件: {e}")

    def open_selected_file(self):
        """打开选中的文件"""
        selected_items = self.file_list.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "未选择", "请选择要打开的文件。")
            return

        filename = selected_items[0].text()
        filepath = os.path.join(self.documents_dir, filename)

        # 在预览区域显示文件内容
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            self.file_preview.setText(content)

            # 滚动到顶部
            cursor = self.file_preview.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            self.file_preview.setTextCursor(cursor)
        except Exception as e:
            self.file_preview.setText(f"读取文件错误: {e}")

    def preview_selected_file(self):
        """预览选中的文件"""
        selected_items = self.file_list.selectedItems()
        if not selected_items:
            self.file_preview.clear()
            return

        filename = selected_items[0].text()
        filepath = os.path.join(self.documents_dir, filename)

        # 在预览区域显示文件内容
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read(1000)  # 只读取前1000个字符
            self.file_preview.setText(content)

            # 滚动到顶部
            cursor = self.file_preview.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            self.file_preview.setTextCursor(cursor)
        except Exception as e:
            self.file_preview.setText(f"读取文件错误: {e}")

    def delete_selected_file(self):
        """删除选中的文件"""
        selected_items = self.file_list.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "未选择", "请选择要删除的文件。")
            return

        filename = selected_items[0].text()
        filepath = os.path.join(self.documents_dir, filename)

        # 确认删除
        reply = QMessageBox.question(
            self,
            "确认删除",
            f"确定要删除 '{filename}' 吗?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            try:
                os.remove(filepath)
                self.refresh_file_list()
                self.file_preview.clear()
            except Exception as e:
                QMessageBox.critical(self, "错误", f"无法删除文件: {e}")


class SettingsScreen(QWidget):
    backClicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # 初始化实例属性
        self.wifi_label = None
        self.wifi_toggle = None
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 10, 20, 20)

        # 标题栏
        title_bar = QHBoxLayout()
        back_btn = create_styled_button("← 返回桌面", "#3498db", "#2980b9")
        back_btn.setFixedHeight(30)
        back_btn.clicked.connect(self.backClicked.emit)

        title_label = QLabel("系统设置")
        title_label.setFont(QFont("Arial", 16, QFont.Weight.Bold))
        title_label.setStyleSheet("color: #2c3e50;")

        title_bar.addWidget(back_btn)
        title_bar.addWidget(title_label)
        title_bar.addStretch()

        # 设置内容区域
        settings_area = QFrame()
        settings_area.setStyleSheet("""
            background-color: white;
            border-radius: 8px;
            padding: 20px;
            border: 1px solid #bdc3c7;
        """)
        settings_layout = QVBoxLayout(settings_area)

        # 只保留三个设置项：Wi-Fi、蓝牙、深色模式
        basic_group = self.create_setting_group("基本设置", [
            ("Wi-Fi", self.create_wifi_setting()),
            ("蓝牙", self.create_toggle(True)),
            ("深色模式", self.create_toggle(False))
        ])

        # 添加到布局
        settings_layout.addWidget(basic_group)
        settings_layout.addStretch()

        # 添加到主布局
        layout.addLayout(title_bar)
        layout.addWidget(settings_area, 1)

        self.setLayout(layout)
        self.setStyleSheet("background-color: #ecf0f1;")

    @staticmethod
    def create_setting_group(title, settings):
        group = QGroupBox(title)
        group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 14px;
                color: #2c3e50;
                border: 1px solid #bdc3c7;
                border-radius: 6px;
                margin-top: 20px;
                padding-top: 20px;
            }
        """)

        layout = QVBoxLayout(group)
        layout.setSpacing(15)
        layout.setContentsMargins(15, 20, 15, 15)

        for name, widget in settings:
            item_layout = QHBoxLayout()
            label = QLabel(name)
            label.setStyleSheet("font-weight: normal; font-size: 13px;")
            label.setFixedWidth(150)

            item_layout.addWidget(label)
            item_layout.addStretch()
            item_layout.addWidget(widget)

            layout.addLayout(item_layout)

        return group

    def create_wifi_setting(self):
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.wifi_label = QLabel("未连接")
        self.wifi_label.setStyleSheet("color: #7f8c8d; font-size: 13px;")

        self.wifi_toggle = QCheckBox()
        self.wifi_toggle.setFixedSize(60, 30)
        self.wifi_toggle.setStyleSheet("""
            QCheckBox::indicator {
                width: 60px;
                height: 30px;
            }
            QCheckBox::indicator:unchecked {
                background-color: #bdc3c7;
                border-radius: 15px;
            }
            QCheckBox::indicator:checked {
                background-color: #2ecc71;
                border-radius: 15px;
            }
        """)

        # 连接切换信号
        self.wifi_toggle.stateChanged.connect(self.update_wifi_status)

        layout.addWidget(self.wifi_label)
        layout.addWidget(self.wifi_toggle)

        return container

    def update_wifi_status(self, state):
        if state:
            self.wifi_label.setText("已连接")
            self.wifi_label.setStyleSheet("color: #27ae60; font-size: 13px;")
        else:
            self.wifi_label.setText("未连接")
            self.wifi_label.setStyleSheet("color: #7f8c8d; font-size: 13px;")

    @staticmethod
    def create_toggle(initial_state=False):
        toggle = QCheckBox()
        toggle.setChecked(initial_state)
        toggle.setFixedSize(60, 30)
        toggle.setStyleSheet("""
            QCheckBox::indicator {
                width: 60px;
                height: 30px;
            }
            QCheckBox::indicator:unchecked {
                background-color: #bdc3c7;
                border-radius: 15px;
            }
            QCheckBox::indicator:checked {
                background-color: #2ecc71;
                border-radius: 15px;
            }
        """)
        return toggle


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = DesktopApp()
    window.showFullScreen()	
    sys.exit(app.exec())
