@echo off
REM Always-on Watchtower runner — invoked by Windows Task Scheduler.
REM Uses the project venv and reads credentials from .env (no env needed here).
"C:\Users\anura\anu_projects\market_mcp\.venv\Scripts\python.exe" "C:\Users\anura\anu_projects\market_mcp\watchtower_cron.py"
