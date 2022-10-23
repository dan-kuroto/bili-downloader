"""
极简B站下载器：
- 优点：
  1. 无广告
  2. 轻量
  3. 能不断维护更新
  4. 因为是开源的，下面列出的所有缺点都可以靠自己改代码解决
- 缺点：
  1. 暂时无登录，无法下载60p及以上分辨率
  2. 不稳定，很容易出现网络问题引发的bug
  3. 暂时只能下载第一个分P
  4. 下载文件的文件名、路径等无法自定义
"""
import aiohttp
import asyncio
from copy import deepcopy
import os
import subprocess
import sys
from typing import Tuple

from PySide2.QtWidgets import QApplication, QWidget, QLineEdit, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QProgressBar, QMessageBox, QTextEdit
from PySide2.QtGui import QFont
from PySide2.QtCore import QThread, Signal
from qtmodern.styles import dark as dark_style
from bilibili_api import video, Credential, HEADERS


# 分片下载用的参数
PIECE = 1 * 1024  # 分片下载的大小的初始值，程序会根据实际情况自动进行动态调整。但初始值对最终的平均下载速度有影响，但也并不是越大越好，初始值太大会导致多次重传，而这会极大地拖慢速度，要实际情况调整，根据我的经验一般8*1024最好，下载效果不好就再缩小一点
SUCCESS_REPEAT = 0  # 仅当重复成功时才允许加快下载速度，而且保险起见必须一次过
# cookie，加上能让你下载1080p以上分辨率，但用多了很危险，而且需要定期更换
SESSDATA = ''
BILI_JCT = ''
BUVID3 = ''


def speed_up():
    """加速并将累计连续成功次数清零，加速上限256K"""
    global PIECE, SUCCESS_REPEAT
    PIECE = min(PIECE + 512, 256 << 10)
    SUCCESS_REPEAT = 0


def speed_down():
    """减速并将累计连续成功次数清零，减速下限512字节"""
    global PIECE, SUCCESS_REPEAT
    PIECE = 512  # max(PIECE - 1024, 512)  # 改成直接降到最低了，慢慢降不行的，会不断遇到重传严重拖慢速度，还不如小水管不断流
    SUCCESS_REPEAT = 0


def retry(num: int):
    """出错后重试，最多重试num次（实际上还加上了动态调整下载速度的功能）"""

    def wrapper(func):

        async def inner(*args, **kwargs):
            result, success = None, False
            global PIECE, SUCCESS_REPEAT

            for i in range(num + 1):
                try:
                    # 运行
                    result = await func(*args, **kwargs)
                    success = True
                    # 调整下载速度
                    if i == 0:  # 一次过，才算“1次成功”
                        SUCCESS_REPEAT += 1
                    if SUCCESS_REPEAT > 1:  # 连续多次成功，当前设定下载速度有余裕，则加快（最后调成1次成功即可了，加速快一点）
                        speed_up()
                except Exception as e:
                    if i < num:  # [0, num)
                        window.logger_text.append(f'遇到错误 {repr(e)}，开始第 {i+1} 次重试……')
                    else:  # i == num
                        window.logger_text.append(f'遇到错误 {repr(e)}，重试次数（{num}）已耗尽！')
                    # 调整下载速度：失败1次，当前设定下载速度凌驾于网速，急需减缓
                    speed_down()
                else:
                    break

            if not success:
                raise RuntimeError(f'已达到最大重试次数！')
            return result

        return inner

    return wrapper


class Data:
    banned_chars = set('\\ / : * ? " < > |'.split())

    def __init__(self, parent: 'Window'):
        self.parent = parent
        self._bvid = ''
        self._owner = ''
        self._title = ''
        self._video_done = 0
        self._video_all = 0
        self._audio_done = 0
        self._audio_all = 0

    @property
    def bvid(self) -> str:
        return self._bvid

    @bvid.setter
    def bvid(self, bvid: str):
        self._bvid = bvid
        self.parent.download_btn.setText(f'下载 {bvid}' if bvid else '下载')

    @property
    def owner(self) -> str:
        return self._owner

    @owner.setter
    def owner(self, owner: str):
        self._owner = owner
        self.parent.owner_label.setText(f'up主：{owner}')

    @property
    def title(self) -> str:
        return self._title

    @title.setter
    def title(self, title: str):
        self._title = title
        self.parent.title_label.setText(f'标题：{title}')

    @property
    def video_done(self) -> int:
        return self._video_done

    @video_done.setter
    def video_done(self, done: int):
        self._video_done = done
        if self.video_all != 0:
            progress = self.video_done / self.video_all * 100
            self.parent.video_bar.setValue(int(progress))
            self.parent.video_bar.setFormat(f'视频进度 {progress :.2f}%')
        else:
            self.parent.video_bar.setValue(0)
            self.parent.video_bar.setFormat('视频进度')

    @property
    def video_all(self) -> int:
        return self._video_all

    @video_all.setter
    def video_all(self, all: int):
        self._video_all = all

    @property
    def audio_done(self) -> int:
        return self._audio_done

    @audio_done.setter
    def audio_done(self, done: int):
        self._audio_done = done
        if self.audio_all != 0:
            progress = self.audio_done / self.audio_all * 100
            self.parent.audio_bar.setValue(int(progress))
            self.parent.audio_bar.setFormat(f'音频进度 {progress :.2f}%')
        else:
            self.parent.audio_bar.setValue(0)
            self.parent.audio_bar.setFormat('音频进度')

    @property
    def audio_all(self) -> int:
        return self._audio_all

    @audio_all.setter
    def audio_all(self, all: int):
        self._audio_all = all

    @staticmethod
    def remove_banned_chars(s: str) -> str:
        li = [c for c in s if c not in Data.banned_chars]
        return ''.join(li)


class GetInfoThread(QThread):
    info_got = Signal(dict)
    error_msg = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.video: video.Video = None

    async def get_info(self):
        try:
            info = await self.video.get_info()
        except Exception as e:
            self.error_msg.emit(repr(e))
            info = {}
        self.info_got.emit(info)

    def run(self) -> None:
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())
        asyncio.get_event_loop().run_until_complete(self.get_info())


class DownloadThread(QThread):
    downloaded = Signal()
    video_done = Signal(int)
    video_all = Signal(int)
    audio_done = Signal(int)
    audio_all = Signal(int)
    error_msg = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.video: video.Video = None

    @retry(5)
    async def download_piece(self, sess: aiohttp.ClientSession, url: str, start: int, end: int) -> Tuple[bytes, int]:
        """
        分段下载 bytes={start}-{end} 并返回 (bytes, total_length)
        """
        headers = deepcopy(HEADERS)
        headers['range'] = f'bytes={start}-{end}'
        async with sess.get(url, headers=headers) as resp:
            bs = await resp.content.read()  # 就是要存内存里，直接写文件就不方便断点续传了
            return bs, int(resp.headers['Content-Range'].split('/')[-1])

    async def download_media(self, sess: aiohttp.ClientSession, url: str, mode: str):
        """
        :param mode: enum('video', 'audio')
        """
        all_signal = self.video_all if mode == 'video' else self.audio_all
        done_signal = self.video_done if mode == 'video' else self.audio_done

        done = 0
        done_signal.emit(done)
        with open(f'{mode}_temp.m4s', 'wb') as file:
            start, end = 0, 1024
            while True:
                # download by piece
                bs, length = await self.download_piece(sess, url, start, end)
                if start == 0:  # init total length
                    all_signal.emit(length)
                file.write(bs)
                done += len(bs)
                done_signal.emit(done)
                # break if finish
                if done == length:
                    break
                # prepare for next piece
                start, end = end + 1, end + PIECE
                if end > length - 1:
                    end = length - 1

    async def download(self):
        try:
            url = await self.video.get_download_url(0)
            async with aiohttp.ClientSession() as sess:
                # create tasks
                download_tasks = []  # 不敢在分片的地方异步，但音视频相对独立，就没问题了
                for mode in ['video', 'audio']:  # 音视频下载的代码长得差不多还重写两遍也太浪费了
                    if os.path.exists(f'{mode}_temp.m4s'):
                        os.remove(f"{mode}_temp.m4s")
                    download_url = url["dash"][mode][0]['baseUrl']
                    download_tasks.append(
                        asyncio.create_task(self.download_media(sess, download_url, mode))
                    )
                # download video&audio asynchronously
                await asyncio.wait(download_tasks)
            self.downloaded.emit()
        except Exception as e:
            self.error_msg.emit(repr(e))
        finally:  # 不管下载成功失败，这个调整下载速度的系统都要重置一下
            PIECE = 8 * 1024
            SUCCESS_REPEAT = 0

    def run(self) -> None:
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())
        asyncio.get_event_loop().run_until_complete(self.download())


class MixThread(QThread):
    msg = Signal(str)
    end = Signal()

    def __init__(self):
        super().__init__()
        self.cmd = 'ping www.bilibili.com'  # for test
        self.path = ''

    def run(self):
        process = subprocess.Popen(
            self.cmd,
            shell=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding='gbk',
            text=True
        )
        self.msg.emit('开始混流……')
        while process.poll() is None:
            msg = process.stdout.readline()
            if msg:
                self.msg.emit(msg)
        if process.poll() != 0:
            msg = process.stderr.read()
            if msg:
                self.msg.emit(msg)
        self.msg.emit(f'混流结束！由于ffmpeg输出信息无法重定向到此处，请自行检查此路径：\n{os.path.abspath(self.path)}')
        self.end.emit()


class Window(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('B站下载器')
        self.setMinimumSize(1000, 375)
        self.resize(1000, 375)

        self.bvid_edit = QLineEdit()
        self.bvid_edit.setPlaceholderText('在这里输入BV号然后回车')
        self.bvid_edit.returnPressed.connect(self.enter_handler)
        self.title_label = QLabel()
        self.title_label.setWordWrap(True)
        self.owner_label = QLabel()
        self.owner_label.setWordWrap(True)
        self.download_btn = QPushButton()
        self.download_btn.setEnabled(False)
        self.download_btn.clicked.connect(self.download_btn_handler)
        self.video_bar = QProgressBar()
        self.audio_bar = QProgressBar()
        self.mix_btn = QPushButton('混流（依赖ffmpeg）')
        self.mix_btn.clicked.connect(self.mix)
        self.logger_text = QTextEdit()
        self.logger_text.setReadOnly(True)
        self.logger_text.setFont(QFont('Microsoft YaHei', 11))

        self.data = Data(self)
        self.data.owner = ''
        self.data.title = ''
        self.data.bvid = ''
        self.data.video_done = 0
        self.data.video_all = 0
        self.data.audio_done = 0
        self.data.audio_all = 0
        self.credential = Credential(sessdata=SESSDATA, bili_jct=BILI_JCT, buvid3=BUVID3)
        self.video: video.Video = None

        self.get_info_thread = GetInfoThread()
        self.get_info_thread.info_got.connect(self.info_got_handler)
        self.get_info_thread.error_msg.connect(lambda msg: QMessageBox.warning(self, '获取信息失败！', msg))
        self.download_thread = DownloadThread()
        self.download_thread.downloaded.connect(self.downloaded_handler)
        self.download_thread.video_done.connect(self.set_video_done)
        self.download_thread.video_all.connect(self.set_video_all)
        self.download_thread.audio_done.connect(self.set_audio_done)
        self.download_thread.audio_all.connect(self.set_audio_all)
        self.download_thread.error_msg.connect(lambda msg: QMessageBox.warning(self, '下载失败！', msg))
        self.mix_thread = MixThread()
        self.mix_thread.msg.connect(lambda msg: self.logger_text.append(msg))
        self.mix_thread.end.connect(lambda: self.mix_btn.setEnabled(True))

        layout_left = QVBoxLayout()
        for widget in [self.bvid_edit, self.title_label, self.owner_label, self.download_btn, self.video_bar, self.audio_bar, self.mix_btn]:
            layout_left.addWidget(widget)
        layout = QHBoxLayout()
        layout.addLayout(layout_left)
        layout.addWidget(self.logger_text)
        self.setLayout(layout)

    def enter_handler(self):
        """get info"""
        if not self.bvid_edit.text():
            return
        try:
            self.video = video.Video(bvid=self.bvid_edit.text(), credential=self.credential)
        except Exception as e:
            QMessageBox.warning(self, 'BV号错误！', repr(e))
            return
        self.get_info_thread.video = self.video
        self.get_info_thread.start()
        self.bvid_edit.setEnabled(False)

    def info_got_handler(self, info):
        """
        :param info: Dict[str, str | Dict[str, str]]
        """
        if info:
            self.data.owner = info['owner']['name']
            self.data.title = info['title']
            self.data.bvid = self.bvid_edit.text()
            self.download_btn.setEnabled(True)  # 若成功才能允许下载
        self.bvid_edit.setEnabled(True)

    def download_btn_handler(self):
        self.download_btn.setEnabled(False)
        self.download_thread.video = self.video
        self.download_thread.start()

    def downloaded_handler(self):
        self.download_btn.setEnabled(True)

    def set_video_done(self, done: int):
        self.data.video_done = done

    def set_video_all(self, all: int):
        self.data.video_all = all

    def set_audio_done(self, done: int):
        self.data.audio_done = done

    def set_audio_all(self, all: int):
        self.data.audio_all = all

    def mix(self):
        if not self.data.bvid or not self.data.owner or not self.data.title:
            QMessageBox.warning(self, '错误！', '你不先根据BV号查一下视频信息的话，可没法确定下载的文件名哦~')
            return
        if not os.path.exists(self.data.owner):
            os.mkdir(self.data.owner)
        path = f'{self.data.owner}/{self.data.title} - {self.data.bvid}.mp4'
        if os.path.exists(path):
            os.remove(path)

        self.mix_thread.path = path
        self.mix_thread.cmd = f'ffmpeg -i video_temp.m4s -i audio_temp.m4s -vcodec copy -acodec copy "{self.data.owner}/{self.data.title} - {self.data.bvid}.mp4"'
        self.mix_btn.setEnabled(False)
        self.mix_thread.start()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    dark_style(app)
    app.setFont(QFont('Microsoft YaHei', 15))
    window = Window()
    window.show()
    sys.exit(app.exec_())
