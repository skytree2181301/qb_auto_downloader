@echo off
REM 设置窗口标题，方便识别
title qB AI 助手 - 动漫音乐下载

REM 激活 Conda 环境
REM 假设你的 Anaconda 安装在默认路径，并且 Conda 已经添加到 PATH
REM 如果不行，你可能需要提供 Conda 的完整路径，例如：
REM call "C:\Users\YourUser\anaconda3\Scripts\activate.bat" qb_auto_download
call conda init
call conda activate qb_auto_download

REM 检查环境是否成功激活
if %errorlevel% neq 0 (
    echo 错误：Conda 环境激活失败！请检查环境名称或路径。
    pause
    exit /b %errorlevel%
)

REM 设置代理环境变量
REM 请确保这里的端口和协议是V2Ray实际监听的SOCKS5代理
set HTTP_PROXY=HTTP://127.0.0.1:10808
set HTTPS_PROXY=HTTP://127.0.0.1:10808
set NO_PROXY=localhost,127.0.0.1

REM 运行你的 Python 脚本
REM 注意：这里的路径是完整的脚本路径
echo.
echo 正在启动 AI 助手脚本...
python D:\qb_auto_downloader\interactive_qb_ai_v2.py

REM 脚本执行完毕后，保持窗口开启，方便查看日志
echo.
echo 脚本执行完毕。按任意键退出。
pause