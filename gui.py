import aiohttp
import asyncio
import argparse
from copy import deepcopy
import os
import sys
from typing import Tuple

from PySide2.QtWidgets import QApplication, QWidget, QLineEdit, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QFormLayout, QProgressBar, QMessageBox, QTextEdit
from PySide2.QtGui import QFont
from PySide2.QtCore import QThread, Signal
from qtmodern.styles import dark as dark_style
from bilibili_api import video, Credential, HEADERS, settings


# 分片下载用的参数
PIECE = 1 * 1024  # 分片下载的大小的初始值，程序会根据实际情况自动进行动态调整。但初始值对最终的平均下载速度有影响，但也并不是越大越好，初始值太大会导致多次重传，而这会极大地拖慢速度，要实际情况调整，根据我的经验一般8*1024最好，下载效果不好就再缩小一点
SUCCESS_REPEAT = 0  # 仅当重复成功时才允许加快下载速度，而且保险起见必须一次过


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
                        window.log_text.append(f'遇到错误 {repr(e)}，开始第 {i+1} 次重试……')
                    else:  # i == num
                        window.log_text.append(f'遇到错误 {repr(e)}，重试次数（{num}）已耗尽！')
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
        self._pid = 0  # 分P号
        self._owner = ''
        self._title = ''  # 界面上显示的标题
        self._grand_title = ''  # 视频标题
        self._sub_title = ''  # 分P标题
        self._is_multi = False  # 是否是多P视频
        self._video_done = 0
        self._video_all = 0
        self._audio_done = 0
        self._audio_all = 0
        # cookie，加上能让你下载1080p以上分辨率，但用多了很危险，而且需要定期更换
        self._sessdata = ''
        self._bili_jct = ''
        self._buvid3 = ''

    @property
    def sessdata(self) -> str:
        return self._sessdata

    @sessdata.setter
    def sessdata(self, sessdata: str):
        self._sessdata = sessdata

    @property
    def bili_jct(self) -> str:
        return self._bili_jct

    @bili_jct.setter
    def bili_jct(self, bili_jct: str):
        self._bili_jct = bili_jct

    @property
    def buvid3(self) -> str:
        return self._buvid3

    @buvid3.setter
    def buvid3(self, buvid3: str):
        self._buvid3 = buvid3

    @property
    def bvid(self) -> str:
        return self._bvid

    @bvid.setter
    def bvid(self, bvid: str):
        self._bvid = bvid
        self.parent.download_btn.setText(f'下载 {bvid} p{self.pid + 1}' if bvid else '下载')

    @property
    def pid(self) -> int:
        return self._pid

    @pid.setter
    def pid(self, pid: int):
        self._pid = pid

    @property
    def grand_title(self) -> str:
        return self._grand_title

    @grand_title.setter
    def grand_title(self, grand_title: str):
        self._grand_title = grand_title

    @property
    def sub_title(self) -> str:
        return self._sub_title

    @sub_title.setter
    def sub_title(self, sub_title: str):
        self._sub_title = sub_title

    @property
    def is_multi(self) -> bool:
        return self._is_multi

    @is_multi.setter
    def is_multi(self, is_multi: bool):
        self._is_multi = is_multi

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
        async with sess.get(url, headers=headers, proxy=args.proxy) as resp:
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
            url = await self.video.get_download_url(window.data.pid)
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
            global PIECE, SUCCESS_REPEAT
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

    async def _main(self):
        process = await asyncio.subprocess.create_subprocess_shell(
            self.cmd,
            stderr=asyncio.subprocess.PIPE,  # 注意，ffmpeg所有输出都在stderr里，所以之前用stdout才获取不到输出
        )
        self.msg.emit('开始混流，请稍等片刻……')
        if process.stderr is None:
            raise RuntimeError()
        while process.returncode is None:  # 用返回代码是否为None来判断子进程是否结束
            data = await process.stderr.readline()
            self.msg.emit(data.decode('utf-8', errors='ignore').strip())
            await asyncio.sleep(0.01)  # 不知为什么这里不sleep就会出很奇怪的错误
        self.msg.emit(f'退出代码为 {process.returncode}')
        self.end.emit()

    def run(self):
        try:
            asyncio.get_event_loop()
        except RuntimeError:  # 不知道为什么，这里不能用new_event_loop，否则后面会报错NotImplementedError，明明我另一个项目里可以的啊……
            asyncio.set_event_loop(asyncio.ProactorEventLoop())
        asyncio.get_event_loop().run_until_complete(self._main())


class SettingWindow(QWidget):
    def __init__(self, main: 'Window'):
        super().__init__()
        self.main = main
        self.setWindowTitle('设置')
        self.sessdata_label = QLabel('SESSDATA')
        self.sessdata_text = QLineEdit()
        self.bili_jct_label = QLabel('bili_jct')
        self.bili_jct_text = QLineEdit()
        self.buvid3_label = QLabel('buvid3')
        self.buvid3_text = QLineEdit()
        self.submit_btn = QPushButton('确认')
        self.submit_btn.clicked.connect(self.submit)
        self.cancel_btn = QPushButton('取消')
        self.cancel_btn.clicked.connect(self.cancel)
        layout = QVBoxLayout()
        form_box = QFormLayout()
        form_box.addRow(self.sessdata_label, self.sessdata_text)
        form_box.addRow(self.bili_jct_label, self.bili_jct_text)
        form_box.addRow(self.buvid3_label, self.buvid3_text)
        layout.addLayout(form_box)
        bottom_box = QHBoxLayout()
        bottom_box.addWidget(self.submit_btn)
        bottom_box.addWidget(self.cancel_btn)
        layout.addLayout(bottom_box)
        self.setLayout(layout)

    def cancel(self):
        self.sessdata_text.setText(self.main.data.sessdata)
        self.bili_jct_text.setText(self.main.data.bili_jct)
        self.buvid3_text.setText(self.main.data.buvid3)
        self.hide()

    def submit(self):
        self.main.data.sessdata = self.sessdata_text.text()
        self.main.data.bili_jct = self.bili_jct_text.text()
        self.main.data.buvid3 = self.buvid3_text.text()
        self.main.credential = Credential(sessdata=self.main.data.sessdata, bili_jct=self.main.data.bili_jct, buvid3=self.main.data.buvid3)
        self.hide()


class Window(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('B站下载器')
        self.setMinimumSize(600, 450)
        self.resize(600, 450)

        self.bvid_edit = QLineEdit()
        self.bvid_edit.setPlaceholderText('输入BV号后回车，要指定分p号就空格加在后面')
        self.bvid_edit.returnPressed.connect(self.enter_handler)
        self.title_label = QLabel()
        self.title_label.setWordWrap(True)
        self.owner_label = QLabel()
        self.owner_label.setWordWrap(True)
        self.setting_window = SettingWindow(self)
        self.download_btn = QPushButton()
        self.download_btn.setEnabled(False)
        self.download_btn.clicked.connect(self.download_btn_handler)
        self.video_bar = QProgressBar()
        self.audio_bar = QProgressBar()
        self.mix_btn = QPushButton('混流')
        self.mix_btn.setToolTip('*依赖ffmpeg')
        self.mix_btn.clicked.connect(self.mix)
        self.setting_btn = QPushButton('设置')
        self.setting_btn.clicked.connect(self.setting_window.show)
        self.log_text = QTextEdit()
        self.log_text.setPlaceholderText('日志 ...')
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont('Microsoft YaHei', 11))

        self.data = Data(self)
        self.data.owner = ''
        self.data.title = ''
        self.data.bvid = ''
        self.data.video_done = 0
        self.data.video_all = 0
        self.data.audio_done = 0
        self.data.audio_all = 0
        self.credential = Credential(sessdata=self.data.sessdata, bili_jct=self.data.bili_jct, buvid3=self.data.buvid3)
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
        self.mix_thread.msg.connect(lambda msg: self.log_text.append(msg))
        self.mix_thread.end.connect(lambda: self.mix_btn.setEnabled(True))

        layout = QVBoxLayout()
        for widget in [self.bvid_edit, self.title_label, self.owner_label]:
            layout.addWidget(widget)
        layout.addWidget(self.download_btn)
        for widgets in [[self.video_bar, self.setting_btn], [self.audio_bar, self.mix_btn]]:
            hbox = QHBoxLayout()
            for widget in widgets:
                hbox.addWidget(widget)
            layout.addLayout(hbox)
        layout.addWidget(self.log_text)
        self.setLayout(layout)

    def enter_handler(self):
        """get info"""
        bvid = self.bvid_edit.text()
        if not bvid:
            return
        try:
            words = bvid.split(' ')
            if len(words) == 1:  # 形如f'{BVID}', 无需多操作，只需分P设为0
                self.data.pid = 0
            elif len(words) == 2:  # 形如f'{BVID} p{PID}'或f'{BVID} P{PID}'或f'{BVID} {PID}'
                bvid = words[0]
                try:
                    if words[1].lower().startswith('p'):
                        self.data.pid = int(words[1][1:]) - 1
                    else:
                        self.data.pid = int(words[1]) - 1
                except ValueError:
                    raise ValueError('分P号必须为整数！')
            else:  # 格式错误
                raise ValueError('格式错误')
            self.video = video.Video(bvid=bvid, credential=self.credential)
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
            self.data.is_multi = len(info['pages']) > 1
            self.data.grand_title = info['title']
            self.data.sub_title = info['pages'][self.data.pid]['part']
            if self.data.is_multi:
                self.data.title = f'{self.data.grand_title} - p{self.data.pid + 1} {self.data.sub_title}'
            else:
                self.data.title = self.data.grand_title  # 单P视频的大小标题是一样的
            self.data.bvid = self.video.get_bvid()
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
        dir = f'downloads/{Data.remove_banned_chars(self.data.owner)}/{self.data.bvid} - {Data.remove_banned_chars(self.data.grand_title)}'
        if not os.path.exists(dir):
            os.makedirs(dir)
        if self.data.is_multi:
            path = f'./{dir}/P{self.data.pid + 1} {Data.remove_banned_chars(self.data.sub_title)}.mp4'
        else:
            path = f'./{dir}/{Data.remove_banned_chars(self.data.grand_title)}.mp4'
        if os.path.exists(path):
            self.log_text.append(f'目标文件已存在，安全起见，请先自行检查这个路径，确认是否需要重新混流，并删除原文件：{os.path.abspath(path)}')
            return

        self.mix_thread.path = path
        self.mix_thread.cmd = f'ffmpeg -i video_temp.m4s -i audio_temp.m4s -vcodec copy -acodec copy "{path}"'
        self.mix_btn.setEnabled(False)
        self.mix_thread.start()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Bilibili video downloader')
    parser.add_argument('-p', '--proxy', type=str, help='Set the proxy for downloading')
    args = parser.parse_args()

    if args.proxy is not None:
        print('use proxy:', args.proxy)
        settings.proxy = args.proxy

    app = QApplication(sys.argv)
    dark_style(app)
    app.setFont(QFont('Microsoft YaHei', 15))
    window = Window()
    window.show()
    sys.exit(app.exec_())
