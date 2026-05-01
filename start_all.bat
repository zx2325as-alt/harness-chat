@echo off
echo ===================================================
echo          Harness Chat Full-Stack Starter
echo ===================================================
echo.

REM Start Backend
echo Starting Backend (Uvicorn on port 8000)...
start "Harness Chat Backend" cmd /c "cd backend && uvicorn app:app --reload --port 8000"

REM Start Frontend
echo Starting Frontend (Vue CLI on port 8080)...
start "Harness Chat Frontend" cmd /c "cd frontend && npm run serve"

echo.
echo All services are starting in separate windows!
echo.
echo Backend API: http://127.0.0.1:8000
echo Frontend UI: http://localhost:8080 (Might take a few seconds to compile)
echo.
echo Please keep the two new command windows open.
echo Close them to stop the services.
echo ===================================================
pause