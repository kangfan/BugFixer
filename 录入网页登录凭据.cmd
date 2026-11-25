@echo off
pushd "%~dp0"
"C:\Users\admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe" -m bugfixer credentials edit
popd
