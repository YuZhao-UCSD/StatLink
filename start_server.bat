@echo off
set "PATH=D:\Software\Anaconda;D:\Software\Anaconda\Library\bin;D:\Software\Anaconda\Scripts;C:\Program Files\MiKTeX\miktex\bin\x64;%PATH%"
set "PYTHONIOENCODING=utf-8"
echo Starting StatLink Web Application at http://127.0.0.1:5050 ...
start "" "http://127.0.0.1:5050"
python "%~dp0app.py"
