@echo off
REM Windows-friendly wrapper for the test runner
python -u "%~dp0run_tests" %*
