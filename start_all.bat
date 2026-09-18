@echo off
chcp 65001 >nul
title 运动助手 - 启动器
echo ============================================
echo  个人运动分析与训练助手 - 一键启动
echo ============================================
echo.

cd /d D:\personal-toolbox

echo [1/4] 启动后端 (8000)...
start "backend-8000" cmd /c "cd /d D:\personal-toolbox\backend && .venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000"

echo [2/4] 启动前端 (5173)...
start "frontend-5173" cmd /c "cd /d D:\personal-toolbox\frontend && npm run dev"

echo [3/4] 启动知识库语义服务 (8100, 可选)...
start "model-server-8100" cmd /c "cd /d D:\personal-toolbox\backend && D:\python\python.exe scripts\model_server.py --port 8100 --device auto"

echo [4/4] 启动 OCR 识图服务 (8200, 可选)...
start "ocr-server-8200" cmd /c "cd /d D:\personal-toolbox\backend && set CODEBUDDY_SAFE_DELETE_ENABLED=0&& set EASYOCR_MODEL_DIR=C:/Users/39813/.EasyOCR/model&& set OCR_CUDA=0&& ocr_venv\Scripts\python.exe scripts\ocr_server.py --port 8200"

echo.
echo OCR 用 CPU 模式：不占 GPU 显存，和语义服务(8100)互不冲突；识图稍慢但每天一次够用。
echo 想用 GPU 加速就把 OCR_CUDA=0 改成 1（但会和语义服务抢显存，需先停 8100）。

echo.
echo 全部启动命令已发出。稍等几秒，浏览器打开:
echo   前端  http://127.0.0.1:5173
echo   接口  http://127.0.0.1:8000/docs
echo.
echo 说明:
echo   - 8100/8200 是可选增强，加载模型要 10 秒左右，没起来也不影响主功能
echo   - 关闭某个窗口即停对应服务；重新双击本脚本可再启
echo.
pause
