# -*- coding: utf-8 -*-
import os
os.environ['XNNPACK_DELEGATE'] = '0'  # 禁用XNNPACK加速

import cv2
import numpy as np
import mediapipe as mp
import time
import requests
import logging
import json
import threading
import pygame
import queue
import subprocess
from datetime import datetime
from qcloud_cos import CosConfig, CosS3Client
from PIL import Image, ImageDraw, ImageFont
import sys
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                             QLabel, QPushButton, QGroupBox, QComboBox, QSlider, QCheckBox,
                             QFileDialog, QMessageBox, QTabWidget, QProgressBar)
from PySide6.QtCore import Qt, QTimer, QSize, Signal, QObject
from PySide6.QtGui import QImage, QPixmap, QPainter, QPen, QFont, QIcon

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# 确保 alerts 文件夹存在
if not os.path.exists("alerts"):
    os.makedirs("alerts")
    logging.info("已创建 alerts 文件夹，请将语音文件放入其中")

# 初始化pygame音频
pygame.mixer.init()

# 全局配置
GLOBAL_CONFIG = {
    'poll_interval': 1,                 # 云指令轮询间隔(秒)
    'cloud_enabled': True,              # 是否启用云服务
    
    # 腾讯云 COS 配置
    'cos': {
        'SecretId': '',
        'SecretKey': '',
        'Region': 'ap-guangzhou',
        'Bucket': '521-1355543084',
        'Scheme': 'https'
    },
    
    # 微信云开发配置
    'wx_cloud': {
        'env_id': 'cloud1-',
        'access_token': '',             # 访问令牌(动态刷新)
        'api_url': 'https://api.weixin.qq.com/tcb/databasequery',  # 这是查询接口
        'add_api_url': 'https://api.weixin.qq.com/tcb/databaseadd',  # 新增写入接口
        'query_api_url': 'https://api.weixin.qq.com/tcb/databasequery',  # 保留查询接口
        'appid': '',
        'secret': '',
        'collection_name': 'photo'
    },
    
    # 姿势检测设置
    'posture': {
        'hunchback_threshold': 13,       # 脊柱弯曲角度阈值(度)
        'slouching_threshold': 68,       # 髋关节角度阈值(度)
        'shoulder_diff_threshold': 0.03, # 肩膀高度差阈值(图像比例)
        'desk_distance_threshold': 0.25, # 下巴位置阈值(图像比例)
        'leg_cross_threshold': 0.06,     # 二郎腿阈值(膝盖水平差)
        'min_visibility': 0.6,          # 关键点可见度阈值
        'min_warnings': 2,               # 触发警告的最小异常姿势数量
        'min_duration': 5,               # 触发截图的最小持续时间(秒)
        'cooldown': 10                   # 两次截图的最小间隔(秒)
    },
    
    # 语音提示设置
    'voice': {
        'min_alert_interval': 3.0,      # 同一种提醒的最小间隔(秒)
        'min_warning_duration': 2.0,    # 触发提醒的最小持续时间(秒)
        'cooldown_period': 5.0          # 全局提醒冷却时间(秒)
    }
}

# 新增：错误姿态类型到中文的映射
POSTURE_CHINESE_MAP = {
    "HUNCHBACK": "驼背",
    "SLOUCHING": "坐姿倾斜",
    "UNEVEN SHOULDERS": "肩部不平",
    "TOO CLOSE": "距离过近",
    "CROSSED LEGS": "二郎腿"
}

# ================== 全局状态管理 ==================
class GlobalState(QObject):
    status_update = Signal(str, str)
    command_executed = Signal(str, str)
    
    def __init__(self):
        super().__init__()
        self.virtual_led = False               # 虚拟LED状态
        self.camera_active = False             # 摄像头是否激活
        self.capture_requested = False         # 截图请求标志
        self.last_command = {                 # 最近执行的指令
            'id': None,                       # 指令ID
            'content': None,                  # 指令内容
            'timestamp': 0,                   # 执行时间戳
            'source': "none"                  # 指令来源
        }
        self.lock = threading.Lock()          # 状态锁
        self.cloud_poller_active = True       # 云轮询器是否激活
        
    def is_duplicate_command(self, command_data: dict) -> bool:
        """检查是否为重复指令"""
        current_cmd_id = command_data.get('_id', '')
        current_cmd = command_data.get('command', '')
        
        # 如果指令ID和内容都与上次相同，则认为是重复指令
        if (current_cmd_id == self.last_command['id'] and 
            current_cmd == self.last_command['content']):
            logging.info(f"跳过重复指令: {current_cmd}")
            return True
        return False
        
    def update_last_command(self, command_data, source="cloud"):
        """更新最后执行的指令信息"""
        with self.lock:
            self.last_command = {
                'id': command_data.get('_id', ''),
                'content': command_data.get('command', ''),
                'timestamp': time.time(),
                'source': source
            }
            self.command_executed.emit(self.last_command['content'], source)
            
    def update_status(self, text, color):
        """更新状态信息"""
        self.status_update.emit(text, color)
        
    def start_cloud_poller(self):
        """启动云指令轮询器"""
        if GLOBAL_CONFIG['cloud_enabled']:
            self.cloud_poller_thread = threading.Thread(
                target=self.run_cloud_poller,
                daemon=True
            )
            self.cloud_poller_thread.start()
            logging.info("云指令轮询已启动")
    
    def stop_cloud_poller(self):
        """停止云指令轮询器"""
        self.cloud_poller_active = False
        logging.info("云指令轮询已停止")
        
    def run_cloud_poller(self):
        """运行云指令轮询器"""
        while self.cloud_poller_active:
            try:
                commands = self.poll_cloud_commands()
                for cmd in commands:
                    self.execute_command(cmd)
            except Exception as e:
                logging.error(f"云指令轮询错误: {e}")
                self.update_status(f"云指令轮询错误: {e}", "#f44336")
            time.sleep(GLOBAL_CONFIG['poll_interval'])
            
    def poll_cloud_commands(self):
        """从微信云开发查询最新指令"""
        if not GLOBAL_CONFIG['wx_cloud']['access_token']:
            self.refresh_token()

        try:
            # 查询最新指令(按时间戳倒序)
            query = {
                "env": GLOBAL_CONFIG['wx_cloud']['env_id'],
                "query": "db.collection('commands')"
                         ".orderBy('timestamp', 'desc')"
                         ".limit(1)"
                         ".get()"
            }

            res = requests.post(
                f"{GLOBAL_CONFIG['wx_cloud']['api_url']}?access_token={GLOBAL_CONFIG['wx_cloud']['access_token']}",
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
            logging.error(f"查询指令失败: {e}")
            return []
            
    def refresh_token(self):
        """刷新微信云开发访问令牌"""
        try:
            res = requests.get(
                "https://api.weixin.qq.com/cgi-bin/token",
                params={
                    'grant_type': 'client_credential',
                    'appid': GLOBAL_CONFIG['wx_cloud']['appid'],
                    'secret': GLOBAL_CONFIG['wx_cloud']['secret']
                },
                timeout=10
            )
            res.raise_for_status()
            GLOBAL_CONFIG['wx_cloud']['access_token'] = res.json()['access_token']
            logging.info(f"Token刷新成功")
            return True
        except Exception as e:
            logging.error(f"Token刷新失败: {e}")
            GLOBAL_CONFIG['wx_cloud']['access_token'] = ''
            return False
            
    def mark_command_executed(self, cmd_id):
        """标记云指令为已执行状态"""
        try:
            requests.post(
                f"{GLOBAL_CONFIG['wx_cloud']['api_url']}?access_token={GLOBAL_CONFIG['wx_cloud']['access_token']}",
                json={
                    "env": GLOBAL_CONFIG['wx_cloud']['env_id'],
                    "query": f"db.collection('commands').doc('{cmd_id}').update({{data:{{status:'completed'}}}})"
                },
                timeout=10
            )
            return True
        except Exception as e:
            logging.error(f"标记指令失败: {e}")
            return False
            
    camera_control_needed = Signal(bool)  # 新增信号：True=启动，False=停止
    def execute_command(self, command_data, source="cloud"):
        command = command_data.get('command', '')
        cmd_id = command_data.get('_id', '')
        
        # 检查是否为重复指令
        if self.is_duplicate_command(command_data):
            return

        # 使用信号触发摄像头控制 (确保线程安全)
        if command == 'start_camera':
            self.camera_control_needed.emit(True)
        elif command == 'stop_camera':
            self.camera_control_needed.emit(False)
        elif command == 'capture':
            with self.lock:
                self.capture_requested = True
            logging.info("收到截图指令，已设置截图标志")    
        # 更新最后执行的指令信息
        self.update_last_command(command_data, source)

        # 标记云指令为已执行
        if source == "cloud" and cmd_id:
            self.mark_command_executed(cmd_id)
# ================== 语音提醒类 ==================
class VoiceAlerts:
    def __init__(self):
        self.alert_timers = {
            "HUNCHBACK": 0,
            "SLOUCHING": 0,
            "UNEVEN SHOULDERS": 0,
            "TOO CLOSE": 0,
            "CROSSED LEGS": 0
        }
        self.last_alert_time = 0
        self.alert_queue = queue.Queue()
        self.alert_thread = threading.Thread(target=self._process_alerts)
        self.alert_thread.daemon = True
        self.alert_thread.start()
        
    def _process_alerts(self):
        while True:
            alert_type = self.alert_queue.get()
            if alert_type is None:
                break
                
            current_time = time.time()
            if current_time - self.last_alert_time < GLOBAL_CONFIG['voice']['cooldown_period']:
                continue
                
            self._play_alert(alert_type)
            self.last_alert_time = current_time
            
    def _play_alert(self, alert_type):
        try:
            file_map = {
                "HUNCHBACK": "hunchback.wav",
                "SLOUCHING": "slouching.wav",
                "UNEVEN SHOULDERS": "uneven_shoulders.wav",
                "TOO CLOSE": "too_close.wav",
                "CROSSED LEGS": "crossed_legs.wav"
            }
            
            filename = file_map.get(alert_type)
            if filename:
                sound = pygame.mixer.Sound(f"alerts/{filename}")
                sound.play()
                logging.info(f"播放语音提醒: {alert_type}")
        except Exception as e:
            logging.error(f"播放语音错误: {e}")
    
    def add_alert(self, alert_type):
        current_time = time.time()
        if current_time - self.alert_timers[alert_type] > GLOBAL_CONFIG['voice']['min_alert_interval']:
            self.alert_queue.put(alert_type)
            self.alert_timers[alert_type] = current_time
    
    def stop(self):
        self.alert_queue.put(None)
        self.alert_thread.join()

# ================== 核心功能类 ==================
class AccessTokenManager:
    def __init__(self):
        self.token = None
        self.expire_time = 0

    def get_token(self):
        if self.token and time.time() < self.expire_time:
            return self.token
        self.refresh_token()
        return self.token

    def refresh_token(self):
        try:
            params = {
                'grant_type': 'client_credential',
                'appid': GLOBAL_CONFIG['wx_cloud']['appid'],
                'secret': GLOBAL_CONFIG['wx_cloud']['secret']
            }
            response = requests.get(
                "https://api.weixin.qq.com/cgi-bin/token", 
                params=params, 
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            self.token = data['access_token']
            self.expire_time = time.time() + data.get('expires_in', 7200) - 60
            logging.info(f"刷新Token成功，有效期至：{time.ctime(self.expire_time)}")
            return True
        except Exception as e:
            logging.error(f"获取AccessToken失败：{str(e)}")
            return False

class COSUploader:
    def __init__(self):
        self.cos_client = CosS3Client(CosConfig(
            Region=GLOBAL_CONFIG['cos']['Region'],
            SecretId=GLOBAL_CONFIG['cos']['SecretId'],
            SecretKey=GLOBAL_CONFIG['cos']['SecretKey'],
            Scheme=GLOBAL_CONFIG['cos']['Scheme']
        ))

    def upload_file(self, file_path):
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在：{file_path}")
        object_key = f"posture_captures/{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        try:
            logging.info(f"上传文件：{object_key}")
            response = self.cos_client.upload_file(
                Bucket=GLOBAL_CONFIG['cos']['Bucket'],
                LocalFilePath=file_path,
                Key=object_key,
                EnableMD5=True
            )
            display_url = f"{GLOBAL_CONFIG['cos']['Scheme']}://{GLOBAL_CONFIG['cos']['Bucket']}.cos.{GLOBAL_CONFIG['cos']['Region']}.myqcloud.com/{object_key}"
            logging.info(f"上传成功，URL：{display_url}")
            return display_url
        except Exception as e:
            logging.error(f"COS上传失败：{str(e)}")
            raise

class PostureMonitor(QWidget):
    status_update = Signal(str, str)
    posture_update = Signal(str, bool)
    upload_complete = Signal(str)
    fps_update = Signal(float)
    command_executed = Signal(str, str)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # 状态跟踪变量
        self.warning_start_time = 0
        self.last_upload_time = 0
        self.current_fps = 0
        self.warning_duration = 0
        self.current_warnings = 0
        self.posture_warnings = []
        self.camera_active = False
        
        # 初始化组件
        self.token_manager = AccessTokenManager()
        self.uploader = COSUploader()
        self.voice_alerts = VoiceAlerts()
        self.global_state = GlobalState()
        
        # 姿势状态跟踪
        self.posture_states = {
            "HUNCHBACK": {"active": False, "start_time": 0},
            "SLOUCHING": {"active": False, "start_time": 0},
            "UNEVEN SHOULDERS": {"active": False, "start_time": 0},
            "TOO CLOSE": {"active": False, "start_time": 0},
            "CROSSED LEGS": {"active": False, "start_time": 0}
        }
        
        # 创建临时目录
        self.temp_dir = "posture_captures"
        os.makedirs(self.temp_dir, exist_ok=True)
        
        # 初始化MediaPipe
        self.mp_pose = mp.solutions.pose
        self.pose = self.mp_pose.Pose(
            static_image_mode=False,
            model_complexity=1,
            min_detection_confidence=0.7,
            min_tracking_confidence=0.7
        )
        self.mp_drawing = mp.solutions.drawing_utils
        
        # 初始化摄像头
        self.cap = None
        self.cam_width = 640
        self.cam_height = 480
        
        # 设置定时器
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_frame)
        self.prev_time = time.time()
        
        # 初始化UI
        self.init_ui()
        self.status_update.connect(self.update_status)
        self.posture_update.connect(self.update_posture_status)
        self.upload_complete.connect(self.update_upload_status)  # 修复上传状态
        self.fps_update.connect(self.update_fps)
        self.command_executed.connect(self.update_command_status)

        # 连接全局状态信号
        self.global_state.status_update.connect(self.update_status)
        self.global_state.command_executed.connect(self.handle_command_executed)
        self.global_state.camera_control_needed.connect(self.handle_cloud_camera_control)
        # 启动云指令轮询
        self.global_state.start_cloud_poller()

    def handle_cloud_camera_control(self, start):
        """处理云端的摄像头控制指令 - 确保在主线程执行"""
        # 使用 QTimer.singleShot 确保在主线程执行 UI 操作
        QTimer.singleShot(0, lambda: self._handle_camera_control(start))

    def _handle_camera_control(self, start):
        """实际处理摄像头控制逻辑"""
        if start:
            if not self.global_state.camera_active:
                if self.start_camera():  # 启动摄像头
                    self.start_btn.setText("停止摄像头")
                    self.start_btn.setStyleSheet("background-color: #f44336; color: white; font-weight: bold;")
                    self.status_update.emit("摄像头已启动(云指令)", "#4CAF50")
        else:
            if self.global_state.camera_active:
                self.stop_camera()  # 停止摄像头
                self.start_btn.setText("启动摄像头")
                self.start_btn.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold;")
                self.status_update.emit("摄像头已停止(云指令)", "#FF9800")
    def init_ui(self):
        # 主布局
        main_layout = QHBoxLayout()
        
        # 左侧布局 - 摄像头和状态
        left_layout = QVBoxLayout()
        
        # 摄像头显示
        self.camera_label = QLabel()
        self.camera_label.setAlignment(Qt.AlignCenter)
        self.camera_label.setMinimumSize(640, 480)
        self.camera_label.setStyleSheet("background-color: black; border: 2px solid #444;")
        left_layout.addWidget(self.camera_label)
        
        # 状态信息
        self.status_label = QLabel("状态: 准备就绪")
        self.status_label.setStyleSheet("font-size: 14px; color: #4CAF50; padding: 5px;")
        left_layout.addWidget(self.status_label)
        
        # 指令状态
        self.command_label = QLabel("最后指令: 无")
        self.command_label.setStyleSheet("font-size: 14px; padding: 5px;")
        left_layout.addWidget(self.command_label)
        
        # 姿势状态
        posture_group = QGroupBox("姿势状态")
        posture_layout = QVBoxLayout()
        
        self.posture_labels = {}
        for posture in self.posture_states.keys():
            lbl = QLabel(f"{posture}: 正常")
            lbl.setStyleSheet("font-size: 12px; color: #4CAF50; padding: 3px;")
            posture_layout.addWidget(lbl)
            self.posture_labels[posture] = lbl
        
        posture_group.setLayout(posture_layout)
        left_layout.addWidget(posture_group)
        
        # 右侧布局 - 控制面板
        right_layout = QVBoxLayout()
        
        # 控制面板
        control_group = QGroupBox("系统控制")
        control_layout = QVBoxLayout()
        
        # 摄像头选择
        camera_layout = QHBoxLayout()
        camera_layout.addWidget(QLabel("摄像头:"))
        self.camera_combo = QComboBox()
        self.camera_combo.addItems(["自动检测", "索引0", "索引1", "索引2", "索引3", "索引4"])
        self.camera_combo.setCurrentIndex(0)
        camera_layout.addWidget(self.camera_combo)
        control_layout.addLayout(camera_layout)
        
        # 摄像头控制按钮
        self.start_btn = QPushButton("启动摄像头")
        self.start_btn.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold;")
        self.start_btn.clicked.connect(self.toggle_camera)
        control_layout.addWidget(self.start_btn)
        
        # 手动截图按钮
        self.capture_btn = QPushButton("手动截图")
        self.capture_btn.setStyleSheet("background-color: #2196F3; color: white;")
        self.capture_btn.clicked.connect(self.manual_capture)
        control_layout.addWidget(self.capture_btn)
        
        # 上传状态
        self.upload_label = QLabel("上次上传: 无")
        self.upload_label.setStyleSheet("font-size: 12px; padding: 5px;")
        control_layout.addWidget(self.upload_label)
        
        # 统计信息
        stats_group = QGroupBox("实时统计")
        stats_layout = QVBoxLayout()
        
        self.fps_label = QLabel("FPS: 0.0")  # 改为初始0.0	
        stats_layout.addWidget(self.fps_label)
        
        self.warnings_label = QLabel("当前警告: 0")
        stats_layout.addWidget(self.warnings_label)
        
        self.duration_label = QLabel("持续时间: 0.0秒")
        stats_layout.addWidget(self.duration_label)
        
        self.cooldown_label = QLabel("冷却时间: 0.0秒")
        stats_layout.addWidget(self.cooldown_label)
        
        stats_group.setLayout(stats_layout)
        control_layout.addWidget(stats_group)
        
        # 设置面板
        settings_group = QGroupBox("检测设置")
        settings_layout = QVBoxLayout()
        
        # 驼背阈值
        hunch_layout = QHBoxLayout()
        hunch_layout.addWidget(QLabel("驼背阈值:"))
        self.hunch_slider = QSlider(Qt.Horizontal)
        self.hunch_slider.setRange(5, 25)
        self.hunch_slider.setValue(int(GLOBAL_CONFIG['posture']['hunchback_threshold']))
        self.hunch_slider.valueChanged.connect(self.update_hunch_threshold)
        hunch_layout.addWidget(self.hunch_slider)
        self.hunch_value_label = QLabel(f"{GLOBAL_CONFIG['posture']['hunchback_threshold']}°")
        hunch_layout.addWidget(self.hunch_value_label)
        settings_layout.addLayout(hunch_layout)
        
        # 坐姿阈值
        slouch_layout = QHBoxLayout()
        slouch_layout.addWidget(QLabel("坐姿阈值:"))
        self.slouch_slider = QSlider(Qt.Horizontal)
        self.slouch_slider.setRange(50, 85)
        self.slouch_slider.setValue(int(GLOBAL_CONFIG['posture']['slouching_threshold']))
        self.slouch_slider.valueChanged.connect(self.update_slouch_threshold)
        slouch_layout.addWidget(self.slouch_slider)
        self.slouch_value_label = QLabel(f"{GLOBAL_CONFIG['posture']['slouching_threshold']}°")
        slouch_layout.addWidget(self.slouch_value_label)
        settings_layout.addLayout(slouch_layout)
        
        # 距离阈值
        distance_layout = QHBoxLayout()
        distance_layout.addWidget(QLabel("距离阈值:"))
        self.distance_slider = QSlider(Qt.Horizontal)
        self.distance_slider.setRange(10, 50)
        self.distance_slider.setValue(int(GLOBAL_CONFIG['posture']['desk_distance_threshold'] * 100))
        self.distance_slider.valueChanged.connect(self.update_distance_threshold)
        distance_layout.addWidget(self.distance_slider)
        self.distance_value_label = QLabel(f"{GLOBAL_CONFIG['posture']['desk_distance_threshold']*100:.0f}%")
        distance_layout.addWidget(self.distance_value_label)
        settings_layout.addLayout(distance_layout)
        
        # 语音设置
        self.voice_check = QCheckBox("启用语音提醒")
        self.voice_check.setChecked(True)
        settings_layout.addWidget(self.voice_check)
        
        # 云服务设置
        self.cloud_check = QCheckBox("启用云服务")
        self.cloud_check.setChecked(GLOBAL_CONFIG['cloud_enabled'])
        self.cloud_check.stateChanged.connect(self.toggle_cloud_service)
        settings_layout.addWidget(self.cloud_check)
        
        settings_group.setLayout(settings_layout)
        control_layout.addWidget(settings_group)
        
        # 添加进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(5)
        control_layout.addWidget(self.progress_bar)
        
        control_group.setLayout(control_layout)
        right_layout.addWidget(control_group)
        
        # 添加退出按钮
        self.exit_btn = QPushButton("退出系统")
        self.exit_btn.setStyleSheet("background-color: #f44336; color: white;")
        self.exit_btn.clicked.connect(self.close_app)
        right_layout.addWidget(self.exit_btn)
        
        # 添加左右布局到主布局
        main_layout.addLayout(left_layout, 70)
        main_layout.addLayout(right_layout, 30)
        
        self.setLayout(main_layout)
    def update_upload_status(self, success):
        self.upload_label.setText(f"上次上传: {datetime.now().strftime('%H:%M:%S')}")
        self.status_update.emit("截图已上传", "#4CAF50")
    #def update_upload_status(self, status):   
    def update_status(self, text, color):
        self.status_label.setText(f"状态: {text}")
        self.status_label.setStyleSheet(f"font-size: 14px; color: {color}; padding: 5px;")
        
    def update_command_status(self, command, source):
        if command == 'capture':
            display_text = "截图指令"
        else:
            display_text = command
        self.command_label.setText(f"最后指令: {command} ({source})")
        
    def update_posture_status(self, posture, warning):
        color = "#f44336" if warning else "#4CAF50"
        status = "警告" if warning else "正常"
        self.posture_labels[posture].setText(f"{posture}: {status}")
        self.posture_labels[posture].setStyleSheet(f"font-size: 12px; color: {color}; padding: 3px;")
    def update_fps(self, fps):
        self.fps_label.setText(f"FPS: {fps:.1f}")
        self.current_fps = fps
        
    def toggle_camera(self):
        if self.global_state.camera_active:
            self.stop_camera()  # 这会自动更新global_state.camera_active
            self.start_btn.setText("启动摄像头")
            self.start_btn.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold;")
            self.status_update.emit("摄像头已停止", "#f44336")
        else:
            if self.start_camera():  # 这会自动更新global_state.camera_active
                self.start_btn.setText("停止摄像头")
                self.start_btn.setStyleSheet("background-color: #f44336; color: white; font-weight: bold;")
                self.status_update.emit("摄像头已启动", "#4CAF50")
            else:
                self.status_update.emit("无法启动摄像头", "#f44336")
                
    def start_camera(self):
        try:
            if self.cap is not None:
                self.cap.release()
                
            # 根据用户选择设置摄像头索引
            cam_index = self.camera_combo.currentIndex() - 1
            if cam_index < 0:
                # 先尝试默认索引21
                cap = cv2.VideoCapture(21)
                if cap.isOpened():
                    cam_index = 21
                    cap.release()
                else:
                    cam_index = self.find_camera()
                if cam_index is None:
                    logging.error("无法找到可用的摄像头")
                    return False
            
            self.cap = cv2.VideoCapture(cam_index)
            if not self.cap.isOpened():
                logging.error(f"无法打开摄像头索引: {cam_index}")
                return False
            
            logging.info(f"成功打开摄像头索引: {cam_index}")
            
            # 获取摄像头分辨率
            self.cam_width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            self.cam_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            logging.info(f"摄像头分辨率: {self.cam_width}x{self.cam_height}")
            
            # 启动定时器
            self.timer.start(30)  # 约33 FPS
            self.prev_time = time.time()
            
            # 更新全局状态
            self.global_state.camera_active = True
            return True
        except Exception as e:
            logging.error(f"启动摄像头时出错: {str(e)}")
            return False

    def stop_camera(self):
        if self.timer.isActive():
            self.timer.stop()
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        self.camera_label.clear()
        self.camera_label.setText("摄像头已停止")
        
        # 更新全局状态
        self.global_state.camera_active = False
        
        self.status_update.emit("摄像头已停止", "#f44336")
        
    def find_camera(self):
        """自动检测可用的摄像头设备"""
        # 先尝试指定的摄像头索引
        cap = cv2.VideoCapture(0)
        if cap.isOpened():
            logging.info(f"成功打开摄像头索引: 0")
            cap.release()
            return 0
        
        # 如果指定索引失败，尝试其他索引
        for idx in range(0, 5):
            cap = cv2.VideoCapture(idx)
            if cap.isOpened():
                logging.info(f"成功打开摄像头索引: {idx}")
                cap.release()
                return idx
        return None

    def calculate_angle(self, a, b, c):
        a = np.array(a)
        b = np.array(b)
        c = np.array(c)
        
        radians = np.arctan2(c[1]-b[1], c[0]-b[0]) - np.arctan2(a[1]-b[1], a[0]-b[0])
        angle = np.abs(radians * 180.0 / np.pi)
        return angle if angle <= 180 else 360 - angle

    def calculate_hip_angle(self, shoulder, hip, knee):
        shoulder = np.array(shoulder)
        hip = np.array(hip)
        knee = np.array(knee)
        
        torso_vec = hip - shoulder
        thigh_vec = knee - hip
        dot_product = np.dot(torso_vec, thigh_vec)
        torso_len = np.linalg.norm(torso_vec)
        thigh_len = np.linalg.norm(thigh_vec)
        
        return np.arccos(dot_product / (torso_len * thigh_len)) * 180 / np.pi if torso_len * thigh_len != 0 else 90

    def put_chinese_text(self, image, text, position, font_size=20, color=(0, 255, 0)):
        """使用PIL在图像上绘制中文文本"""
        # 将OpenCV图像转换为PIL图像
        img_pil = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(img_pil)
        
        try:
            # 尝试加载中文字体（请确保字体文件存在）
            font_path = '/home/elf/main/SimHei.ttf'  # 或者使用绝对路径如 'C:/Windows/Fonts/simhei.ttf'
            font = ImageFont.truetype(font_path, font_size)
        except Exception as e:
            # 回退到默认字体（可能无法显示中文）
            font = ImageFont.load_default()
            logging.warning(f"加载中文字体失败，使用默认字体: {str(e)}")
        
        # 绘制文本
        draw.text(position, text, font=font, fill=color)
        
        # 转换回OpenCV格式
        return cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)

    def save_to_cloudbase(self, display_url):
        """将URL存入微信云开发数据库"""
        access_token = self.token_manager.get_token()
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        payload = {
            "env": GLOBAL_CONFIG['wx_cloud']['env_id'],
            "query": f"db.collection('{GLOBAL_CONFIG['wx_cloud']['collection_name']}').add({{data: {{display_url: '{display_url}', upload_time: '{current_time}'}}}})"
        }
        
        try:
            # 使用正确的add_api_url
            response = requests.post(
                f"{GLOBAL_CONFIG['wx_cloud']['add_api_url']}?access_token={access_token}",
                json=payload,
                headers={'Content-Type': 'application/json'},
                timeout=20
            )
            response.raise_for_status()
            result = response.json()
            
            if result.get('errcode') == 0:
                record_id = result.get('id_list', [None])[0] or '未知ID'
                logging.info(f"数据存入成功，记录ID: {record_id}")
                return True
            else:
                raise Exception(f"云开发错误: {result.get('errmsg')} (Code: {result.get('errcode')})")
                
        except Exception as e:
            logging.error(f"保存到云开发失败: {str(e)}")
            return False

    def handle_capture(self, image):
        """处理截图和上传，添加中文水印"""
        try:
            self.status_update.emit("正在处理截图...", "#2196F3")
            # 生成唯一文件名
            filename = os.path.join(self.temp_dir, f"capture_{int(time.time())}.jpg")
            
            # 添加中文水印（如果有错误姿势）
            if self.posture_warnings:
                # 转换为PIL图像
                img_pil = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
                draw = ImageDraw.Draw(img_pil)
                
                try:
                    # 加载中文字体
                    font_path = '/home/elf/main/SimHei.ttf'  # 确保字体文件存在
                    font = ImageFont.truetype(font_path, 30)
                except Exception as e:
                    logging.warning(f"加载中文字体失败: {str(e)}")
                    font = ImageFont.load_default()
                
                # 准备水印文本（转换为中文）
                chinese_warnings = [POSTURE_CHINESE_MAP[warn] for warn in self.posture_warnings]
                watermark_text = " | ".join(chinese_warnings)
                
                # 计算水印位置（右上角）
                # 使用兼容的方法获取文本尺寸
                try:
                    # 新版本Pillow使用textbbox
                    bbox = draw.textbbox((0, 0), watermark_text, font=font)
                    text_width = bbox[2] - bbox[0]
                    text_height = bbox[3] - bbox[1]
                except AttributeError:
                    # 旧版本Pillow使用textsize
                    text_width, text_height = draw.textsize(watermark_text, font=font)
                
                margin = 20
                position = (img_pil.width - text_width - margin, margin)

                # 绘制水印（红色半透明背景+白色文字）
                # 背景矩形的位置：左上角和右下角
                background_position = (position[0]-5, position[1]-5, 
                                     position[0]+text_width+5, position[1]+text_height+5)
                draw.rectangle(background_position, fill=(255, 0, 0, 128))
                draw.text(position, watermark_text, font=font, fill=(255, 255, 255))
                
                # 转换回OpenCV格式
                image = cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)
            
            # 保存图像
            cv2.imwrite(filename, image)
            logging.info(f"已保存截图: {filename}")
            
            # 上传到COS
            display_url = self.uploader.upload_file(filename)
            
            # 保存到云开发
            if self.save_to_cloudbase(display_url):
                logging.info("截图已成功发送到小程序")
            
            # 清理临时文件
            os.remove(filename)
            
            # 更新UI
            self.upload_complete.emit(display_url)
            
            return True
        except Exception as e:
            logging.error(f"截图处理失败: {str(e)}")
            return False

    def update_frame(self):
        if not self.cap or not self.cap.isOpened():
            return
            
        success, image = self.cap.read()
        if not success:
            logging.warning("无法接收帧，尝试重新连接...")
            self.status_update.emit("无法接收帧", "#f44336")
            return
        
        # 检查全局截图请求
        if self.global_state.capture_requested:
            with self.global_state.lock:
                self.global_state.capture_requested = False
            threading.Thread(
                target=self.handle_capture, 
                args=(image.copy(),), 
                daemon=True
            ).start()
        
        # 计算FPS
        current_time = time.time()
        fps = 1 / (current_time - self.prev_time) if self.prev_time > 0 else 0
        self.prev_time = current_time
        self.fps_update.emit(fps)
        
        # 姿势检测
        image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        results = self.pose.process(image_rgb)
        
        self.posture_warnings = []
        if results.pose_landmarks:
            landmarks = results.pose_landmarks.landmark
            
            # 获取关键点坐标
            left_shoulder = [landmarks[self.mp_pose.PoseLandmark.LEFT_SHOULDER.value].x, 
                            landmarks[self.mp_pose.PoseLandmark.LEFT_SHOULDER.value].y]
            right_shoulder = [landmarks[self.mp_pose.PoseLandmark.RIGHT_SHOULDER.value].x, 
                             landmarks[self.mp_pose.PoseLandmark.RIGHT_SHOULDER.value].y]
            left_hip = [landmarks[self.mp_pose.PoseLandmark.LEFT_HIP.value].x, 
                       landmarks[self.mp_pose.PoseLandmark.LEFT_HIP.value].y]
            right_hip = [landmarks[self.mp_pose.PoseLandmark.RIGHT_HIP.value].x, 
                        landmarks[self.mp_pose.PoseLandmark.RIGHT_HIP.value].y]
            left_knee = [landmarks[self.mp_pose.PoseLandmark.LEFT_KNEE.value].x, 
                        landmarks[self.mp_pose.PoseLandmark.LEFT_KNEE.value].y]
            right_knee = [landmarks[self.mp_pose.PoseLandmark.RIGHT_KNEE.value].x, 
                         landmarks[self.mp_pose.PoseLandmark.RIGHT_KNEE.value].y]
            left_ankle = [landmarks[self.mp_pose.PoseLandmark.LEFT_ANKLE.value].x,
                         landmarks[self.mp_pose.PoseLandmark.LEFT_ANKLE.value].y]
            right_ankle = [landmarks[self.mp_pose.PoseLandmark.RIGHT_ANKLE.value].x,
                          landmarks[self.mp_pose.PoseLandmark.RIGHT_ANKLE.value].y]
            chin = [landmarks[self.mp_pose.PoseLandmark.NOSE.value].x, 
                   landmarks[self.mp_pose.PoseLandmark.NOSE.value].y]

            # 计算各项指标
            upper_back = [(left_shoulder[0]+right_shoulder[0])/2, (left_shoulder[1]+right_shoulder[1])/2]
            lower_back = [(left_hip[0]+right_hip[0])/2, (left_hip[1]+right_hip[1])/2]
            
            spine_angle = self.calculate_angle(upper_back, lower_back, chin)
            left_hip_angle = self.calculate_hip_angle(left_shoulder, left_hip, left_knee)
            right_hip_angle = self.calculate_hip_angle(right_shoulder, right_hip, right_knee)
            avg_hip_angle = (left_hip_angle + right_hip_angle) / 2
            shoulder_diff = abs(left_shoulder[1] - right_shoulder[1])
            chin_height = chin[1]
            
            # 二郎腿检测指标
            knee_height_diff = abs(left_knee[1] - right_knee[1])
            ankle_crossed = abs(left_ankle[0] - right_ankle[0]) < 0.1  # 两脚踝水平位置接近

            # 改进的姿势判断逻辑
            if spine_angle > GLOBAL_CONFIG['posture']['hunchback_threshold']:
                self.posture_warnings.append("HUNCHBACK")
            
            # 更智能的葛优躺检测
            if avg_hip_angle < GLOBAL_CONFIG['posture']['slouching_threshold']:
                if spine_angle > 35:  # 只有同时脊柱弯曲才判定
                    self.posture_warnings.append("SLOUCHING")
            
            if shoulder_diff > GLOBAL_CONFIG['posture']['shoulder_diff_threshold']:
                self.posture_warnings.append("UNEVEN SHOULDERS")
            
            if chin_height > GLOBAL_CONFIG['posture']['desk_distance_threshold']:
                self.posture_warnings.append("TOO CLOSE")
                
            # 二郎腿检测
            if knee_height_diff > GLOBAL_CONFIG['posture']['leg_cross_threshold'] and ankle_crossed:
                self.posture_warnings.append("CROSSED LEGS")

            # 更新警告状态
            self.current_warnings = len(self.posture_warnings)
            self.warnings_label.setText(f"当前警告: {self.current_warnings}")
            
            # 更新姿势状态并触发语音提醒
            current_detected = set(self.posture_warnings)
            
            # 遍历所有姿势状态
            for posture, state in self.posture_states.items():
                warning = posture in current_detected
                self.posture_update.emit(posture, warning)
                
                if warning:
                    # 如果之前未激活，则激活并记录开始时间
                    if not state["active"]:
                        state["active"] = True
                        state["start_time"] = current_time
                    else:
                        # 如果已经激活，检查持续时间是否达到语音提醒的最小持续时间
                        if self.voice_check.isChecked() and current_time - state["start_time"] >= GLOBAL_CONFIG['voice']['min_warning_duration']:
                            # 触发语音提醒
                            self.voice_alerts.add_alert(posture)
                else:
                    # 当前未检测到该姿势，重置状态
                    state["active"] = False
            
            # 有足够多的警告时开始计时
            if self.current_warnings >= GLOBAL_CONFIG['posture']['min_warnings']:
                if self.warning_start_time == 0:  # 第一次检测到警告
                    self.warning_start_time = time.time()
                    logging.info(f"检测到{self.current_warnings}个异常姿势，开始计时...")
                    self.status_update.emit(f"检测到{self.current_warnings}个异常姿势", "#FF9800")
                
                # 计算持续时间
                self.warning_duration = time.time() - self.warning_start_time
                self.duration_label.setText(f"持续时间: {self.warning_duration:.1f}秒")
                
                # 更新进度条
                progress = min(100, int((self.warning_duration / GLOBAL_CONFIG['posture']['min_duration']) * 100))
                self.progress_bar.setValue(progress)
                
                # 冷却时间显示
                cooldown = max(0, GLOBAL_CONFIG['posture']['cooldown'] - (time.time() - self.last_upload_time))
                self.cooldown_label.setText(f"冷却时间: {cooldown:.1f}秒")
                
                # 满足持续时间且冷却期已过
                if (self.warning_duration >= GLOBAL_CONFIG['posture']['min_duration'] and 
                    (time.time() - self.last_upload_time) >= GLOBAL_CONFIG['posture']['cooldown']):
                    self.last_upload_time = time.time()
                    self.warning_start_time = 0  # 重置计时器
                    
                    # 在新线程中处理截图和上传
                    threading.Thread(
                        target=self.handle_capture, 
                        args=(image.copy(),),  # 使用副本避免主线程修改
                        daemon=True
                    ).start()
                    
                    # 重置进度条
                    self.progress_bar.setValue(0)
            else:
                # 警告数量不足，重置计时器
                self.warning_start_time = 0
                self.warning_duration = 0
                self.duration_label.setText("持续时间: 0.0秒")
                self.cooldown_label.setText("冷却时间: 0.0秒")
                self.progress_bar.setValue(0)

            # 可视化
            h, w = image.shape[:2]
            self.mp_drawing.draw_landmarks(image, results.pose_landmarks, self.mp_pose.POSE_CONNECTIONS)
            
            # 显示实时数据
            y_offset = 30
            # FPS显示
            cv2.putText(image, f"FPS: {fps:.2f}", (w-150, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
            y_offset += 30
            
            # 中文部分使用新方法
            image = self.put_chinese_text(image, f"异常姿势: {self.current_warnings}", (10, y_offset),
                                        font_size=20, color=(0, 255, 0) if self.current_warnings < 2 else (0, 0, 255))
            y_offset += 30
            image = self.put_chinese_text(image, f"持续时间: {self.warning_duration:.1f}秒", (10, y_offset),
                                        font_size=20, color=(0, 255, 0) if self.warning_duration < 5 else (0, 0, 255))
            y_offset += 30
            image = self.put_chinese_text(image, 
                                        f"冷却剩余: {max(0, GLOBAL_CONFIG['posture']['cooldown'] - (time.time() - self.last_upload_time)):.1f}秒", 
                                        (10, y_offset),
                                        font_size=20,
                                        color=(0, 255, 0))
            y_offset += 30
            
            # 英文部分保持原样
            cv2.putText(image, f"Hip Angle: {avg_hip_angle:.1f}°", (10, y_offset), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
            y_offset += 30
            cv2.putText(image, f"Spine Angle: {spine_angle:.1f}°", (10, y_offset),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

            # 显示警告
            warning_y = h - 50
            for warning in self.posture_warnings:
                cv2.putText(image, f"{warning} DETECTED!", (10, warning_y), 
                           cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
                warning_y -= 30

            # 新增：绘制错误姿态水印（顶部居中，红色大字体）
            if self.posture_warnings:
                # 转换为中文警告列表
                chinese_warnings = [POSTURE_CHINESE_MAP[warn] for warn in self.posture_warnings]
                # 合并为字符串（如"驼背 | 坐姿倾斜"）
                watermark_text = " | ".join(chinese_warnings)
                # 设置水印位置（顶部居中）
                text_x = int(w / 2)
                text_y = 40  # 顶部偏移量
                # 使用更大的字体（30px）和红色
                image = self.put_chinese_text(image, watermark_text, (text_x, text_y), font_size=30, color=(0, 0, 255))

            # 绘制参考线
            cv2.line(image, 
                     (0, int(GLOBAL_CONFIG['posture']['desk_distance_threshold']*h)), 
                     (w, int(GLOBAL_CONFIG['posture']['desk_distance_threshold']*h)), 
                     (0, 255, 255), 1)
        else:
            # 没有检测到姿势
            cv2.putText(image, "NO POSE DETECTED", (10, 30), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        
        # 转换为Qt图像格式
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        h, w, ch = image.shape
        bytes_per_line = ch * w
        qt_image = QImage(image.data, w, h, bytes_per_line, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(qt_image)
        
        # 缩放图像以适应标签
        scaled_pixmap = pixmap.scaled(
            self.camera_label.width(), 
            self.camera_label.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )
        
        # 显示图像
        self.camera_label.setPixmap(scaled_pixmap)
        
    def manual_capture(self):
        if self.cap is not None and self.cap.isOpened():
            success, image = self.cap.read()
            if success:
                threading.Thread(
                    target=self.handle_capture, 
                    args=(image.copy(),), 
                    daemon=True
                ).start()
                self.status_update.emit("正在上传截图...", "#2196F3")
            else:
                self.status_update.emit("无法获取当前帧", "#f44336")
        else:
            self.status_update.emit("摄像头未启动", "#f44336")
            
    def update_hunch_threshold(self, value):
        GLOBAL_CONFIG['posture']['hunchback_threshold'] = value
        self.hunch_value_label.setText(f"{value}°")
        logging.info(f"更新驼背阈值为: {value}度")
        
    def update_slouch_threshold(self, value):
        GLOBAL_CONFIG['posture']['slouching_threshold'] = value
        self.slouch_value_label.setText(f"{value}°")
        logging.info(f"更新坐姿阈值为: {value}度")
        
    def update_distance_threshold(self, value):
        GLOBAL_CONFIG['posture']['desk_distance_threshold'] = value / 100
        self.distance_value_label.setText(f"{value}%")
        logging.info(f"更新距离阈值为: {value/100:.2f}")
        
    def toggle_cloud_service(self, state):
        GLOBAL_CONFIG['cloud_enabled'] = state == Qt.Checked
        if GLOBAL_CONFIG['cloud_enabled']:
            self.global_state.start_cloud_poller()
            self.status_update.emit("云服务已启用", "#4CAF50")
        else:
            self.global_state.stop_cloud_poller()
            self.status_update.emit("云服务已禁用", "#FF9800")
            
    def handle_command_executed(self, command, source):
        """处理指令执行事件"""
        self.command_executed.emit(command, source)
        
    def close_app(self):
        self.stop_camera()
        self.voice_alerts.stop()
        self.global_state.stop_cloud_poller()
        self.parent().close()
        
    def closeEvent(self, event):
        self.stop_camera()
        self.voice_alerts.stop()
        self.global_state.stop_cloud_poller()
        event.accept()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        
        self.setWindowTitle("智能坐姿监测系统")
       
        # 设置应用图标
        try:
            self.setWindowIcon(QIcon("posture_icon.png"))
        except:
            pass
        
        # 创建主部件
        self.monitor_widget = PostureMonitor()
        self.setCentralWidget(self.monitor_widget)
        
        # 添加状态栏
        self.statusBar().showMessage("就绪")
        
        # 添加菜单
        menubar = self.menuBar()
        file_menu = menubar.addMenu('文件')
        
        exit_action = QAction('退出', self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        
        help_menu = menubar.addMenu('帮助')
        about_action = QAction('关于', self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)
        
        self.setStyleSheet("""
            QMainWindow {
                background-color: #f0f0f0;
            }
            QGroupBox {
                border: 1px solid #ccc;
                border-radius: 5px;
                margin-top: 1ex;
                font-weight: bold;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 3px 0 3px;
            }
            QLabel {
                padding: 2px;
            }
            QPushButton {
                padding: 8px;
                border-radius: 4px;
            }
            QProgressBar {
                border: 1px solid #ccc;
                border-radius: 2px;
                background: white;
            }
            QProgressBar::chunk {
                background-color: #4CAF50;
            }
        """)
        
        # 设置窗口大小为1024x600
        self.setFixedSize(1024, 600)
        
    def show_about(self):
        QMessageBox.about(self, "关于智能坐姿监测系统",
                         "智能坐姿监测系统 v1.0\n\n"
                         "本系统使用计算机视觉技术实时监测用户坐姿，"
                         "当检测到不良坐姿时提供语音提醒并记录截图。\n\n"
                         "功能包括：\n"
                         "- 驼背检测\n"
                         "- 坐姿倾斜检测\n"
                         "- 肩部不平检测\n"
                         "- 屏幕距离过近检测\n"
                         "- 二郎腿检测\n\n"
                         "© 2023 智能健康解决方案")

# ================== 主程序 ==================
if __name__ == '__main__':
    try:
        logging.info("启动智能坐姿监测系统...")
        app = QApplication(sys.argv)
        
        # 设置全局字体
        font = QFont("Microsoft YaHei", 9)
        app.setFont(font)
        
        window = MainWindow()
        window.showFullScreen()  # 全屏显示
        sys.exit(app.exec())
    except Exception as e:
        logging.exception("程序发生严重错误")
        print(f"\n❌❌ 程序失败：{str(e)}")
        exit(1)
