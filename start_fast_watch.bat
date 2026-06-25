@echo off
cd /d "C:\Users\刘天龙\Desktop\jumpshop自动监控"
echo Fast Watch starting... (3 products, every 30s)
echo Close this window to stop.
echo.
python fast_watch.py run
pause
