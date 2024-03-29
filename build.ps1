pyinstaller -F -w -i bili.ico gui.py
mv dist/gui.exe B站视频下载.exe
rm ./dist
rm -r -Force ./build
rm gui.spec