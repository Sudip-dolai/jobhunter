@echo off
setlocal enabledelayedexpansion
echo.
echo Open one of these addresses in your phone browser (phone and PC on the same Wi-Fi):
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
  set "ip=%%a"
  set "ip=!ip: =!"
  echo    http://!ip!:8501
)
echo.
echo If Windows asks about the firewall, allow access on Private networks.
echo.
call venv\Scripts\activate
streamlit run streamlit_app.py --server.address 0.0.0.0
