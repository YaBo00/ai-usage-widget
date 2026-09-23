@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  AI 中转站用量悬浮窗 - 调试模式
echo  关闭本窗口即退出程序
echo ============================================
"C:\Users\10201\AppData\Local\Programs\Python\Python314\python.exe" "%~dp0ai_usage_widget.py"
pause
