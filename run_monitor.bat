@echo off
cd /d "C:\Users\刘天龙\Desktop\jumpshop自动监控"
python monitor_loop.py --once --state-file=data/jumpshop_state.json --db=data/products.db
