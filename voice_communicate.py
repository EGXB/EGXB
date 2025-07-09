import sys
import websocket
import datetime
import hashlib
import base64
import hmac
import json
from urllib.parse import urlencode
import time
import ssl
from wsgiref.handlers import format_date_time
from datetime import datetime
from time import mktime
import _thread as thread
import sounddevice as sd
import tempfile
import os
from openai import OpenAI
import re
import pyttsx3

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QVBoxLayout, QHBoxLayout,
    QWidget, QTextEdit, QLabel, QSpinBox, QProgressBar
)
from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtGui import QFont, QColor, QPalette
from text_to_voice import text_to_speech

# 讯飞语音听写参数
STATUS_FIRST_FRAME = 0  # 第一帧标识
STATUS_CONTINUE_FRAME = 1  # 中间帧标识
STATUS_LAST_FRAME = 2  # 最后一帧标识

# Deepseek AI 参数
DEEPSEEK_API_KEY = ""
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# 讯飞API参数 (请替换为您的实际参数)
APPID = ''  # 应用ID
APISecret = ''  # API密钥
APIKey = ''  # API密钥


class Ws_Param(object):
    """WebSocket参数配置类"""

    def __init__(self, APPID, APIKey, APISecret, AudioFile):
        self.APPID = APPID
        self.APIKey = APIKey
        self.APISecret = APISecret
        self.AudioFile = AudioFile
        self.CommonArgs = {"app_id": self.APPID}  # 通用参数
        # 业务参数：领域、语言、口音等
        self.BusinessArgs = {"domain": "iat", "language": "zh_cn", "accent": "mandarin", "vinfo": 1, "vad_eos": 10000}

    def create_url(self):
        """创建WebSocket URL"""
        url = 'wss://ws-api.xfyun.cn/v2/iat'
        now = datetime.now()
        date = format_date_time(mktime(now.timetuple()))  # 生成时间戳

        # 生成签名
        signature_origin = "host: " + "ws-api.xfyun.cn" + "\n"
        signature_origin += "date: " + date + "\n"
        signature_origin += "GET " + "/v2/iat " + "HTTP/1.1"
        signature_sha = hmac.new(self.APISecret.encode('utf-8'), signature_origin.encode('utf-8'),
                                 digestmod=hashlib.sha256).digest()
        signature_sha = base64.b64encode(signature_sha).decode(encoding='utf-8')

        # 生成授权信息
        authorization_origin = "api_key=\"%s\", algorithm=\"%s\", headers=\"%s\", signature=\"%s\"" % (
            self.APIKey, "hmac-sha256", "host date request-line", signature_sha)
        authorization = base64.b64encode(authorization_origin.encode('utf-8')).decode(encoding='utf-8')

        # 构建URL参数
        v = {
            "authorization": authorization,
            "date": date,
            "host": "ws-api.xfyun.cn"
        }
        url = url + '?' + urlencode(v)
        return url


class VoiceRecognitionThread(QThread):
    """语音识别线程类"""
    # 定义信号
    recognition_result = Signal(str)  # 语音识别结果信号
    ai_response_result = Signal(str)  # AI响应结果信号
    error_occurred = Signal(str)  # 错误发生信号
    recording_progress = Signal(int)  # 录音进度信号
    finished = Signal()  # 完成信号
    recording_started = Signal()  # 新增：录音开始信号

    def __init__(self, duration=10):
        super().__init__()
        self.duration = duration  # 录音时长
        self.temp_path = None  # 临时文件路径
        self._is_recording = False  # 录音状态标志
        self._stop_recording = False  # 停止录音标志

    def run(self):
        """线程主函数"""
        self._is_recording = True
        self._stop_recording = False
        self.temp_path = None
        try:
            # 发出录音开始信号（用于禁用按钮）
            self.recording_started.emit()

            # 录音处理
            print("🎙️ 正在录音...")
            fs = 16000  # 采样率
            audio_data = []  # 音频数据列表

            # 录音回调函数
            def callback(indata, frames, time, status):
                if status:
                    print(status)
                audio_data.append(indata.copy())
                # 计算并发送进度
                progress = int((len(audio_data) * frames / fs) / self.duration * 100)
                self.recording_progress.emit(progress)
                if self._stop_recording:
                    raise sd.CallbackStop()

            # 开始录音
            with sd.InputStream(samplerate=fs, channels=1, dtype='int16', callback=callback):
                sd.sleep(int(self.duration * 1000))

            print("✅ 录音完成！")

            # 检查是否有录音数据
            if not audio_data:
                self.error_occurred.emit("录音失败或中断。")
                self._is_recording = False
                self.finished.emit()
                return

            # 合并音频数据
            import numpy as np
            audio = np.concatenate(audio_data, axis=0)

            # 创建临时文件保存音频数据
            temp_file = tempfile.NamedTemporaryFile(mode='w+b', suffix='.pcm', delete=False)
            temp_file.write(audio.tobytes())
            temp_file.flush()
            self.temp_path = temp_file.name
            temp_file.close()

            # 语音识别结果
            result = ""

            # WebSocket消息处理回调
            def on_message(ws, message):
                try:
                    data = json.loads(message)
                    code = data["code"]
                    sid = data["sid"]
                    if code != 0:  # 错误处理
                        errMsg = data["message"]
                        self.error_occurred.emit(f"sid:{sid} call error:{errMsg} code is:{code}")
                    else:  # 成功处理
                        ws_data = data["data"]["result"]["ws"]
                        temp = ""
                        # 拼接识别结果
                        for i in ws_data:
                            for w in i["cw"]:
                                temp += w["w"]
                        nonlocal result
                        result += temp
                        self.recognition_result.emit(result)  # 发送识别结果
                except Exception as e:
                    self.error_occurred.emit(f"receive msg,but parse exception:{e}")

            # WebSocket错误处理回调
            def on_error(ws, error):
                self.error_occurred.emit(f"WebSocket error: {error}")

            # WebSocket关闭回调
            def on_close(ws, code, msg):
                print("### closed ###")

            # WebSocket打开回调
            def on_open(ws):
                # 发送音频数据的线程函数
                def send_audio():
                    frameSize = 8000  # 帧大小
                    intervel = 0.04  # 发送间隔
                    status = STATUS_FIRST_FRAME  # 初始状态
                    try:
                        with open(self.temp_path, "rb") as fp:
                            while True:
                                buf = fp.read(frameSize)
                                if not buf:  # 无数据则标记为最后一帧
                                    status = STATUS_LAST_FRAME

                                # 根据状态构建数据包
                                if status == STATUS_FIRST_FRAME:
                                    d = {"common": wsParam.CommonArgs,
                                         "business": wsParam.BusinessArgs,
                                         "data": {"status": 0, "format": "audio/L16;rate=16000",
                                                  "audio": str(base64.b64encode(buf), 'utf-8'),
                                                  "encoding": "raw"}}
                                elif status == STATUS_CONTINUE_FRAME:
                                    d = {"data": {"status": 1, "format": "audio/L16;rate=16000",
                                                  "audio": str(base64.b64encode(buf), 'utf-8'),
                                                  "encoding": "raw"}}
                                elif status == STATUS_LAST_FRAME:
                                    d = {"data": {"status": 2, "format": "audio/L16;rate=16000",
                                                  "audio": str(base64.b64encode(buf), 'utf-8'),
                                                  "encoding": "raw"}}
                                    ws.send(json.dumps(d))
                                    time.sleep(1)
                                    break
                                ws.send(json.dumps(d))
                                time.sleep(intervel)
                    except Exception as e:
                        self.error_occurred.emit(f"Error sending audio: {e}")
                    finally:
                        ws.close()  # 关闭连接

                # 启动发送音频的线程
                thread.start_new_thread(send_audio, ())

            # 创建WebSocket参数
            wsParam = Ws_Param(APPID=APPID, APIKey=APIKey, APISecret=APISecret, AudioFile=self.temp_path)
            wsUrl = wsParam.create_url()
            # 创建WebSocket应用
            ws = websocket.WebSocketApp(wsUrl, on_message=on_message, on_error=on_error, on_close=on_close)
            ws.on_open = on_open
            # 运行WebSocket
            ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})

            # AI响应处理
            if result:
                try:
                    # 添加用户消息到对话历史
                    global messages
                    messages.append({"role": "user", "content": f"{result}"})

                    # 创建OpenAI客户端
                    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

                    # 调用Deepseek API
                    response = client.chat.completions.create(
                        model="deepseek-chat",
                        messages=messages,
                        stream=False
                    )

                    # 获取AI响应
                    ai_response = response.choices[0].message.content

                    # 添加AI响应到对话历史
                    messages.append({"role": "assistant", "content": ai_response})

                    # 发送AI响应
                    self.ai_response_result.emit(ai_response)
                except Exception as e:
                    self.error_occurred.emit(f"AI request failed: {e}")
            else:
                self.ai_response_result.emit("未识别到语音，无法生成AI回答。")

        except Exception as e:
            self.error_occurred.emit(f"An error occurred: {e}")
        finally:
            # 清理临时文件
            if self.temp_path and os.path.exists(self.temp_path):
                os.remove(self.temp_path)
                self.temp_path = None
            self._is_recording = False
            self.finished.emit()  # 发送完成信号

    def stop_recording(self):
        """停止录音"""
        self._stop_recording = True

    def is_recording(self):
        """检查是否正在录音"""
        return self._is_recording


class ModernVoiceAssistant(QMainWindow):
    """主窗口类"""

    def __init__(self):
        super().__init__()
        # 窗口设置
        self.setWindowTitle("智能语音助手")
        
        # 设置窗口大小为1024x600
        self.resize(1024, 600)
        
        self.setStyleSheet("""
            QMainWindow {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                          stop:0 #f5f7fa, stop:1 #c3cfe2);
            }
            QTextEdit {
                background-color: rgba(255, 255, 255, 0.9);
                border: 2px solid #e0e0e0;
                border-radius: 12px;
                padding: 15px;
                font-size: 14px;
                color: #333;
            }
            QLabel#title {
                color: #3a4a6d;
                font-size: 28px;
                font-weight: bold;
                margin-bottom: 20px;
            }
        """)

        self.thread = None  # 语音识别线程
        # 在调用 setup_ui() 之前初始化状态标志
        self.stop_state = True  # 停止状态标志
        self.is_processing = False  # 处理状态标志
        self.is_playing = False  # 语音播放状态标志
        self.setup_ui()  # 初始化UI

    def setup_ui(self):
        """初始化UI界面"""
        # 创建中央部件
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # 主布局
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(30, 30, 30, 30)  # 边距
        main_layout.setSpacing(20)  # 间距
         # 添加退出按钮
        exit_button = QPushButton("退出")
        exit_button.setObjectName("exit_button")  # 设置对象名以便样式表应用
        exit_button.setFixedHeight(50)
        exit_button.clicked.connect(self.close)  # 连接关闭事件
        # 标题
        title_label = QLabel("智能语音助手")
        title_label.setObjectName("title")
        title_label.setAlignment(Qt.AlignCenter)  # 居中
        main_layout.addWidget(title_label)

        # 语音识别区域
        self.recognition_text = QTextEdit()
        self.recognition_text.setPlaceholderText("语音识别结果将显示在这里...")
        self.recognition_text.setMinimumHeight(100)  # 减小高度以适应新窗口尺寸
        main_layout.addWidget(self.recognition_text)

        # AI响应区域
        self.ai_response_text = QTextEdit()
        self.ai_response_text.setPlaceholderText("AI回答将显示在这里...")
        self.ai_response_text.setMinimumHeight(100)  # 减小高度以适应新窗口尺寸
        main_layout.addWidget(self.ai_response_text)

        # 控制按钮布局
        control_layout = QHBoxLayout()
        control_layout.setSpacing(15)  # 按钮间距
        control_layout.addWidget(exit_button)
        # 录音按钮
        self.record_button = QPushButton("🎤 开始录音")
        self.record_button.setFixedHeight(60)  # 固定高度
        # 按钮样式
        self.record_button.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                          stop:0 #4facfe, stop:1 #00f2fe);
                color: white;
                border-radius: 15px;
                font-size: 16px;
                font-weight: bold;
                padding: 10px 20px;
                border: none;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                          stop:0 #3aa1f0, stop:1 #00d9e8);
            }
            QPushButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                          stop:0 #2d8fd8, stop:1 #00c0d0);
            }
        """)
        # 连接点击事件
        self.record_button.clicked.connect(self.start_recording)
        control_layout.addWidget(self.record_button)

        # 清空按钮
        self.clear_button = QPushButton("清空")
        self.clear_button.setFixedHeight(50)
        self.clear_button.setStyleSheet(
            "QPushButton { background-color: #f44336; color: white; border: none; border-radius: 8px; }"
            "QPushButton:hover { background-color: #d32f2f; }"
            "QPushButton:pressed { background-color: #b71c1c; }"
        )
        self.clear_button.setFont(QFont("Arial", 14, QFont.Bold))  # 字体设置
        self.clear_button.clicked.connect(self.clear_results)  # 连接点击事件
        control_layout.addWidget(self.clear_button)

        # 录音时长标签
        duration_label = QLabel("录音时长 (秒):")
        duration_label.setFont(QFont("Arial", 12))  # 字体设置
        control_layout.addWidget(duration_label)

        # 录音时长选择框
        self.duration_spinbox = QSpinBox()
        self.duration_spinbox.setMinimum(1)  # 最小值
        self.duration_spinbox.setMaximum(60)  # 最大值
        self.duration_spinbox.setValue(10)  # 默认值
        self.duration_spinbox.setFixedWidth(80)  # 固定宽度
        self.duration_spinbox.setFont(QFont("Arial", 12))  # 字体设置
        control_layout.addWidget(self.duration_spinbox)

        # 添加控制布局到主布局
        main_layout.addLayout(control_layout)

        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)  # 显示文本
        self.progress_bar.setFixedHeight(20)  # 固定高度
        # 进度条样式
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 2px solid #e0e0e0;
                border-radius: 10px;
                text-align: center;
                height: 25px;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                          stop:0 #a18cd1, stop:1 #fbc2eb);
                border-radius: 8px;
            }
        """)
        main_layout.addWidget(self.progress_bar)

        # 状态标签
        self.status_label = QLabel("准备就绪")
        self.status_label.setFont(QFont("Arial", 10))
        # 状态标签样式
        self.status_label.setStyleSheet("""
            QLabel {
                color: #666;
                font-size: 12px;
                padding: 5px;
                background: rgba(255,255,255,0.7);
                border-radius: 8px;
            }
        """)
        main_layout.addWidget(self.status_label)

        # 初始化UI状态
        self.set_ui_enabled(True)

    def play_voice(self, text):
        """语音播放函数"""
        try:
            # 在语音播放前保持录音按钮禁用状态
            self.record_button.setText("语音播放中...")
            self.record_button.setEnabled(False)
            self.status_label.setText("语音播放中...")
            
            # 播放语音
            text_to_speech(text)
            
        except Exception as e:
            print(f"语音播放出错: {e}")
        finally:
            # 无论是否成功播放，都在最后结束处理状态并启用录音按钮
            self.is_playing = False
            self.is_processing = False
            self.record_button.setEnabled(True)
            self.record_button.setText("🎤 开始录音")
            # 恢复按钮样式
            self.record_button.setStyleSheet("""
                QPushButton {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                              stop:0 #4facfe, stop:1 #00f2fe);
                    color: white;
                    border-radius: 15px;
                    font-size: 16px;
                    font-weight: bold;
                    padding: 10px 20px;
                    border: none;
                }
                QPushButton:hover {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                              stop:0 #3aa1f0, stop:1 #00d9e8);
                }
                QPushButton:pressed {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                              stop:0 #2d8fd8, stop:1 #00c0d0);
                }
            """)
            self.status_label.setText("准备就绪")

    @Slot()
    def start_recording(self):
        """开始录音按钮点击事件处理"""
        # 如果正在处理中，忽略点击
        if self.is_processing or self.is_playing:
            return

        # 设置处理状态标志
        self.is_processing = True

        # 检查是否有正在运行的线程
        if self.thread and self.thread.isRunning():
            self.status_label.setText("正在停止录音...")
            self.thread.stop_recording()  # 停止录音
            return

        # 清除结果
        self.clear_results()
        # 禁用UI控件
        self.set_ui_enabled(False)
        self.status_label.setText("开始录音...")
        self.progress_bar.setValue(0)  # 重置进度条

        # 获取录音时长
        duration = self.duration_spinbox.value()

        # 创建语音识别线程
        self.thread = VoiceRecognitionThread(duration=duration)

        # 连接线程信号
        self.thread.recognition_result.connect(self.update_recognition_result)
        self.thread.ai_response_result.connect(self.update_ai_response_result)
        self.thread.error_occurred.connect(self.display_error)
        self.thread.recording_progress.connect(self.update_progress)
        self.thread.finished.connect(self.on_thread_finished)
        self.thread.recording_started.connect(self.on_recording_started)  # 连接录音开始信号

        # 启动线程
        self.thread.start()

        # 更新按钮状态
        self.record_button.setText("处理中...")
        self.record_button.setStyleSheet(
            "QPushButton { background-color: #FF9800; color: white; border: none; border-radius: 8px; }"
            "QPushButton:hover { background-color: #F57C00; }"
            "QPushButton:pressed { background-color: #EF6C00; }"
            "QPushButton:disabled { background-color: #cccccc; }"
        )

    @Slot()
    def on_recording_started(self):
        """录音开始信号处理"""
        # 禁用录音按钮，直到整个处理流程结束
        self.record_button.setEnabled(False)
        self.record_button.setText("处理中...")

    @Slot(str)
    def update_recognition_result(self, result):
        """更新语音识别结果"""
        self.recognition_text.setPlainText(result)
        self.status_label.setText("语音识别中...")

    @Slot(str)
    def update_ai_response_result(self, response):
        """更新AI响应结果并播放语音"""
        # 先更新文本框显示
        self.ai_response_text.setPlainText(response)
        self.status_label.setText("AI 回答完成。")
        
        # 设置播放状态
        self.is_playing = True
        
        # 播放语音
        self.play_voice(response)

    @Slot(str)
    def display_error(self, error_message):
        """显示错误信息"""
        self.status_label.setText(f"错误: {error_message}")
        # 错误处理中也要结束处理状态
        self.is_processing = False
        self.is_playing = False
        # 启用UI
        self.set_ui_enabled(True)
        # 恢复按钮状态
        self.record_button.setText("开始录音")
        self.record_button.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                          stop:0 #4facfe, stop:1 #00f2fe);
                color: white;
                border-radius: 15px;
                font-size: 16px;
                font-weight: bold;
                padding: 10px 20px;
                border: none;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                          stop:0 #3aa1f0, stop:1 #00d9e8);
            }
            QPushButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                          stop:0 #2d8fd8, stop:1 #00c0d0);
            }
        """)

    @Slot(int)
    def update_progress(self, value):
        """更新录音进度"""
        self.progress_bar.setValue(value)
        if value < 100:
            self.status_label.setText(f"录音中... {value}%")
        else:
            self.status_label.setText("录音完成，正在处理...")

    @Slot()
    def on_thread_finished(self):
        """线程完成时处理"""
        # 注意：此时只是语音识别和AI处理完成，语音播放可能还在进行
        # 启用其他UI控件（录音按钮在语音播放结束后启用）
        self.clear_button.setEnabled(True)
        self.duration_spinbox.setEnabled(True)

        # 更新状态标签
        if self.status_label.text().startswith("录音中") or self.status_label.text() == "开始录音...":
            self.status_label.setText("处理完成，正在播放语音...")

    @Slot()
    def clear_results(self):
        """清除结果"""
        self.recognition_text.clear()
        self.ai_response_text.clear()
        self.status_label.setText("准备就绪")
        self.progress_bar.setValue(0)  # 重置进度条

    def set_ui_enabled(self, enabled):
        """设置UI控件启用状态"""
        # 当语音播放时保持禁用状态
        if not self.is_playing:
            self.record_button.setEnabled(enabled)
            self.clear_button.setEnabled(enabled)
            self.duration_spinbox.setEnabled(enabled)


if __name__ == "__main__":
    # 初始化对话历史
    messages = [{"role": "system",
                 "content": "你是一个简易的智能回答助手,回答要求简洁，不要回答表情，只要回答纯文本,必须简洁干练"}]

    # 创建应用
    app = QApplication(sys.argv)
    # 创建主窗口
    window = ModernVoiceAssistant()
    window.show()  # 使用普通show而不是全屏
    
    # 运行应用
    sys.exit(app.exec())
